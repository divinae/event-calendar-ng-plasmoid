// CalDAV event colors (package/contents/ui/calendars/CalDAVColors.js).
const assert = require('assert')
const fs = require('fs')
const path = require('path')
const vm = require('vm')

const read = file => fs.readFileSync(path.join(__dirname, '../package/contents/ui', file), 'utf8')
	.replace(/^\.pragma library\s*$/m, '')
const C = {}
vm.runInNewContext(read('calendars/CalDAVColors.js'), C)
const P = {}
vm.runInNewContext(read('code/GooglePalette.js'), P)

// Each of Google's event colors has its own CSS name, read back as the same colorId.
const names = P.eventPalette.map(entry => C.cssName(entry.id))
assert.strictEqual(names.length, 11)
assert.strictEqual(new Set(names).size, 11, 'all different')
P.eventPalette.forEach(entry => assert.strictEqual(C.colorIdOf(C.cssName(entry.id)), entry.id))
assert.ok(names.every(name => /^[a-z]{3,30}$/.test(name)), 'as caldav.py accepts them')
assert.strictEqual(C.cssName(''), null, 'the calendar color: none')
assert.strictEqual(C.cssName(undefined), null)
assert.strictEqual(C.colorIdOf('DimGrey'), '8', 'case and grey')
assert.strictEqual(C.colorIdOf('teal'), '', 'another name: no colorId')
assert.strictEqual(C.colorIdOf(undefined), '')

console.log('caldav colors: ok')
