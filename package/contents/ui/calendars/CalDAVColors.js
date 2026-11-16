.pragma library

// Event colors on CalDAV calendars: COLOR (RFC 7986), a CSS color name, as Nextcloud writes it.
// Google's 11 event colors are saved as their nearest CSS name and read back as that colorId.

var cssByColorId = {
	'1': 'cornflowerblue', // Lavender
	'2': 'mediumseagreen', // Sage
	'3': 'darkorchid', // Grape
	'4': 'lightcoral', // Flamingo
	'5': 'goldenrod', // Banana
	'6': 'orangered', // Tangerine
	'7': 'deepskyblue', // Peacock
	'8': 'dimgray', // Graphite
	'9': 'royalblue', // Blueberry
	'10': 'seagreen', // Basil
	'11': 'red', // Tomato
}

// The CSS name to save for a colorId, null for none (the calendar's color).
function cssName(colorId) {
	return cssByColorId[String(colorId || '')] || null
}

// The colorId of a saved CSS name, '' when it isn't one of them.
function colorIdOf(name) {
	var css = String(name || '').toLowerCase().replace('grey', 'gray')
	for (var id in cssByColorId) {
		if (cssByColorId[id] === css) {
			return id
		}
	}
	return ''
}
