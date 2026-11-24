.pragma library

// https://github.com/KDE/plasma-framework/blob/master/src/declarativeimports/calendar/eventpluginsmanager.h
// https://github.com/KDE/plasma-framework/blob/master/src/declarativeimports/calendar/eventpluginsmanager.cpp

var DEFAULT_PLUGIN_FILENAMES = [
	'holidaysevents.so',
	'pimevents.so',
]

// A missing plugin is reported once per plasmashell run, and not at all for the defaults (eg:
// pimevents.so is on by default but needs kdepim-addons, which many systems don't have).
var warnedMissingPlugins = {}
function warnMissingPlugin(pluginFilename) {
	if (warnedMissingPlugins[pluginFilename] || DEFAULT_PLUGIN_FILENAMES.indexOf(pluginFilename) >= 0) {
		return
	}
	warnedMissingPlugins[pluginFilename] = true
	console.log('[eventcalendar] Plasma calendar plugin not installed:', pluginFilename)
}

function toArray(listLike) {
	if (!listLike) {
		return []
	}
	if (Array.isArray(listLike)) {
		return listLike
	}
	if (typeof listLike === 'string') {
		if (!listLike) {
			return []
		}
		return [listLike]
	}
	if (typeof listLike.length === 'number') {
		var array = []
		for (var i = 0; i < listLike.length; i++) {
			array.push(listLike[i])
		}
		return array
	}
	return []
}

// The Akonadi collections of a PimCalendarsModel (kdepim-addons' pimevents plugin):
// [{ id, name, checked }], checked: shown in the Plasma calendars (the plugin's own selection,
// [PIMEventsPlugin] calendars= in plasmashellrc). Accounts (resources) are left out.
// Current versions: a flat list with the roles collectionId (UserRole + 1), name, enabled (false:
// an account) and checked. Older ones: a tree with a { id, enabled } object in UserRole + 1.
// withAccounts: the accounts too, as { id, name, checked: false, account: true } (also a folder
// without events of its own), and the calendars in an account's tree with the top account's id as
// accountId (none: a calendar that is a resource of its own, eg: a local .ics file); with the
// model's icon name as icon (eg: view-pim-tasks for a task list, which has no color).
var PIM_ROLE_ID = 1, PIM_ROLE_NAME = 2, PIM_ROLE_ENABLED = 3, PIM_ROLE_CHECKED = 4, PIM_ROLE_ICON = 5
function pimCalendarRows(model, userRole, displayRole, withAccounts) {
	var rows = []
	if (!model) {
		return rows
	}
	// The row of index (pushed), or null for none (an account without withAccounts, or no data).
	function push(index, accountId) {
		var data = model.data(index, userRole + PIM_ROLE_ID)
		var row = null
		if (data && typeof data === 'object') {
			row = {
				id: String(data.id),
				name: String(model.data(index, displayRole) || ''),
				checked: !!data.enabled,
			}
		} else if (data !== undefined && data !== null) {
			var account = model.data(index, userRole + PIM_ROLE_ENABLED) === false
			if (account && !withAccounts) {
				return null
			}
			row = {
				id: String(data),
				name: String(model.data(index, userRole + PIM_ROLE_NAME) || model.data(index, displayRole) || ''),
				checked: !account && !!model.data(index, userRole + PIM_ROLE_CHECKED),
			}
			if (account) {
				row.account = true
			}
		}
		if (row) {
			if (withAccounts && accountId && !row.account) {
				row.accountId = accountId
			}
			var iconName = withAccounts ? model.data(index, userRole + PIM_ROLE_ICON) : ''
			if (iconName && typeof iconName === 'string') {
				row.icon = iconName
			}
			rows.push(row)
		}
		return row
	}
	// A tree under the accounts (Akonadi's EntityTreeModel): calendars can be folders in folders.
	function walk(parent, depth, accountId) {
		var count = parent ? model.rowCount(parent) : model.rowCount()
		for (var i = 0; i < count; i++) {
			var index = parent ? model.index(i, 0, parent) : model.index(i, 0)
			var row = push(index, accountId)
			if (depth < 8 && model.hasChildren(index)) {
				walk(index, depth + 1, accountId || (row && row.account ? row.id : '')) // The top account
			}
		}
	}
	walk(null, 0, '')
	return rows
}

// The pimevents plugin's own selection, [PIMEventsPlugin] calendars= in plasmashellrc: a script
// printing its collection ids, comma separated.
function pimSelectionCommand() {
	return ['sh', '-c', 'f="${XDG_CONFIG_HOME:-$HOME/.config}/plasmashellrc"\n'
		+ '[ -f "$f" ] && awk -F= \'/^\\[/{s=($0=="[PIMEventsPlugin]")} s&&$1=="calendars"{print substr($0,11);exit}\' "$f"\n'
		+ 'exit 0']
}
// ["collection id", ...] from pimSelectionCommand's output.
function parsePimSelection(stdout) {
	return String(stdout || '').split(/[,\s]+/).filter(function(id, i, list) {
		return /^[0-9]+$/.test(id) && list.indexOf(id) === i
	})
}
// The selected ids (parsePimSelection) Akonadi no longer has, eg: of an account set up anew, whose
// calendars got new ids (and aren't shown until selected). rows: pimCalendarRows with the
// accounts; none while it's empty (Akonadi not read yet).
function stalePimSelection(selectedIds, rows) {
	if (!rows || !rows.length) {
		return []
	}
	var known = {}
	rows.forEach(function(row) {
		known[row.id] = true
	})
	return (selectedIds || []).filter(function(id) {
		return !known[id]
	})
}

function getPluginFilename(pluginPath) {
	if (!pluginPath || typeof pluginPath !== 'string') {
		return ''
	}
	var lastSlash = pluginPath.lastIndexOf('/')
	if (lastSlash === -1) {
		return pluginPath
	}
	return pluginPath.substr(lastSlash + 1)
}

// The PIM events plugin's calendars model (kdepim-addons), or null without it (it's optional).
function createPimCalendarsModel(parent) {
	try {
		return Qt.createQmlObject("import org.kde.plasma.PimCalendars; PimCalendarsModel {}", parent)
	} catch (e) {
		return null
	}
}
// Whether calendars can be ticked through the model (newer kdepim-addons).
function pimModelCanChoose(model) {
	return !!model && typeof model.setChecked === "function"
}

// Whether pluginList (enabledCalendarPlugins) has the plugin named stem ("pimevents").
function hasPlugin(pluginList, stem) {
	return toArray(pluginList).some(function(plugin) {
		return getPluginStem(plugin) === stem
	})
}

function getPluginStem(pluginRef) {
	var pluginFilename = getPluginFilename(pluginRef)
	if (!pluginFilename) {
		return ''
	}
	if (pluginFilename.substr(-3) === '.so') {
		return pluginFilename.substr(0, pluginFilename.length - 3)
	}
	return pluginFilename
}

function getStoredPluginFilename(pluginRef) {
	var pluginStem = getPluginStem(pluginRef)
	if (!pluginStem) {
		return ''
	}
	return pluginStem + '.so'
}

// enabledCalendarPlugins as another (broken) Plasma 6 port may have left it: one character per
// entry ("h", "o", "l", ...: the characters joined back are the comma-separated list it was), or
// several plugins in one entry ("holidaysevents.so,pimevents.so"). Returns the plugin filenames,
// one-letter names (never a plugin) left out; a sound list comes back the same.
function repairPluginList(pluginList) {
	var entries = toArray(pluginList).map(function(entry) {
		return String(entry)
	})
	var oneCharacterEach = entries.length > 1 && entries.every(function(entry) {
		return entry.length === 1
	})
	if (oneCharacterEach) {
		entries = [entries.join('')]
	}
	var split = []
	entries.forEach(function(entry) {
		entry.split(',').forEach(function(part) {
			part = part.trim()
			if (part) {
				split.push(part)
			}
		})
	})
	return pluginPathToFilenameList(split).filter(function(filename) {
		return getPluginStem(filename).length > 1
	})
}

function pluginPathToFilenameList(pluginPathList) {
	var pluginFilenameList = []
	pluginPathList = toArray(pluginPathList)
	for (var i = 0; i < pluginPathList.length; i++) {
		var pluginFilename = getStoredPluginFilename(pluginPathList[i])
		if (!pluginFilename) {
			continue
		}
		if (pluginFilenameList.indexOf(pluginFilename) == -1) {
			pluginFilenameList.push(pluginFilename)
		}
	}
	return pluginFilenameList
}

function getModelRoleValue(eventPluginsManager, rowIndex, roleName) {
	if (!eventPluginsManager || !eventPluginsManager.model || typeof eventPluginsManager.model.get !== 'function') {
		return ''
	}
	try {
		var roleValue = eventPluginsManager.model.get(rowIndex, roleName)
		if (typeof roleValue !== 'undefined' && roleValue !== null && roleValue !== '') {
			return roleValue
		}
	} catch (e) {
	}
	try {
		var rowData = eventPluginsManager.model.get(rowIndex)
		if (rowData && typeof rowData === 'object' && typeof rowData[roleName] !== 'undefined') {
			return rowData[roleName]
		}
	} catch (e) {
	}
	return ''
}

function getPluginRuntimeIdentifier(eventPluginsManager, pluginFilenameA) {
	if (!eventPluginsManager || !eventPluginsManager.model || typeof eventPluginsManager.model.rowCount !== 'function') {
		return null
	}
	var pluginStemA = getPluginStem(pluginFilenameA)
	var sawPluginPath = false
	for (var i = 0; i < eventPluginsManager.model.rowCount(); i++) {
		var pluginId = getModelRoleValue(eventPluginsManager, i, 'pluginId')
		if (pluginId && pluginStemA == getPluginStem(pluginId)) {
			return pluginId
		}

		var pluginPath = getModelRoleValue(eventPluginsManager, i, 'pluginPath')
		if (pluginPath) {
			sawPluginPath = true
		}
		if (!pluginPath) {
			continue
		}
		if (pluginStemA == getPluginStem(pluginPath)) {
			return pluginPath
		}
	}

	// Plasma 6 switched enabledPlugins to plugin ids instead of full paths.
	// Fall back to the plugin stem when the manager model does not expose pluginPath.
	if (pluginStemA && !sawPluginPath) {
		return pluginStemA
	}

	// Plugin not installed or the manager model has not populated yet.
	return null
}

function getModelPluginFilename(eventPluginsManager, rowIndex, rowData) {
	if (rowData) {
		if (rowData.pluginId) {
			return getStoredPluginFilename(rowData.pluginId)
		}
		if (rowData.pluginPath) {
			return getStoredPluginFilename(rowData.pluginPath)
		}
	}

	var pluginId = getModelRoleValue(eventPluginsManager, rowIndex, 'pluginId')
	if (pluginId) {
		return getStoredPluginFilename(pluginId)
	}

	return getStoredPluginFilename(getModelRoleValue(eventPluginsManager, rowIndex, 'pluginPath'))
}

function pluginFilenameToPathList(eventPluginsManager, pluginFilenameList) {
	var pluginPathList = []
	pluginFilenameList = toArray(pluginFilenameList)
	for (var i = 0; i < pluginFilenameList.length; i++) {
		var pluginFilename = pluginFilenameList[i]
		if (!pluginFilename) {
			continue
		}
		var pluginRuntimeIdentifier = getPluginRuntimeIdentifier(eventPluginsManager, pluginFilename)
		if (!pluginRuntimeIdentifier) {
			warnMissingPlugin(pluginFilename)
			continue
		}
		if (pluginPathList.indexOf(pluginRuntimeIdentifier) == -1) {
			pluginPathList.push(pluginRuntimeIdentifier)
		}
	}
	return pluginPathList
}

function normalizePluginFilenameList(pluginFilenameList) {
	var normalizedList = []
	pluginFilenameList = toArray(pluginFilenameList)
	for (var i = 0; i < pluginFilenameList.length; i++) {
		var pluginFilename = pluginFilenameList[i]
		if (!pluginFilename || typeof pluginFilename !== 'string') {
			continue
		}
		if (normalizedList.indexOf(pluginFilename) === -1) {
			normalizedList.push(pluginFilename)
		}
	}
	return normalizedList
}

function getDefaultPluginFilenameList(eventPluginsManager) {
	var defaultPluginFilenameList = []
	for (var i = 0; i < DEFAULT_PLUGIN_FILENAMES.length; i++) {
		var pluginFilename = DEFAULT_PLUGIN_FILENAMES[i]
		if (eventPluginsManager && !getPluginRuntimeIdentifier(eventPluginsManager, pluginFilename)) {
			continue
		}
		defaultPluginFilenameList.push(pluginFilename)
	}
	return defaultPluginFilenameList
}

function getEffectivePluginFilenameList(eventPluginsManager, pluginFilenameList, allowEmpty) {
	var normalizedPluginFilenameList = normalizePluginFilenameList(pluginFilenameList)
	if (normalizedPluginFilenameList.length > 0 || allowEmpty) {
		return normalizedPluginFilenameList
	}
	return getDefaultPluginFilenameList(eventPluginsManager)
}

function populateEnabledPluginsByFilename(eventPluginsManager, pluginFilenameList, allowEmpty) {
	var pluginPathList = pluginFilenameToPathList(
		eventPluginsManager,
		getEffectivePluginFilenameList(eventPluginsManager, pluginFilenameList, allowEmpty)
	)
	eventPluginsManager.populateEnabledPluginsList(pluginPathList)
}

// Only when the list changed: writing enabledPlugins unloads the plugins left out, and reloading
// them while they're in use crashed Plasma 6.3 (#14).
function setEnabledPluginsByFilename(eventPluginsManager, pluginFilenameList, allowEmpty) {
	var pluginPathList = pluginFilenameToPathList(
		eventPluginsManager,
		getEffectivePluginFilenameList(eventPluginsManager, pluginFilenameList, allowEmpty)
	)
	var current = toArray(eventPluginsManager.enabledPlugins).slice().sort()
	var next = pluginPathList.slice().sort()
	if (current.length === next.length && current.every(function(path, i) { return path === next[i] })) {
		return false
	}
	eventPluginsManager.enabledPlugins = pluginPathList
	return true
}

//--- Akonadi events by color
// The PIM events plugin tells an event's color, not its calendar: the color stands in for it.
// "#RRGGBB" or "#AARRGGBB" (a QColor as a string) -> "rrggbb", "" without one.
function colorKey(color) {
	var m = /^#(?:[0-9a-f]{2})?([0-9a-f]{6})$/i.exec(String(color || ""))
	return m ? m[1].toLowerCase() : ""
}

// The events' calendars named idPrefix + a color key, as [{ id, color, count }]. By color: each
// keeps its place as the events change.
function colorCalendars(events, idPrefix) {
	var byId = {}
	var list = []
	;(events || []).forEach(function(event) {
		var id = String(event.calendarId || "")
		if (id.indexOf(idPrefix) !== 0) {
			return
		}
		var group = byId[id]
		if (!group) {
			group = byId[id] = { id: id, color: "#" + id.substr(idPrefix.length), count: 0 }
			list.push(group)
		}
		group.count++
	})
	return list.sort(function(a, b) {
		return a.id < b.id ? -1 : a.id > b.id ? 1 : 0
	})
}

// Akonadi's database as its akonadiserverrc says: PostgreSQL (psql) or SQLite (python3, read
// only), nothing for MySQL. The script lines that run the queries ([{ sql, sqlite }]) in one run,
// rows as "column|column" lines. SQLite's file: the rc's [QSQLITE] Name (a QSettings value).
var AKONADI_SQLITE_READER = [
	'import pathlib, sqlite3, sys',
	'def unescape(text):',
	'	slash, out, i = chr(92), "", 0',
	'	while i < len(text):',
	'		if text[i] == slash and i + 1 < len(text):',
	'			if text[i + 1] == "x":',
	'				j = i + 2',
	'				while j < len(text) and j < i + 6 and text[j] in "0123456789abcdefABCDEF":',
	'					j += 1',
	'				out += chr(int(text[i + 2:j], 16)) if j > i + 2 else "x"',
	'				i = j',
	'				continue',
	'			out += {"n": chr(10), "t": chr(9)}.get(text[i + 1], text[i + 1])',
	'			i += 2',
	'			continue',
	'		out += text[i]',
	'		i += 1',
	'	return out',
	'section, name = "", ""',
	'for line in open(sys.argv[1], encoding="utf-8", errors="replace"):',
	'	line = line.strip()',
	'	if line.startswith("["):',
	'		section = line',
	'	elif section == "[QSQLITE]" and line.startswith("Name="):',
	'		name = line[5:].strip()',
	'		name = unescape(name[1:-1] if len(name) > 1 and name[0] == name[-1] == chr(34) else name)',
	'db = pathlib.Path(name or sys.argv[2] + "/akonadi/akonadi.db")',
	'if db.is_file():',
	'	c = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=3)',
	'	c.text_factory = lambda b: b.decode("utf-8", "replace")',
	'	for sql in sys.argv[3:]:',
	'		try:',
	'			for r in c.execute(sql):',
	'				print("|".join("" if v is None else v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v) for v in r))',
	'		except sqlite3.Error:',
	'			pass',
]
function akonadiDatabaseLines(queries) {
	var quote = function(sql) { return '"' + sql + '"' } // The SELECTs have no double quotes, $ or backticks
	return [
		'rc="${XDG_CONFIG_HOME:-$HOME/.config}/akonadi/akonadiserverrc"',
		'if [ -f "$rc" ] && command -v psql >/dev/null && grep -q "^Driver=QPSQL" "$rc"; then',
		// Host (the socket directory) and the database name of the [QPSQL] section.
		'host=$(awk -F= \'/^\\[/{s=($0=="[QPSQL]")} s&&$1=="Host"{print substr($0,6);exit}\' "$rc")',
		'name=$(awk -F= \'/^\\[/{s=($0=="[QPSQL]")} s&&$1=="Name"{print substr($0,6);exit}\' "$rc")',
		'timeout 5 psql ${host:+-h "$host"} -d "${name:-akonadi}" -At '
			+ queries.map(function(q) { return '-c ' + quote(q.sql) }).join(' ') + ' 2>/dev/null',
		'elif [ -f "$rc" ] && command -v python3 >/dev/null && grep -q "^Driver=QSQLITE" "$rc"; then',
		'timeout 5 python3 -I -c \'' + AKONADI_SQLITE_READER.join('\n') + '\' "$rc" "${XDG_DATA_HOME:-$HOME/.local/share}" '
			+ queries.map(function(q) { return quote(q.sqlite || q.sql) }).join(' ') + ' 2>/dev/null',
		'fi',
	]
}

// What akonadiCalendarsCommand reads from the database:
// - the calendars' colors (the collectioncolor attribute, only there; without it the colors and
//   names come from the events): lines "collection id|#aarrggbb";
// - the collections of Akonadi's Google resources: "collection|id|resource|remote id" (a calendar's
//   remote id is its Google calendar id);
// - those of its DAV resources (eg: Nextcloud): "davcollection|id|remote id" (a calendar's URL).
var AKONADI_QUERIES = [
	{
		sql: "SELECT collectionid, convert_from(value, 'UTF8') FROM collectionattributetable WHERE convert_from(type, 'UTF8') = 'collectioncolor'",
		sqlite: "SELECT collectionid, CAST(value AS TEXT) FROM collectionattributetable WHERE CAST(type AS TEXT) = 'collectioncolor'",
	},
	{ sql: "SELECT 'collection', c.id, r.name, c.remoteid FROM collectiontable c JOIN resourcetable r ON r.id = c.resourceid WHERE r.name LIKE 'akonadi_google_resource_%'" },
	{ sql: "SELECT 'davcollection', c.id, c.remoteid FROM collectiontable c JOIN resourcetable r ON r.id = c.resourceid WHERE r.name LIKE 'akonadi_davgroupware_resource_%'" },
]

// The accounts of Akonadi's Google resources, from their settings: lines "account|resource|email".
function akonadiGoogleAccountsLines() {
	return [
		'for f in "${XDG_CONFIG_HOME:-$HOME/.config}"/akonadi_google_resource_*rc; do',
		'r=${f##*/}; [ -f "$f" ] && sed -n "s/^Account=/account|${r%rc}|/p" "$f"',
		'done',
	]
}

// All of it in one output (parseAkonadiColors, parseAkonadiCollections).
function akonadiCalendarsCommand() {
	return ['sh', '-c', akonadiGoogleAccountsLines().concat(akonadiDatabaseLines(AKONADI_QUERIES), ['exit 0']).join('\n')]
}
// The widget's Google accounts (GoogleAccountsStore.accounts) for akonadiMirroredCollections:
// { "email": ["calendar id", ...] }, the email being the primary calendar's id, in lower case. Only
// the calendars shown in the widget (calendarIdList, "primary" as the primary's id): Akonadi's copy
// of one left out here stays (its events come from nowhere else).
function googleCalendarIdsByAccount(accounts) {
	var calendarIds = {}
	;(accounts || []).forEach(function(account) {
		var list = account && account.calendarList ? Array.prototype.slice.call(account.calendarList) : []
		var primary = list.filter(function(calendar) { return calendar && calendar.primary })[0]
		if (primary && primary.id) {
			var shown = account.calendarIdList ? Array.prototype.slice.call(account.calendarIdList) : []
			calendarIds[String(primary.id).toLowerCase()] = shown.map(function(id) {
				return id === 'primary' ? primary.id : id
			})
		}
	})
	return calendarIds
}
// The collection ids of the calendars Akonadi syncs that the widget also reads: { "collection id": true }.
// calendarIds: { "email": ["calendar id", ...] } (lower case emails), caldavUrls: [url].
function akonadiMirroredCollections(stdout, calendarIds, caldavUrls) {
	var parsed = parseAkonadiCollections(stdout)
	var mirrored = akonadiDavCopies(stdout, caldavUrls)
	parsed.collections.forEach(function(collection) {
		var ids = (calendarIds || {})[parsed.accounts[collection.resource]]
		if (ids && ids.indexOf(collection.remoteId) >= 0) {
			mirrored[collection.id] = true
		}
	})
	return mirrored
}
// Akonadi's DAV collections at the URL of one of caldavUrls: { "collection id": true }.
function akonadiDavCopies(stdout, caldavUrls) {
	var urls = Array.prototype.slice.call(caldavUrls || []).map(davUrlKey)
	var copies = {}
	parseAkonadiCollections(stdout).davCollections.forEach(function(collection) {
		if (collection.remoteId && urls.indexOf(davUrlKey(collection.remoteId)) >= 0) {
			copies[collection.id] = true
		}
	})
	return copies
}
// A calendar URL to compare: scheme and host in lower case, no user name, no default port, the
// path decoded (one side may keep the server's %-encoding) and without a trailing slash.
function davUrlKey(url) {
	var m = /^([a-z][a-z0-9+.-]*):\/\/([^/?#]*)([^?#]*)/i.exec(String(url || '').trim())
	if (!m) {
		return String(url || '').trim()
	}
	var scheme = m[1].toLowerCase()
	var host = m[2].replace(/^.*@/, '').toLowerCase().replace(scheme === 'https' ? /:443$/ : /:80$/, '')
	var path = m[3].split('/').map(function(segment) {
		try {
			return decodeURIComponent(segment)
		} catch (e) {
			return segment
		}
	}).join('/')
	return scheme + '://' + host + path.replace(/\/+$/, '')
}
// The URLs of the selected calendars in caldavAccounts (for the settings; the widget uses
// CalDAVManager.readUrls). The calendars CalDAVManager.getCalendarList reads: not task lists
// (events: false, PR #51's lists only for the Tasks tab, whatever their tasksSelected).
function caldavCalendarUrls(accounts) {
	var urls = []
	Array.prototype.slice.call(accounts || []).forEach(function(account) {
		Array.prototype.slice.call(account && account.calendars || []).forEach(function(calendar) {
			if (calendar && calendar.href && calendar.selected !== false && calendar.events !== false) {
				urls.push(String(calendar.href))
			}
		})
	})
	return urls
}
// akonadiCalendarsCommand's output: { accounts: { resource: "email" }, collections (Google's):
// [{ id, resource, remoteId }], davCollections: [{ id, remoteId }] }, the emails in lower case.
function parseAkonadiCollections(stdout) {
	var accounts = {}
	var collections = []
	var davCollections = []
	String(stdout || '').split('\n').forEach(function(line) {
		var fields = line.trim().split('|')
		if (fields[0] === 'account' && fields.length >= 3) {
			accounts[fields[1]] = fields.slice(2).join('|').trim().toLowerCase()
		} else if (fields[0] === 'collection' && fields.length >= 4) {
			collections.push({ id: fields[1], resource: fields[2], remoteId: fields.slice(3).join('|') })
		} else if (fields[0] === 'davcollection' && fields.length >= 3) {
			davCollections.push({ id: fields[1], remoteId: fields.slice(2).join('|') })
		}
	})
	return { accounts: accounts, collections: collections, davCollections: davCollections }
}
// The Akonadi calendars (pimCalendarRows with the accounts) under their accounts, apart: of
// Akonadi's Google accounts (by akonadiCalendarsCommand's output: none without it) and the
// others, in that order: [{ name, google, calendars: [row] }], a Google account named by its email; the calendars without an account (a resource of its own, eg: a local
// calendar file) in one group without a name, the last. Accounts without a calendar are left out.
// From a tree, by a calendar's accountId; from a flat list (older models), each account before its
// calendars.
function pimCalendarGroups(rows, stdout) {
	var parsed = parseAkonadiCollections(stdout)
	var googleAccountOf = {}
	parsed.collections.forEach(function(collection) {
		googleAccountOf[collection.id] = parsed.accounts[collection.resource] || ''
	})
	var google = [], other = []
	var groupOf = {} // Account id -> its group
	var ownGroup = { name: '', calendars: [] } // Calendars without an account
	var tree = (rows || []).some(function(row) { return !!row.accountId })
	var group = null // A flat list's last account
	;(rows || []).forEach(function(row) {
		if (row.account) {
			var isGoogle = row.id in googleAccountOf
			group = groupOf[row.id] = { name: (isGoogle && googleAccountOf[row.id]) || row.name, google: isGoogle, calendars: [] }
			;(isGoogle ? google : other).push(group)
		} else {
			var into = tree ? groupOf[row.accountId] : group
			;(into || ownGroup).calendars.push(row)
		}
	})
	ownGroup.google = false
	return google.concat(other, [ownGroup]).filter(function(g) { return g.calendars.length > 0 })
}
// The command's output as { "collection id": "#rrggbb" }.
function parseAkonadiColors(stdout) {
	var colors = {}
	String(stdout || '').split('\n').forEach(function(line) {
		var m = /^(\d+)\|(#[0-9a-f]{6,8})\s*$/i.exec(line)
		if (m) {
			colors[m[1]] = '#' + colorKey(m[2])
		}
	})
	return colors
}
// The color keys that only mirrored calendars (akonadiMirroredCollections) among the shown ones
// (pimCalendarRows, checked) have: { key: true }. Their events are the signed-in account's.
function mirroredColorKeys(rows, colors, mirrored) {
	var mirroredKeys = {}
	var otherKeys = {}
	;(rows || []).forEach(function(row) {
		var key = row.checked ? colorKey((colors || {})[row.id]) : ''
		if (key) {
			if ((mirrored || {})[row.id]) {
				mirroredKeys[key] = true
			} else {
				otherKeys[key] = true
			}
		}
	})
	Object.keys(otherKeys).forEach(function(key) {
		delete mirroredKeys[key]
	})
	return mirroredKeys
}

// Color key -> the names of the shown calendars (pimCalendarRows, checked) in that color; without
// one, of the others in it. A model keeps the selection it was made with (a calendar selected
// later, in the widget's settings or the Digital Clock's, isn't checked in it), while the plugin
// shows its events.
function namesByColor(rows, colors) {
	var shown = {}
	var others = {}
	;(rows || []).forEach(function(row) {
		var key = colorKey((colors || {})[row.id])
		if (key) {
			var names = row.checked ? shown : others
			names[key] = (names[key] || []).concat(row.name)
		}
	})
	Object.keys(others).forEach(function(key) {
		if (!shown[key]) {
			shown[key] = others[key]
		}
	})
	return shown
}
