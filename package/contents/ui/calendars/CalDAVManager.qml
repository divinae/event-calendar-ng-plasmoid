import QtQuick
import org.kde.kirigami as Kirigami

import "../ErrorType.js" as ErrorType
import "../ScriptPath.js" as ScriptPath
import "../lib"
import "../lib/Async.js" as Async
import "../lib/Base64Compat.js" as Base64Compat
import "../Shared.js" as Shared
import "CalDAVErrors.js" as CalDAVErrors

// CalDAV calendars (Nextcloud, Fastmail, iCloud, Radicale, …) through scripts/caldav.py: the
// selected calendars of the accounts in caldavAccounts (Settings > Calendars > CalDAV), read for the shown
// range, and events created, changed, deleted and imported there. The password stays in the
// keyring (scope caldav-account); the helper reads it there, it's never on a command line.
// Repeating events: each occurrence is an event (recurringEventId = the series' UID,
// caldavRecurrenceId = its original start); one occurrence changes with --recurrence-id.
// Undo of a delete (restoreEvent): the helper keeps the resource as it was (a token per delete).
CalendarManager {
	id: caldavManager

	calendarManagerId: "CalDAV"
	property var accounts: [] // caldavAccounts: [{ id, label, url, username, calendars: [{ href, name, color, writable, selected, userColor }] }]

	ExecUtil { id: executable }
	Logger {
		id: logger
		name: "eventcalendar-caldav"
		showDebug: plasmoid.configuration.debugging
	}

	property string helperPath: ScriptPath.scriptPath("caldav.py") // (A test harness points it elsewhere)

	// The calendar ids: "caldav-<account>::<calendar url>" (the part before :: is the account).
	function calendarIdOf(account, calendar) {
		return "caldav-" + account.id + "::" + calendar.href
	}

	function getCalendarList() {
		var out = []
		;(accounts || []).forEach(function(account) {
			;(account.calendars || []).forEach(function(calendar) {
				if (calendar.selected === false) {
					return
				}
				out.push({
					id: calendarIdOf(account, calendar),
					summary: String(calendar.name || calendar.href),
					calendarName: String(calendar.name || calendar.href), // The picker's label (CalendarSelection)
					backgroundColor: String(calendar.userColor || calendar.color || Kirigami.Theme.highlightColor), // userColor: picked in the widget
					accessRole: calendar.writable === false ? 'reader' : 'owner',
					isTasklist: false,
					accountId: account.id,
					accountLabel: String(account.label || account.username || ""),
					username: String(account.username || ""),
					caldavHref: calendar.href,
				})
			})
		})
		return out
	}

	// The helper's error (CalDAVErrors.parse) as a message for the user.
	function helperError(stderr, calendarName) {
		var parsed = CalDAVErrors.parse(stderr)
		var kind = parsed.kind
		var text = parsed.text
		if (kind === "AUTH") {
			return { kind: kind, message: i18n("CalDAV: %1 didn't accept the user name or password", calendarName) }
		}
		if (kind === "NETWORK") {
			return { kind: kind, message: i18n("Could not connect") }
		}
		if (kind === "KEYRING") {
			return { kind: kind, message: i18n("CalDAV: the password couldn't be read from the keyring") }
		}
		if (kind === "DEPS") {
			return { kind: kind, message: i18n("CalDAV needs Python modules: install them in Settings > Calendars > CalDAV") }
		}
		if (kind === "CONFLICT") {
			return { kind: kind, message: i18n("The event was changed elsewhere meanwhile; it's loaded again") }
		}
		return { kind: kind, message: i18n("CalDAV: %1", text || i18n("Unknown error")) }
	}

	// helper(args, calendar, callback(err, data)): runs caldav.py with the calendar's account.
	function helper(args, calendar, callback) {
		var argv = ['python3', helperPath].concat(args).concat(['--account', calendar.accountId, '--user', calendar.username])
		executable.execArgv(argv, function(cmd, exitCode, exitStatus, stdout, stderr) {
			if (exitCode) {
				logger.log('caldav.helperError', args[0], exitCode) // No event data or URLs in logs
				callback(helperError(stderr, calendar.summary), null)
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

	// The event's fields as the helper takes them (Google style), base64 JSON on its command line.
	function eventArg(fields) {
		var body = {}
		;['summary', 'location', 'description', 'start', 'end', 'recurrence'].forEach(function(key) {
			if (fields && fields[key] !== undefined) {
				body[key] = fields[key]
			}
		})
		return Base64Compat.base64EncodeString(JSON.stringify(body)) // UTF-8
	}

	// The keyring couldn't be read (KEYRING): at login the Secret Service or KWallet may answer only
	// a little after the widget's first fetch. Fetched again a few times before it's an error.
	Timer {
		id: keyringRetry
		property int attempts: 0
		readonly property int maxAttempts: 6
		interval: 15000
		onTriggered: {
			attempts += 1
			if (caldavManager.dateMin < caldavManager.dateMax) {
				caldavManager.fetchAll() // The last range
			}
		}
	}

	//--- Reading
	// The calendar unchanged since the last fetch of the same range (its change tag, see caldav.py
	// query --if-ctag): its events aren't downloaded and parsed again, the last ones are kept (with
	// the calendar's color and rights applied again). A server that gave no tag isn't asked again.
	function fetchCalendar(calendar) {
		caldavManager.asyncRequests += 1
		var calendarId = calendar.id
		var generation = caldavManager.fetchGeneration
		var range = Shared.localDateString(dateMin) + '/' + Shared.localDateString(dateMax)
		var previous = previousEventsByCalendar[calendarId]
		var args = ['query', '--url', calendar.caldavHref, Shared.localDateString(dateMin), Shared.localDateString(dateMax)]
		if (previous && typeof previous.ctag === "string") {
			if (!previous.ctag) {
				args.push('--no-ctag') // The server has none
			} else if (previous.range === range) {
				args.push('--if-ctag=' + previous.ctag) // One argument: a tag may start with "-"
			}
		}
		helper(args, calendar, function(err, data) {
			if (generation !== caldavManager.fetchGeneration) {
				return // A newer round started meanwhile (its own counts)
			}
			try {
				if (err) {
					restorePreviousCalendar(calendarId) // Keep what was shown
					if (err.kind === "KEYRING" && keyringRetry.attempts < keyringRetry.maxAttempts) {
						keyringRetry.start() // Quietly: at login the keyring may not be up yet
						return
					}
					caldavManager.error(err.message, err.kind === "NETWORK" ? ErrorType.NetworkError : ErrorType.UnknownError)
					return
				}
				keyringRetry.attempts = 0
				if (data && data.unchanged && previous) {
					setCalendarData(calendarId, previous) // This range's, current; parsed again
					return
				}
				if (!(data && Array.isArray(data.items))) {
					return
				}
				data.range = range
				setCalendarData(calendarId, data) // Parsed by parseCalendarData
			} finally {
				caldavManager.asyncRequestsDone += 1
			}
		})
	}

	//--- Search
	// Events matching query in the selected calendars between timeMin and timeMax (Dates): each
	// calendar read for that range, the helper keeps those with every word in their title,
	// location or description (as the loaded events are searched). callback(err, items): parsed
	// like fetched events.
	function searchEvents(query, timeMin, timeMax, callback) {
		var tasks = getCalendarList().map(function(calendar) {
			return function(done) {
				helper(['query', '--url', calendar.caldavHref, Shared.localDateString(timeMin), Shared.localDateString(timeMax),
					'--match=' + query], calendar, function(err, data) {
					if (err || !(data && Array.isArray(data.items))) {
						done(err ? err.message : 'searchEvents: no items', null)
						return
					}
					calendarParsing(calendar.id, data)
					done(null, data.items)
				})
			}
		})
		Async.settleAll(tasks, function(results) {
			var found = []
			var firstError = null
			results.forEach(function(result) {
				firstError = firstError || result.err
				found = found.concat(result.data || [])
			})
			callback(found.length ? null : firstError, found)
		})
	}

	onFetchAllCalendars: {
		movedResources = {} // Read again: loaded where they are now
		getCalendarList().forEach(function(calendar) {
			fetchCalendar(calendar)
		})
	}

	function parseCalendarData(calendarId, data) {
		var calendar = getCalendar(calendarId)
		if (!calendar) {
			return
		}
		data.items.forEach(function(event) {
			event.backgroundColor = calendar.backgroundColor
			// caldavReadOnly: occurrences without their series in the resource (see caldav.py).
			event.canEdit = calendar.accessRole === 'owner' && !event.caldavReadOnly
		})
		// What the calendar gave its events: an unchanged calendar (the same tag) with another
		// color or rights is still shown anew (EventModel's unchanged check compares it).
		data.look = calendar.backgroundColor + '|' + calendar.accessRole
	}

	//--- Writing
	// write(calendar, args, ok(data), done(err), failed(report)): runs the helper, then reads
	// everything again (the server's final form, or the current version after a conflict).
	function write(calendar, args, ok, done, failed) {
		helper(args, calendar, function(err, data) {
			if (err) {
				reportError(err.message, failed)
			} else if (ok) {
				ok(data)
			}
			caldavManager.refresh()
			if (done && !(err && failed)) {
				done(err ? err.message : null)
			}
		})
	}

	// Shows the error, or hands it to failed(report).
	function reportError(message, failed) {
		var report = function() {
			caldavManager.error(message, ErrorType.UnknownError)
		}
		if (failed) {
			failed(report)
		} else {
			report()
		}
	}

	// The new etag on every loaded event of the resource: no stale If-Match before the refresh.
	function adoptEtag(calendarId, href, data) {
		if (!href || !data || !data.etag) {
			return
		}
		eventItems(calendarId).forEach(function(item) {
			if (item.caldavHref === href) {
				item.caldavEtag = data.etag
			}
		})
	}

	// An occurrence of a repeating event (changed or deleted on its own with --recurrence-id).
	function isOccurrence(event) {
		return !!(event && event.recurringEventId && event.caldavRecurrenceId)
	}
	function recurrenceIdArgs(event) {
		// With "=": one argument, whatever the JSON holds.
		return isOccurrence(event) ? ['--recurrence-id=' + JSON.stringify(event.caldavRecurrenceId)] : []
	}

	// A loaded occurrence of the series with that UID (its resource's href and etag), or null.
	function seriesOccurrence(calendarId, seriesId) {
		var items = eventItems(calendarId)
		for (var i = 0; i < items.length; i++) {
			if (items[i].recurringEventId === seriesId && items[i].caldavHref) {
				return items[i]
			}
		}
		return null
	}

	function createEventFromData(calendarId, eventData, done) {
		var calendar = getCalendar(calendarId)
		if (!calendar) {
			if (done) done('createEventFromData: unknown calendar')
			return
		}
		write(calendar, ['put', '--url', calendar.caldavHref, '--event-b64', eventArg(eventData)], function() {
			eventCreated(calendarId, eventData)
		}, done)
	}

	// Quick add: the text as the title, on date (at time for an hour, else all day).
	function createEvent(calendarId, date, text, time) {
		var day = date || new Date()
		var data = { summary: text }
		if (time) {
			var parts = String(time).split(':')
			var start = new Date(day.getFullYear(), day.getMonth(), day.getDate(), Number(parts[0]) || 0, Number(parts[1]) || 0)
			data.start = { dateTime: start.toISOString() }
			data.end = { dateTime: new Date(start.getTime() + 3600000).toISOString() }
		} else {
			data.start = { date: Shared.localDateString(day) }
			data.end = { date: Shared.localDateString(Shared.addDays(day, 1)) }
		}
		createEventFromData(calendarId, data, null)
	}

	// The loaded event and its calendar, or null (with done told) when it isn't loaded.
	function loadedEvent(calendarId, eventId, done, what) {
		var calendar = getCalendar(calendarId)
		var event = getEvent(calendarId, eventId)
		if (!calendar || !event || !event.caldavHref) {
			logger.log('caldav.' + what, 'event not loaded')
			if (done) done(what + ': event not loaded')
			return null
		}
		return { calendar: calendar, event: event }
	}

	// One event, or one occurrence. options: { done(err), failed(report) } (see write).
	function setEventProperties(calendarId, eventId, args, options) {
		var done = options && options.done
		var loaded = loadedEvent(calendarId, eventId, done, 'setEventProperties')
		if (!loaded) {
			return
		}
		write(loaded.calendar, ['put', '--url', loaded.calendar.caldavHref, '--href', loaded.event.caldavHref,
			'--etag', String(loaded.event.caldavEtag || ''), '--event-b64', eventArg(args)].concat(recurrenceIdArgs(loaded.event)), function(data) {
			adoptEtag(calendarId, loaded.event.caldavHref, data)
			eventUpdated(calendarId, eventId, loaded.event)
		}, done, options && options.failed)
	}

	// The server copy of an event, or of a series' master by its UID. callback(err, event)
	function fetchEvent(calendarId, eventId, callback) {
		var event = getEvent(calendarId, eventId)
		if (event) {
			callback(null, event)
			return
		}
		var calendar = getCalendar(calendarId)
		// A series by an occurrence; an event just moved here (not loaded yet) by where it went.
		var resource = seriesOccurrence(calendarId, eventId) || movedResources[calendarId + '\n' + eventId]
		if (!calendar || !resource) {
			callback('fetchEvent: event not loaded', null)
			return
		}
		helper(['get', '--href', resource.caldavHref], calendar, function(err, series) {
			if (err) {
				callback(err.message, null)
			} else {
				series.calendarId = calendarId
				callback(null, series)
			}
		})
	}

	// The editor's update (buildArgs(serverEvent) -> args) of an event, an occurrence or a whole
	// series (by its UID). Optional failed(report), see write.
	function updateEventFromServer(calendarId, eventId, buildArgs, done, options, failed) {
		var event = getEvent(calendarId, eventId)
		if (event) {
			setEventProperties(calendarId, eventId, buildArgs(event), { done: done, failed: failed })
			return
		}
		var calendar = getCalendar(calendarId)
		fetchEvent(calendarId, eventId, function(err, series) {
			if (err || !series || !calendar) {
				reportError(err ? String(err) : i18n("CalDAV: %1", i18n("Unknown error")), failed)
				return
			}
			write(calendar, ['put', '--url', calendar.caldavHref, '--href', series.caldavHref,
				'--etag', String(series.caldavEtag || ''), '--event-b64', eventArg(buildArgs(series))], function(data) {
				adoptEtag(calendarId, series.caldavHref, data)
			}, done, failed)
		})
	}

	function deleteEvent(calendarId, eventId, done) {
		var loaded = loadedEvent(calendarId, eventId, done, 'deleteEvent')
		if (!loaded) {
			return
		}
		// An occurrence: only it goes (an EXDATE in its series).
		write(loaded.calendar, ['delete', '--href', loaded.event.caldavHref, '--etag', String(loaded.event.caldavEtag || '')]
			.concat(recurrenceIdArgs(loaded.event)), function(data) {
			keepUndo(calendarId, eventId, data)
			adoptEtag(calendarId, loaded.event.caldavHref, data) // The series' other occurrences
			eventDeleted(calendarId, eventId, loaded.event)
			removeEvent(calendarId, eventId)
		}, done)
	}

	// A whole series by its UID. done(err), always called.
	function deleteSeries(calendarId, seriesId, done) {
		var calendar = getCalendar(calendarId)
		var occurrence = seriesOccurrence(calendarId, seriesId)
		if (!calendar || !occurrence) {
			logger.log('caldav.deleteSeries', 'series not loaded')
			if (done) done('deleteSeries: series not loaded')
			return
		}
		write(calendar, ['delete', '--href', occurrence.caldavHref, '--etag', String(occurrence.caldavEtag || '')], function(data) {
			keepUndo(calendarId, seriesId, data)
			eventItems(calendarId).slice().forEach(function(item) {
				if (item.recurringEventId === seriesId) {
					removeEvent(calendarId, item.id)
					eventDeleted(calendarId, item.id, item)
				}
			})
		}, done)
	}

	// The undo tokens of the deletes: { "<calendar id>\n<event or series id>": token }.
	property var undoTokens: ({})
	function keepUndo(calendarId, eventId, data) {
		if (data && data.undo) {
			undoTokens[calendarId + '\n' + eventId] = data.undo
		}
	}

	// Undo a delete (eventId: the event, the occurrence or the series' UID): the resource put back as
	// it was. If that fails, copy (Undo.restoreCopy, single events only) is created as a new event.
	// done(err), always called (refreshed afterwards).
	function restoreEvent(calendarId, eventId, copy, done) {
		var calendar = getCalendar(calendarId)
		var key = calendarId + '\n' + eventId
		var token = undoTokens[key]
		var fallback = function(report) {
			if (copy && calendar) {
				createEventFromData(calendarId, copy, done)
			} else {
				report()
				if (done) done('restoreEvent: failed')
			}
		}
		if (!calendar || !token) {
			fallback(function() { reportError(i18n("CalDAV: %1", i18n("Unknown error"))) })
			return
		}
		delete undoTokens[key]
		write(calendar, ['restore', '--undo', token], null, done, fallback)
	}

	// Events moved to another calendar, until it's read again: { "<calendar id>\n<event id>":
	// { caldavHref, caldavEtag } } (the editor saves its other changes there right after the move).
	property var movedResources: ({})

	// Move an event, or a series (eventId: its UID), to another calendar of the same account (its
	// resource, see caldav.py move). done(err, { id }), always called.
	function moveEvent(calendarId, eventId, destinationCalendarId, done, params) {
		var calendar = getCalendar(calendarId)
		var destination = getCalendar(destinationCalendarId)
		var event = getEvent(calendarId, eventId) || seriesOccurrence(calendarId, eventId)
		var fail = function(message) {
			logger.log('caldav.moveEvent', message)
			if (done) done('moveEvent: ' + message, null)
		}
		if (!calendar || !event || !event.caldavHref) {
			fail('event not loaded')
			return
		}
		if (!destination || destination.accountId !== calendar.accountId) {
			fail('destination is in another account')
			return
		}
		helper(['move', '--href', event.caldavHref, '--etag', String(event.caldavEtag || ''), '--to', destination.caldavHref], calendar, function(err, data) {
			if (err) {
				reportError(err.message)
				if (done) done(err.message, null)
				return
			}
			movedResources[destinationCalendarId + '\n' + eventId] = { caldavHref: data.href, caldavEtag: data.etag }
			eventItems(calendarId).slice().forEach(function(item) {
				if (item.caldavHref === event.caldavHref) { // The event, or all of the series' occurrences
					removeEvent(calendarId, item.id)
				}
			})
			if (done) done(null, { id: eventId })
		})
	}

	// Import (ImportView, one event at a time, Google-style fields): created as a new event. Not
	// read again after each: the import card refreshes once at its end.
	function importEvent(calendarId, eventData, done) {
		var calendar = getCalendar(calendarId)
		if (!calendar) {
			done(i18n("This calendar can't import events"), null)
			return
		}
		helper(['put', '--url', calendar.caldavHref, '--event-b64', eventArg(eventData)], calendar, function(err, data) {
			done(err ? err.message : null, err ? null : data)
		})
	}
}
