// Akonadi calendars of a PimCalendarsModel (package/contents/ui/calendars/PlasmaCalendarUtils.js).
const assert = require('assert')
const fs = require('fs')
const path = require('path')
const vm = require('vm')

const U = {}
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../package/contents/ui/calendars/PlasmaCalendarUtils.js'), 'utf8')
	.replace(/^\.pragma library\s*$/m, ''), U)

const UserRole = 256, DisplayRole = 0
// A model of rows (optionally with children), each a map of role -> value.
function model(rows) {
	return {
		rowCount: parent => (parent ? parent.row.children || [] : rows).length,
		index: (row, column, parent) => ({ row: (parent ? parent.row.children : rows)[row] }),
		hasChildren: i => !!(i.row.children && i.row.children.length),
		data: (i, role) => i.row[role],
	}
}

// Current kdepim-addons: flat, with roles; accounts have enabled = false.
const flat = model([
	{ [UserRole + 1]: 1490, [UserRole + 2]: 'me@example.com', [UserRole + 3]: false, [UserRole + 4]: false },
	{ [UserRole + 1]: 1506, [UserRole + 2]: 'me@example.com', [UserRole + 3]: true, [UserRole + 4]: true },
	{ [UserRole + 1]: 1505, [UserRole + 2]: 'Support', [UserRole + 3]: true, [UserRole + 4]: false },
])
assert.deepStrictEqual(JSON.parse(JSON.stringify(U.pimCalendarRows(flat, UserRole, DisplayRole))), [
	{ id: '1506', name: 'me@example.com', checked: true },
	{ id: '1505', name: 'Support', checked: false },
])

// Older versions: a tree with a { id, enabled } object.
const tree = model([
	{ [DisplayRole]: 'Personal', [UserRole + 1]: { id: 7, enabled: true }, children: [
		{ [DisplayRole]: 'Birthdays', [UserRole + 1]: { id: 8, enabled: false } },
	] },
])
assert.deepStrictEqual(JSON.parse(JSON.stringify(U.pimCalendarRows(tree, UserRole, DisplayRole))), [
	{ id: '7', name: 'Personal', checked: true },
	{ id: '8', name: 'Birthdays', checked: false },
])
// Current versions are a tree under the accounts: calendars in folders of an account too.
const nested = model([
	{ [UserRole + 1]: 1, [UserRole + 2]: 'Work (DAV)', [UserRole + 3]: false, [UserRole + 4]: false, children: [
		{ [UserRole + 1]: 2, [UserRole + 2]: 'Team', [UserRole + 3]: false, [UserRole + 4]: false, children: [
			{ [UserRole + 1]: 3, [UserRole + 2]: 'Releases', [UserRole + 3]: true, [UserRole + 4]: true },
		] },
	] },
])
assert.deepStrictEqual(JSON.parse(JSON.stringify(U.pimCalendarRows(nested, UserRole, DisplayRole))), [
	{ id: '3', name: 'Releases', checked: true },
], 'a calendar two folders down')
assert.deepStrictEqual(Array.from(U.pimCalendarRows(null, UserRole, DisplayRole)), [])
assert.deepStrictEqual(JSON.parse(JSON.stringify(U.pimCalendarRows(flat, UserRole, DisplayRole, true))), [
	{ id: '1490', name: 'me@example.com', checked: false, account: true },
	{ id: '1506', name: 'me@example.com', checked: true },
	{ id: '1505', name: 'Support', checked: false },
], 'with the accounts')

// The plugin's saved selection: ids Akonadi no longer has (an account set up anew).
assert.deepStrictEqual(Array.from(U.parsePimSelection('1386,1486,1501,1486\n')), ['1386', '1486', '1501'])
assert.deepStrictEqual(Array.from(U.parsePimSelection('')), [], 'no selection saved')
const allRows = U.pimCalendarRows(flat, UserRole, DisplayRole, true)
assert.deepStrictEqual(Array.from(U.stalePimSelection(['1490', '1506', '1501', '1386'], allRows)), ['1501', '1386'],
	'an account id saved is not missing')
assert.deepStrictEqual(Array.from(U.stalePimSelection(['1501'], [])), [], 'none before Akonadi is read')

// enabledCalendarPlugins left one character per entry by another port: joined back.
const repair = list => Array.from(U.repairPluginList(list))
assert.deepStrictEqual(repair('holidaysevents.so,pimevents.so'.split('')), ['holidaysevents.so', 'pimevents.so'], 'one character each')
assert.deepStrictEqual(repair(['holidaysevents.so,pimevents.so']), ['holidaysevents.so', 'pimevents.so'], 'several in one entry')
assert.deepStrictEqual(repair(['holidaysevents.so', 'pimevents.so']), ['holidaysevents.so', 'pimevents.so'], 'a sound list as it is')
assert.deepStrictEqual(repair(['h', 'pimevents.so']), ['pimevents.so'], 'a stray letter left out')
assert.deepStrictEqual(repair(['x']), [], 'nothing usable')
assert.deepStrictEqual(repair([]), [])

//--- Akonadi events by color
assert.strictEqual(U.colorKey('#9A9CFF'), '9a9cff')
assert.strictEqual(U.colorKey('#ff9a9cff'), '9a9cff', 'with alpha')
assert.strictEqual(U.colorKey(''), '')
assert.strictEqual(U.colorKey('red'), '')
const prefix = 'plasma_EventsColor_'
const ev = (color, summary) => ({ calendarId: prefix + color, summary })
const groups = JSON.parse(JSON.stringify(U.colorCalendars([
	ev('aaaaaa', 'Standup'), ev('bbbbbb', 'Vacation'), ev('bbbbbb', 'Physio'), ev('bbbbbb', 'Vacation'),
	ev('bbbbbb', 'Dentist'), ev('bbbbbb', 'Review'), { calendarId: 'plasma_Holidays', summary: 'Easter' },
], prefix)))
assert.deepStrictEqual(groups, [
	{ id: prefix + 'aaaaaa', color: '#aaaaaa', count: 1 },
	{ id: prefix + 'bbbbbb', color: '#bbbbbb', count: 5 },
], 'by color, other calendars left out')

// Named after the calendars of a color: the shown ones, else the others in it (a model made before
// a calendar was selected in the Digital Clock's settings has it unchecked).
const named = U.namesByColor([
	{ id: '1', name: 'Work', checked: true },
	{ id: '2', name: 'Team', checked: false },
	{ id: '3', name: 'Support', checked: false },
	{ id: '4', name: 'Holidays', checked: true },
	{ id: '5', name: 'No color', checked: true },
], { 1: '#ffaaaaaa', 2: '#aaaaaa', 3: '#ffbbbbbb', 4: '#cccccc' })
assert.deepStrictEqual(JSON.parse(JSON.stringify(named)), { aaaaaa: ['Work'], bbbbbb: ['Support'], cccccc: ['Holidays'] },
	'a shown calendar first; not shown ones only for a color without one')
assert.deepStrictEqual(JSON.parse(JSON.stringify(U.namesByColor([{ id: '1', name: 'Work', checked: true }], undefined))), {},
	'no colors read')

//--- Akonadi's Google calendars of an account also signed in to in the widget
const mirrored = U.akonadiMirroredCollections([
	'account|akonadi_google_resource_14|Me@Gmail.com',
	'account|akonadi_google_resource_15|work@example.com',
	'collection|1466|akonadi_google_resource_14|RootCollection',
	'collection|1485|akonadi_google_resource_14|de.german#holiday@group.v.calendar.google.com',
	'collection|1486|akonadi_google_resource_14|me@gmail.com',
	'collection|1489|akonadi_google_resource_14|other@group.calendar.google.com',
	'collection|1495|akonadi_google_resource_15|de.german#holiday@group.v.calendar.google.com',
	'collection|1499|akonadi_google_resource_99|me@gmail.com',
	'',
].join('\n'), { 'me@gmail.com': ['me@gmail.com', 'de.german#holiday@group.v.calendar.google.com'] })
assert.deepStrictEqual(JSON.parse(JSON.stringify(mirrored)), { 1485: true, 1486: true },
	"that account's calendars in the widget: not those it lacks, not another account's, not a resource without settings")
assert.deepStrictEqual(JSON.parse(JSON.stringify(U.akonadiMirroredCollections('', { 'me@gmail.com': ['me@gmail.com'] }))), {}, 'no database: none')

const rows = [
	{ id: '1486', name: 'me@gmail.com', checked: true },
	{ id: '1485', name: 'Holidays', checked: true },
	{ id: '20', name: 'Work', checked: true },
	{ id: '21', name: 'Off', checked: false },
]
const colors = { 1486: '#aaaaaa', 1485: '#bbbbbb', 20: '#bbbbbb', 21: '#cccccc' }
assert.deepStrictEqual(JSON.parse(JSON.stringify(U.mirroredColorKeys(rows, colors, { 1486: true, 1485: true, 21: true }))), { aaaaaa: true },
	'a color also of another shown calendar stays; not shown calendars left out')
assert.deepStrictEqual(JSON.parse(JSON.stringify(U.googleCalendarIdsByAccount([
	{ calendarList: [{ id: 'x@group.calendar.google.com' }, { id: 'y@group.calendar.google.com' }, { id: 'Me@Gmail.com', primary: true }],
		calendarIdList: ['x@group.calendar.google.com', 'primary'] },
	{ calendarList: [] },
	null,
]))), { 'me@gmail.com': ['x@group.calendar.google.com', 'Me@Gmail.com'] },
	'only the calendars shown in the widget ("primary" as its id): an Akonadi copy of another one stays')

//--- The Akonadi calendars under their accounts, Google's apart (the Google and Akonadi tabs)
const dbLines = [
	'account|akonadi_google_resource_14|Me@Gmail.com',
	'collection|1466|akonadi_google_resource_14|RootCollection',
	'collection|1486|akonadi_google_resource_14|me@gmail.com',
	'collection|1488|akonadi_google_resource_14|e_2_de#weeknum@group.v.calendar.google.com',
].join('\n')
const groupRows = [
	{ id: '1466', name: 'Google Groupware', checked: false, account: true },
	{ id: '1486', name: 'me@gmail.com', checked: true },
	{ id: '1488', name: 'Week numbers', checked: false },
	{ id: '10', name: 'Personal calendar', checked: false, account: true },
	{ id: '11', name: 'Personal', checked: true },
	{ id: '20', name: 'Birthdays & Anniversaries', checked: false, account: true },
]
const g = JSON.parse(JSON.stringify(U.pimCalendarGroups(groupRows, dbLines)))
assert.deepStrictEqual(g.filter(x => x.google).map(x => [x.name, x.calendars.map(c => c.id)]), [['me@gmail.com', ['1486', '1488']]],
	'a Google account by its email, with its calendars')
assert.deepStrictEqual(g.filter(x => !x.google).map(x => [x.name, x.calendars.map(c => c.id)]), [['Personal calendar', ['11']]],
	'the others under their accounts; an account without calendars left out')
const noDb = JSON.parse(JSON.stringify(U.pimCalendarGroups(groupRows, '')))
assert.deepStrictEqual([noDb.filter(x => x.google).length, noDb.filter(x => !x.google).map(x => x.name)], [0, ['Google Groupware', 'Personal calendar']],
	'without Akonadi\'s database all are others')
assert.deepStrictEqual(JSON.parse(JSON.stringify(U.pimCalendarGroups([{ id: '7', name: 'Old', checked: true }], ''))).filter(x => !x.google),
	[{ name: '', calendars: [{ id: '7', name: 'Old', checked: true }], google: false }], 'calendars without an account (older models)')

// From the model's tree: a calendar that is a resource of its own (a local calendar file) after a
// Google account isn't that account's: in its own group, the last.
const accountTree = model([
	{ [UserRole + 1]: 1490, [UserRole + 2]: 'Google Groupware', [UserRole + 3]: false, [UserRole + 4]: false, children: [
		{ [UserRole + 1]: 1617, [UserRole + 2]: 'work@example.com', [UserRole + 3]: true, [UserRole + 4]: true },
	] },
	{ [UserRole + 1]: 5, [UserRole + 2]: 'Personal calendar', [UserRole + 3]: true, [UserRole + 4]: false, [UserRole + 5]: 'office-calendar' },
	{ [UserRole + 1]: 20, [UserRole + 2]: 'Work (DAV)', [UserRole + 3]: false, [UserRole + 4]: false, children: [
		{ [UserRole + 1]: 21, [UserRole + 2]: 'Team', [UserRole + 3]: false, [UserRole + 4]: false, children: [
			{ [UserRole + 1]: 22, [UserRole + 2]: 'Releases', [UserRole + 3]: true, [UserRole + 4]: true },
		] },
	] },
])
const treeRows = U.pimCalendarRows(accountTree, UserRole, DisplayRole, true)
assert.deepStrictEqual(JSON.parse(JSON.stringify(treeRows)).filter(r => !r.account).map(r => [r.id, r.accountId || '']),
	[['1617', '1490'], ['5', ''], ['22', '20']], 'each calendar with its top account from the tree; in a folder too')
assert.strictEqual(treeRows.filter(r => r.id === '5')[0].icon, 'office-calendar', 'the model\'s icon (eg: a task list\'s)')
const treeGroups = JSON.parse(JSON.stringify(U.pimCalendarGroups(treeRows,
	'account|akonadi_google_resource_15|work@example.com\ncollection|1490|akonadi_google_resource_15|RootCollection\ncollection|1617|akonadi_google_resource_15|work@example.com')))
assert.deepStrictEqual(treeGroups.filter(x => x.google).map(x => [x.name, x.calendars.map(c => c.id)]), [['work@example.com', ['1617']]])
assert.deepStrictEqual(treeGroups.filter(x => !x.google).map(x => [x.name, x.calendars.map(c => c.id)]), [['Work (DAV)', ['22']], ['', ['5']]],
	'the local calendar in its own group, the last; not under the Google account')
assert.deepStrictEqual(JSON.parse(JSON.stringify(U.pimCalendarRows(accountTree, UserRole, DisplayRole))).map(r => Object.keys(r).join()),
	['id,name,checked', 'id,name,checked', 'id,name,checked'], 'without the accounts: rows as before')

console.log('PASS plasma_calendar_utils')

// enabledPlugins written only when the list changes: writing it unloads plugins, and reloading
// them while in use crashed Plasma 6.3 (#14).
{
	const savedToPaths = U.pluginFilenameToPathList, savedEffective = U.getEffectivePluginFilenameList
	U.pluginFilenameToPathList = (m, list) => list.map(name => '/plugins/' + name)
	U.getEffectivePluginFilenameList = (m, list) => list
	let writes = 0
	const manager = {
		_list: ['/plugins/holidays', '/plugins/pimevents'],
		get enabledPlugins() { return this._list },
		set enabledPlugins(list) { writes++; this._list = list },
	}
	assert.strictEqual(U.setEnabledPluginsByFilename(manager, ['pimevents', 'holidays'], false), false, 'same plugins, another order')
	assert.strictEqual(writes, 0)
	assert.strictEqual(U.setEnabledPluginsByFilename(manager, ['holidays'], false), true, 'one less')
	assert.strictEqual(writes, 1)
	U.pluginFilenameToPathList = savedToPaths
	U.getEffectivePluginFilenameList = savedEffective
}

//--- Akonadi's DAV copies of the widget's CalDAV calendars
{
	const stdout = [
		'davcollection|9|akonadi_davgroupware_resource_1',
		'davcollection|10|https://Cloud.Example.com:443/remote.php/dav/calendars/me/personal/',
		'davcollection|11|https://cloud.example.com/remote.php/dav/calendars/me/work/',
		'davcollection|12|https://cloud.example.com/remote.php/dav/calendars/me/Personal/',
	].join('\n')
	const urls = U.caldavCalendarUrls([
		{ id: 'a', calendars: [
			{ href: 'https://cloud.example.com/remote.php/dav/calendars/me/personal', selected: true },
			{ href: 'https://cloud.example.com/remote.php/dav/calendars/me/work/', selected: false },
			{ href: 'https://cloud.example.com/remote.php/dav/calendars/me/tasks/', events: false, tasksSelected: true },
		] },
		{ id: 'b' },
	])
	assert.deepStrictEqual(JSON.parse(JSON.stringify(urls)), ['https://cloud.example.com/remote.php/dav/calendars/me/personal'], 'selected ones only, not task lists')
	assert.deepStrictEqual(JSON.parse(JSON.stringify(U.akonadiMirroredCollections(stdout, {}, urls))), { 10: true },
		'the same calendar (host case, default port, trailing slash); not an unselected one, not another path')
	assert.deepStrictEqual(JSON.parse(JSON.stringify(U.akonadiMirroredCollections(stdout, {}, undefined))), {})
	assert.strictEqual(U.davUrlKey('HTTP://Host:80/a/'), 'http://host/a')
	assert.strictEqual(U.davUrlKey('https://host:8443/a//'), 'https://host:8443/a')
}

//--- Akonadi's database on SQLite: read with python3 (read only), as on PostgreSQL with psql
{
	const os = require('os')
	const { execFileSync } = require('child_process')
	const home = fs.mkdtempSync(path.join(os.tmpdir(), 'ecng-akonadi-'))
	try {
		// A folder with ü and a space: QSettings writes it quoted, with \xNNNN escapes.
		const dir = path.join(home, 'jürgen data')
		fs.mkdirSync(dir)
		const db = path.join(dir, 'akonadi.db')
		const rc = path.join(home, 'config/akonadi/akonadiserverrc')
		fs.mkdirSync(path.dirname(rc), { recursive: true })
		const escaped = db.replace('ü', '\\xfc')
		fs.writeFileSync(rc, '[%General]\nDriver=QSQLITE\n\n[QSQLITE]\nName="' + escaped + '"\n')
		execFileSync('python3', ['-c', [
			'import sqlite3, sys',
			'c = sqlite3.connect(sys.argv[1])',
			'c.execute("CREATE TABLE resourcetable (id INTEGER, name TEXT)")',
			'c.execute("CREATE TABLE collectiontable (id INTEGER, resourceid INTEGER, remoteid TEXT)")',
			'c.execute("CREATE TABLE collectionattributetable (collectionid INTEGER, type BLOB, value BLOB)")',
			'c.executemany("INSERT INTO resourcetable VALUES (?, ?)", [(1, "akonadi_davgroupware_resource_1"), (2, "akonadi_ical_resource_0")])',
			// A remote id with bytes that aren't UTF-8 first: the rows after it still come.
			'c.execute("INSERT INTO collectiontable VALUES (9, 1, CAST(X\'FF41\' AS TEXT))")',
			'c.executemany("INSERT INTO collectiontable VALUES (?, ?, ?)", [(10, 1, "https://h/cal/a/"), (20, 2, "/home/x.ics")])',
			'c.executemany("INSERT INTO collectionattributetable VALUES (?, ?, ?)", [(10, b"collectioncolor", b"#ff3c6dff"), (10, b"ctag", b"x")])',
			'c.commit()',
		].join('\n'), db])
		const cmd = U.akonadiCalendarsCommand()
		const run = () => execFileSync(cmd[0], cmd.slice(1), { env: Object.assign({}, process.env, { XDG_CONFIG_HOME: path.join(home, 'config') }) }).toString()
		const out = run()
		assert.deepStrictEqual(out.trim().split('\n'), ['10|#ff3c6dff', 'davcollection|9|�A', 'davcollection|10|https://h/cal/a/'])
		assert.deepStrictEqual(JSON.parse(JSON.stringify(U.parseAkonadiColors(out))), { 10: '#3c6dff' })
		assert.deepStrictEqual(JSON.parse(JSON.stringify(U.akonadiMirroredCollections(out, {}, ['https://h/cal/a']))), { 10: true })
		assert.deepStrictEqual(Object.keys(U.akonadiDavUrlKeys(out)).sort(), ['https://h/cal/a', '�A'])
		// One python3 run for the three queries.
		assert.strictEqual((cmd[2].match(/python3 -I/g) || []).length, 1)
		assert.strictEqual((cmd[2].match(/timeout 5 psql/g) || []).length, 1)
		// No Driver line (older Akonadi: MySQL), MySQL, or no rc at all: nothing (and no error).
		for (const text of ['[QSQLITE]\nName=' + db + '\n', '[%General]\nDriver=QMYSQL\n']) {
			fs.writeFileSync(rc, text)
			assert.strictEqual(run(), '')
		}
		fs.rmSync(rc)
		assert.strictEqual(run(), '')
	} finally {
		fs.rmSync(home, { recursive: true, force: true })
	}
}

// URL keys: the path's %-encoding and a user name don't matter.
assert.strictEqual(U.davUrlKey('https://me%40x.de@Cloud.example.com/dav/calendars/me%40x.de/priv%C3%A9/'),
	U.davUrlKey('https://cloud.example.com/dav/calendars/me@x.de/privé'))
assert.strictEqual(U.davUrlKey('https://h/a%zz/'), 'https://h/a%zz', 'a broken escape stays')
