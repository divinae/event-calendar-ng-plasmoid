import QtQuick
import org.kde.kirigami as Kirigami

import "../ErrorType.js" as ErrorType
import "../ScriptPath.js" as ScriptPath
import "../Shared.js" as Shared
import "../TaskLists.js" as TaskLists
import "../Tasks.js" as Tasks
import "../lib"
import "../lib/Base64Compat.js" as Base64Compat
import "CalDAVErrors.js" as CalDAVErrors

// The task lists of one CalDAV account (Nextcloud Tasks, …): its calendars for tasks (VTODO) picked
// in Settings > Calendars > CalDAV (tasksSelected), through scripts/caldav.py (tasks, put-task and
// the list commands; see scripts/vtodo.py for the fields). Same contract as TickTickTasksManager:
// lists are task lists with ids "caldavtasks-<account>::<list url>", tasks event-shaped items (kind
// 'tasks#task') with the Google fields the UI reads and the provider-neutral ones (dueTime,
// reminders, priority, parentId; Tasks.capabilities('CalDAVTasks')), plus caldavHref / caldavEtag
// to change them. Changes come in Google shape (Tasks.editBody, toggleBody, restoreBody).
// The open tasks and those done in the last 30 days are loaded, each list skipped while its change
// tag is the same. A deleted task can be brought back (the helper keeps it, see restoreEvent).
CalendarManager {
	id: caldavTasksManager

	calendarManagerId: "CalDAVTasks"
	property var account: null // One of caldavAccounts
	property var accountsConfig: null // appletConfig.caldavAccounts: lists made or changed here are stored there
	readonly property string accountId: account ? "caldavtasks-" + account.id : ""
	readonly property string accountLabel: account ? String(account.label || account.username || "") : ""
	property string helperPath: ScriptPath.scriptPath("caldav.py")

	ExecUtil { id: executable }
	Logger {
		id: logger
		name: "eventcalendar-caldav-tasks"
		showDebug: plasmoid.configuration.debugging
	}

	function listIdOf(calendar) {
		return accountId + "::" + calendar.href
	}

	// The account's calendars that hold tasks and are picked as task lists.
	function taskCalendars() {
		return (account && account.calendars || []).filter(function(c) {
			return c && c.tasks === true && c.tasksSelected === true
		})
	}

	function getCalendarList() {
		return taskCalendars().map(function(calendar) {
			return {
				id: listIdOf(calendar),
				summary: String(calendar.name || calendar.href),
				calendarName: String(calendar.name || calendar.href),
				backgroundColor: String(calendar.userColor || calendar.color || Kirigami.Theme.highlightColor),
				accessRole: calendar.writable === false ? 'reader' : 'owner',
				isTasklist: true,
				caldavHref: calendar.href,
			}
		})
	}
	function isTasklistSelected(calendarId) {
		return !!getCalendar(calendarId)
	}

	//--- The helper
	function helperError(stderr) {
		var parsed = CalDAVErrors.parse(stderr)
		if (parsed.kind === "AUTH") {
			return { kind: parsed.kind, message: i18n("CalDAV: %1 didn't accept the user name or password", accountLabel) }
		}
		if (parsed.kind === "NETWORK") {
			return { kind: parsed.kind, message: i18n("Could not connect") }
		}
		if (parsed.kind === "KEYRING") {
			return { kind: parsed.kind, message: i18n("CalDAV: the password couldn't be read from the keyring") }
		}
		if (parsed.kind === "DEPS") {
			return { kind: parsed.kind, message: i18n("CalDAV needs Python modules: install them in Settings > Calendars > CalDAV") }
		}
		if (parsed.kind === "CONFLICT") {
			return { kind: parsed.kind, message: i18n("The event was changed elsewhere meanwhile; it's loaded again") }
		}
		return { kind: parsed.kind, message: i18n("CalDAV: %1", parsed.text || i18n("Unknown error")) }
	}
	// helper(args, callback(err, data)): caldav.py with this account (the password stays in the keyring).
	function helper(args, callback) {
		if (!account) {
			callback({ kind: "INPUT", message: i18n("CalDAV: %1", i18n("Unknown error")) }, null)
			return
		}
		var argv = ['python3', helperPath].concat(args).concat(['--account', account.id, '--user', String(account.username || '')])
		executable.execArgv(argv, function(cmd, exitCode, exitStatus, stdout, stderr) {
			if (exitCode) {
				logger.log('caldavTasks.helperError', args[0], exitCode) // No task data or URLs in logs
				callback(helperError(stderr), null)
				return
			}
			var data
			try {
				data = JSON.parse(stdout)
			} catch (e) {
				callback({ kind: "PROTOCOL", message: i18n("CalDAV: an unexpected answer") }, null)
				return
			}
			callback(null, data)
		})
	}
	function report(err) {
		caldavTasksManager.error(err.message, err.kind === "NETWORK" ? ErrorType.NetworkError : ErrorType.UnknownError)
	}

	// A task's changes as the helper takes them (Tasks.editBody's Google shape), base64 JSON. A
	// timed due is in the system's zone (the helper reads its name; TimeZone.guessLocal only guesses
	// one with the same offsets).
	function taskArg(body) {
		var out = {}
		;['title', 'notes', 'due', 'dueTime', 'status', 'completed', 'priority', 'reminders', 'parentId', 'repeatRule'].forEach(function(key) {
			if (body && body[key] !== undefined) {
				out[key] = body[key]
			}
		})
		return Base64Compat.base64EncodeString(JSON.stringify(out))
	}

	//--- Reading
	onFetchAllCalendars: {
		getCalendarList().forEach(function(list) {
			fetchList(list)
		})
	}

	// The list unchanged since the last fetch (its change tag): the last tasks are kept, placed again.
	function fetchList(list) {
		caldavTasksManager.asyncRequests += 1
		var generation = caldavTasksManager.fetchGeneration
		var previous = previousEventsByCalendar[list.id]
		var args = ['tasks', '--url', list.caldavHref]
		if (previous && typeof previous.ctag === "string") {
			args.push(previous.ctag ? '--if-ctag=' + previous.ctag : '--no-ctag')
		}
		helper(args, function(err, data) {
			if (generation !== caldavTasksManager.fetchGeneration) {
				return
			}
			try {
				if (err) {
					restorePreviousCalendar(list.id)
					report(err)
					return
				}
				if (data && data.unchanged && previous) {
					setCalendarData(list.id, previous)
					return
				}
				if (!(data && Array.isArray(data.items))) {
					return
				}
				data.items = data.items.map(taskItem)
				data.items.sort(function(a, b) { // Its order (X-APPLE-SORT-ORDER); AgendaModel sorts again
					var ap = a.position || '', bp = b.position || ''
					return ap < bp ? -1 : ap > bp ? 1 : 0
				})
				setCalendarData(list.id, data)
			} finally {
				caldavTasksManager.asyncRequestsDone += 1
			}
		})
	}

	// A sort order (a number, may be negative) as a position string like Google's.
	function positionOf(sortOrder) {
		var n = Number(sortOrder)
		if (sortOrder === undefined || sortOrder === null || !isFinite(n)) {
			return undefined
		}
		var s = String(Math.max(0, Math.round(n + 9007199254740992)))
		while (s.length < 20) {
			s = '0' + s
		}
		return s
	}

	// The helper's task as an event-shaped item (placed by placeTask).
	function taskItem(raw) {
		var item = {}
		for (var key in raw) {
			item[key] = raw[key]
		}
		item.kind = 'tasks#task'
		item.summary = item.title
		item.description = item.notes
		item.htmlLink = ''
		var position = positionOf(raw.sortOrder)
		if (position) {
			item.position = position
		}
		placeTask(item)
		return item
	}

	// All-day on the day it's shown on (Tasks.placementDay), its due day's fields.
	function placeTask(item) {
		item.isCompleted = Tasks.isDone(item)
		var dueDay = Tasks.dueDay(item)
		if (dueDay) {
			item.dueDate = dueDay
			item.dueDateTime = new Date(dueDay + ' 00:00:00')
			item.dueEndTime = new Date(Tasks.addDays(dueDay, 1) + ' 00:00:00')
		} else {
			delete item.dueDate
			delete item.dueDateTime
			delete item.dueEndTime
		}
		var day = Tasks.placementDay(item, new Date(), !!plasmoid.configuration.agendaPlaceOverdueTasksOnToday)
		item.start = { date: day }
		item.end = { date: Tasks.addDays(day, 1) }
	}

	// Every task gets its list's color and rights; placed again (today may have changed since an
	// unchanged list's tasks were read).
	function parseCalendarData(calendarId, data) {
		var list = getCalendar(calendarId)
		data.items.forEach(function(item) {
			placeTask(item)
			item.summary = item.title // Parsed again: not the "(No title)" of the last time
			defaultEventParsing(calendarId, item)
			item.backgroundColor = list ? list.backgroundColor : Kirigami.Theme.highlightColor.toString()
			item.canEdit = !!list && list.accessRole === 'owner'
		})
	}

	// A task of a shown list that isn't loaded yet, parsed and shown.
	function addTaskItem(calendarId, item) {
		if (!getCalendar(calendarId)) {
			return false
		}
		if (!eventsByCalendar[calendarId]) {
			eventsByCalendar[calendarId] = { items: [] }
		}
		if (getEvent(calendarId, item.id)) {
			return false
		}
		parseSingleEvent(calendarId, item)
		addEvent(calendarId, item)
		return true
	}

	//--- Create
	// date: the due day (a Date), or null for no due date.
	function createEvent(calendarId, date, text) {
		var body = { title: text }
		if (date) {
			body.due = Tasks.dueValue(Tasks.dayString(date))
		}
		createEventFromData(calendarId, body, null, null)
	}

	// A task from a body (Tasks.editBody; restoreBody: also status, completed, parentId).
	// done(err, item): optional, always called. params: ignored (Google's parent / previous).
	function createEventFromData(calendarId, body, done, params) {
		var list = getCalendar(calendarId)
		if (!list) {
			if (done) done('createEvent: unknown list', null)
			return
		}
		helper(['put-task', '--url', list.caldavHref, '--task-b64', taskArg(body)], function(err, raw) {
			if (err) {
				report(err)
				if (done) done(err.message, null)
				return
			}
			var item = taskItem(raw)
			if (addTaskItem(calendarId, item)) {
				eventCreated(calendarId, item)
			}
			if (done) done(null, item)
		})
	}

	//--- Get
	// The server copy of a task: the loaded one (the helper has no single-task read; a change is
	// checked by its etag). callback(err, item)
	function fetchEvent(calendarId, eventId, callback) {
		var item = getEvent(calendarId, eventId)
		callback(item ? null : 'fetchEvent: task not loaded', item || null)
	}

	//--- Update
	// args: Google-shaped changes (Tasks.editBody / toggleBody). options: optional { done(err, item) }.
	function setEventProperties(calendarId, eventId, args, options) {
		var done = options && options.done
		var list = getCalendar(calendarId)
		var item = getEvent(calendarId, eventId)
		if (!list || !item || !item.caldavHref) {
			logger.log('caldavTasks.setEventProperties', 'task not loaded')
			if (done) done('setEventProperties: task not loaded', null)
			return
		}
		helper(['put-task', '--url', list.caldavHref, '--href', item.caldavHref, '--etag', String(item.caldavEtag || ''),
			'--task-b64', taskArg(args)], function(err, raw) {
			if (err) {
				report(err)
				if (err.kind === "CONFLICT") {
					caldavTasksManager.refresh() // Changed elsewhere: the current version
				}
				if (done) done(err.message, null)
				return
			}
			// The task may have been reloaded meanwhile: update the loaded copy.
			var current = getEvent(calendarId, eventId) || item
			var saved = taskItem(raw)
			Shared.merge(current, saved)
			Shared.removeMissingKeys(current, saved) // eg: notes or due cleared
			parseSingleEvent(calendarId, current)
			eventUpdated(calendarId, eventId, current)
			if (done) done(null, current)
		})
	}

	//--- Delete, and its undo
	// The undo tokens of the deletes (see caldav.py delete): { "<list id>\n<task id>": token }.
	property var undoTokens: ({})
	// done(err): optional, always called.
	function deleteEvent(calendarId, eventId, done) {
		var item = getEvent(calendarId, eventId)
		if (!item || !item.caldavHref) {
			logger.log('caldavTasks.deleteEvent', 'task not loaded')
			if (done) done('deleteEvent: task not loaded')
			return
		}
		helper(['delete', '--href', item.caldavHref, '--etag', String(item.caldavEtag || '')], function(err, data) {
			if (err) {
				report(err)
			} else {
				if (data && data.undo) {
					undoTokens[calendarId + '\n' + eventId] = data.undo
				}
				var deleted = getEvent(calendarId, eventId)
				if (deleted) {
					removeEvent(calendarId, eventId)
					eventDeleted(calendarId, eventId, deleted)
				}
			}
			if (done) done(err ? err.message : null)
		})
	}

	// Undo a delete: the task as it was (the helper's copy), else copy (Undo.restoreCopy) created
	// again. done(err): optional, always called.
	function restoreEvent(calendarId, eventId, copy, done) {
		var key = calendarId + '\n' + eventId
		var token = undoTokens[key]
		var fromCopy = function(err) {
			if (copy) {
				createEventFromData(calendarId, Tasks.restoreBody(copy), function(createErr) {
					if (done) done(createErr || null)
				}, null)
			} else {
				if (err) report(err)
				if (done) done(err ? err.message : 'restoreEvent: no copy')
			}
		}
		if (!token) {
			fromCopy(null)
			return
		}
		delete undoTokens[key]
		helper(['restore', '--undo', token], function(err) {
			if (err) {
				fromCopy(err)
				return
			}
			caldavTasksManager.refresh() // Shown again with the list's next read
			if (done) done(null)
		})
	}

	//--- Move to another list of the account
	// done(err, item): always called; item is the moved task.
	function moveEvent(calendarId, eventId, destinationCalendarId, done, params) {
		var item = getEvent(calendarId, eventId)
		var destination = getCalendar(destinationCalendarId)
		var finish = function(err, moved) {
			if (done) done(err || null, moved || null)
		}
		if (!item || !item.caldavHref) {
			logger.log('caldavTasks.moveEvent', 'task not loaded')
			finish('moveEvent: task not loaded', null)
			return
		}
		if (!destination) {
			logger.log('caldavTasks.moveEvent', 'destination is in another account')
			finish('moveEvent: destination is in another account', null)
			return
		}
		helper(['move', '--href', item.caldavHref, '--etag', String(item.caldavEtag || ''), '--to', destination.caldavHref], function(err, data) {
			if (err) {
				report(err)
				finish(err.message, null)
				return
			}
			removeEvent(calendarId, eventId)
			var moved = {}
			for (var key in item) {
				moved[key] = item[key]
			}
			moved.caldavHref = data.href
			moved.caldavEtag = data.etag
			addTaskItem(destinationCalendarId, moved)
			finish(null, moved)
		})
	}

	//--- Task lists: create, rename, recolor, delete. The account's calendars in caldavAccounts follow
	// (a new list is picked; a deleted one goes). done(err, …) is optional and always called.
	function taskListCapabilities(calendarId) {
		var list = getCalendar(calendarId)
		return TaskLists.capabilities(calendarManagerId, calendarId, list ? list.accessRole : 'none')
	}

	// The account in caldavAccounts changed by change(account) (a copy), then stored.
	function storeAccount(change) {
		if (!accountsConfig || !account) {
			return
		}
		var accounts = JSON.parse(JSON.stringify(accountsConfig.value || []))
		accounts.forEach(function(a) {
			if (a.id === account.id) {
				change(a)
			}
		})
		accountsConfig.value = accounts
		accountsConfig.serialize()
	}
	function changeListEntry(href, fields) {
		storeAccount(function(a) {
			;(a.calendars || []).forEach(function(c) {
				if (c.href === href) {
					for (var key in fields) {
						if (fields[key] === undefined) {
							delete c[key]
						} else {
							c[key] = fields[key]
						}
					}
				}
			})
		})
	}
	// Where new lists go: the account's calendar home, else the folder of one of its calendars.
	function calendarHome() {
		if (account && account.home) {
			return String(account.home)
		}
		var first = (account && account.calendars || [])[0]
		return first ? String(first.href).replace(/[^/]+\/?$/, '') : ''
	}
	function refuse(what, calendarId, capability, done) {
		if (taskListCapabilities(calendarId)[capability]) {
			return false
		}
		logger.log('caldavTasks.' + what, 'not allowed')
		if (done) done(what + ': not allowed', null)
		return true
	}

	// done(err, list): list is { id, name }.
	function createTaskList(title, done) {
		var name = TaskLists.cleanTitle(title)
		var home = calendarHome()
		if (!name || !home) {
			if (done) done(name ? 'createTaskList: no calendar home' : 'createTaskList: no title', null)
			return
		}
		helper(['mklist', '--home', home, '--name', name], function(err, made) {
			if (err) {
				report(err)
				if (done) done(err.message, null)
				return
			}
			made.tasksSelected = true
			made.selected = false // Not an events calendar
			storeAccount(function(a) {
				a.calendars = (a.calendars || []).concat([made])
			})
			if (done) done(null, { id: caldavTasksManager.accountId + "::" + made.href, name: made.name })
		})
	}

	function renameTaskList(calendarId, title, done) {
		var name = TaskLists.cleanTitle(title)
		var list = getCalendar(calendarId)
		if (!name || !list) {
			if (done) done('renameTaskList: no title', null)
			return
		}
		if (refuse('renameTaskList', calendarId, 'rename', done)) {
			return
		}
		helper(['setlist', '--url', list.caldavHref, '--name', name], function(err) {
			if (err) {
				report(err)
			} else {
				changeListEntry(list.caldavHref, { name: name })
			}
			if (done) done(err ? err.message : null, null)
		})
	}

	// color: '#rrggbb', the server's from now on (a color picked in the widget's settings goes).
	function setTaskListColor(calendarId, color, done) {
		var hex = TaskLists.hexColor(color)
		var list = getCalendar(calendarId)
		if (!hex || !list) {
			if (done) done('setTaskListColor: not a color', null)
			return
		}
		if (refuse('setTaskListColor', calendarId, 'color', done)) {
			return
		}
		helper(['setlist', '--url', list.caldavHref, '--color', hex], function(err) {
			if (err) {
				report(err)
			} else {
				changeListEntry(list.caldavHref, { color: hex, userColor: undefined })
			}
			if (done) done(err ? err.message : null, null)
		})
	}

	// The list and its tasks are deleted (Nextcloud keeps it in its trash bin for a while).
	function deleteTaskList(calendarId, done) {
		var list = getCalendar(calendarId)
		if (!list) {
			if (done) done('deleteTaskList: unknown list', null)
			return
		}
		if (refuse('deleteTaskList', calendarId, 'delete', done)) {
			return
		}
		helper(['rmlist', '--url', list.caldavHref], function(err) {
			if (err) {
				report(err)
			} else {
				delete eventsByCalendar[calendarId]
				delete previousEventsByCalendar[calendarId]
				storeAccount(function(a) {
					a.calendars = (a.calendars || []).filter(function(c) { return c.href !== list.caldavHref })
				})
			}
			if (done) done(err ? err.message : null, null)
		})
	}
}
