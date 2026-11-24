import QtQuick

import "../ScriptPath.js" as ScriptPath
import "../lib"
import "CalDAVErrors.js" as CalDAVErrors

// scripts/caldav.py for the CalDAV managers (events: CalDAVManager, tasks: CalDAVTasksManager): its
// command line with an account (the password stays in the keyring, the helper reads it there), its
// errors as messages for the user, and the quiet fetch again while the keyring isn't up at login.
Item {
	id: caldavHelper

	property string helperPath: ScriptPath.scriptPath("caldav.py")
	property var helperLogger: null // The manager's Logger
	property string logName: "caldav" // Its log lines: "<logName>.helperError"

	// Time to fetch again: an earlier fetch couldn't read the keyring (see retryKeyring).
	signal keyringRetry()

	ExecUtil { id: executable }

	// The helper's error (CalDAVErrors.parse) as a message for the user. name: the calendar's or the
	// account's, for a refused user name or password.
	function errorFor(stderr, name) {
		var parsed = CalDAVErrors.parse(stderr)
		var kind = parsed.kind
		if (kind === "AUTH") {
			return { kind: kind, message: i18n("CalDAV: %1 didn't accept the user name or password", name) }
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
		return { kind: kind, message: i18n("CalDAV: %1", parsed.text || i18n("Unknown error")) }
	}

	// run(args, accountId, username, name, callback(err, data)): caldav.py with the account. err is
	// { kind, message } (errorFor), data the parsed JSON answer.
	function run(args, accountId, username, name, callback) {
		var argv = ['python3', helperPath].concat(args).concat(['--account', String(accountId), '--user', String(username || '')])
		executable.execArgv(argv, function(cmd, exitCode, exitStatus, stdout, stderr) {
			if (exitCode) {
				if (helperLogger) {
					helperLogger.log(logName + '.helperError', args[0], exitCode) // No event or task data or URLs in logs
				}
				callback(errorFor(stderr, name), null)
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

	// The keyring couldn't be read (KEYRING): at login the Secret Service or KWallet may answer only
	// a little after the widget's first fetch. Fetched again a few times (keyringRetry) before it's
	// an error: true when err is such a case and a fetch again is planned (nothing to report yet).
	function retryKeyring(err) {
		if (err && err.kind === "KEYRING" && keyringTimer.attempts < keyringTimer.maxAttempts) {
			keyringTimer.start()
			return true
		}
		return false
	}
	// A fetch worked: the keyring is up, a later failure gets its retries again.
	function keyringRead() {
		keyringTimer.attempts = 0
	}

	Timer {
		id: keyringTimer
		property int attempts: 0
		readonly property int maxAttempts: 6
		interval: 15000
		onTriggered: {
			attempts += 1
			caldavHelper.keyringRetry()
		}
	}
}
