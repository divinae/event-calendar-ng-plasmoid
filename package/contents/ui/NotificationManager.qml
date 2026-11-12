import QtQuick
import QtCore

import "./lib"
import "./NotificationState.js" as NotificationState

// Desktop notifications through KNotification (KNotificationBackend.qml): no helper process,
// actions handled in QML.
//
// notify({ summary, body, appName, appIcon, event, urgency, expireTimeout, soundFile, actions }, callback)
// - event: notifyrc event id (reminder, eventStarting, invitation, dailyAgenda, taskReminder, timer, syncProblem); each can be
//   configured in System Settings > Notifications > Event Calendar NG.
// - expireTimeout: 0 = keep until dismissed; > 0 = closed after that many ms; else (-1) Plasma's
//   popup timeout.
// - closeAt: ms since the epoch (eg: the event's end), > 0: a notification still shown then is
//   closed (one in Plasma's history can't be reached any more).
// - resident: true = Plasma keeps it shown when clicked or an action is chosen (closed by us or
//   dismissed); then 'closed' also comes after an action.
// - urgency: 'low' | 'normal' | 'critical'.
// - logInfo: shown in the log with it (Settings > debugging), never a title.
// - actions: ["id,Label", ...].
// callback(actionId): an action id or 'default' (the notification was clicked), once (resident: each
// time), then 'closed' once.
// Returns a handle with setCloseAt(ms) (see KNotificationBackend.send), or null.
QtObject {
	id: notificationManager

	property var executable: ExecUtil {}
	property var soundPlayer: SoundPlayer {}

	property var backend: null
	Component.onCompleted: {
		var component = Qt.createComponent("KNotificationBackend.qml")
		if (component.status === Component.Ready) {
			backend = component.createObject(notificationManager)
		} else {
			console.warn('[eventcalendar] Notifications disabled: the KNotifications QML module (org.kde.notification) is missing.', component.errorString())
		}
		installNotifyrc()
	}

	// Our notifyrc is copied to ~/.local/share/knotifications6 (a plasmoid package can't install it),
	// once per plasmashell start and only written when it changed. KNotification sends nothing for
	// a component it can't resolve, so until then the generic workspace event is used, with the app
	// name in the title.
	property bool ownComponent: NotificationState.notifyrcInstalled
	readonly property string notifyrcSource: soundPlayer.localPath(Qt.resolvedUrl("../knotifications/eventcalendar.notifyrc"))
	readonly property string notifyrcTarget: soundPlayer.localPath(StandardPaths.writableLocation(StandardPaths.GenericDataLocation)) + "/knotifications6/eventcalendar.notifyrc"
	function installNotifyrc() {
		if (NotificationState.notifyrcInstalled || NotificationState.notifyrcInstalling) {
			return
		}
		NotificationState.notifyrcInstalling = true
		executable.execArgv(['install', '-C', '-D', '-m', '644', notifyrcSource, notifyrcTarget], function(cmd, exitCode, exitStatus, stdout, stderr) {
			NotificationState.notifyrcInstalling = false
			if (exitCode === 0) {
				NotificationState.notifyrcInstalled = true
				notificationManager.ownComponent = true
			} else {
				logger.log('Could not install eventcalendar.notifyrc:', exitCode, stderr)
			}
		})
	}

	// Numbers the notifications in the log (Settings > debugging), to follow one from sent to closed.
	property int logCount: 0
	function notify(args, callback) {
		var logId = '#' + (++logCount) + ' ' + (args.event || 'reminder')
		// No event titles in logs. logInfo: eg: the event's start.
		logger.debug('notification', logId, 'sent', args.logInfo || '')
		var answered = false
		var closed = false
		function answer(actionId, closedBy) {
			// Once, or (resident) any actions; 'closed' always once, also after an action, so whoever
			// keeps the notification (eg: UpcomingEvents' shown list) learns it's gone.
			if (closed || (answered && actionId !== 'closed')) {
				return
			}
			answered = !args.resident
			closed = actionId === 'closed'
			if (closed) {
				logger.debug('notification', logId, 'closed', {
					end: 'at the event\'s end',
					timeout: 'after its timeout',
					widget: 'by the widget',
				}[closedBy] || 'in Plasma (dismissed, or timed out into the history)')
			} else {
				logger.debug('notification', logId, 'action:', actionId === 'default' ? 'clicked' : actionId)
			}
			if (typeof callback === 'function') {
				callback(actionId)
			}
		}

		var sound = args.sound || args.soundFile
		if (sound) {
			soundPlayer.play(sound)
		}
		if (!backend) {
			return null
		}

		var own = ownComponent || NotificationState.notifyrcInstalled
		var title = args.summary || ""
		if (!own && args.appName) {
			title = args.appName + " · " + title // Shown under "System Notifications" otherwise
		}
		var actionList = (args.actions || []).map(function(entry) {
			var parts = String(entry).split(',')
			var id = parts.shift()
			return { id: id, label: parts.join(',') }
		})
		var handle = backend.send({
			componentName: own ? "eventcalendar" : "plasma_workspace",
			eventId: own ? (args.event || "reminder") : "notification",
			title: title,
			text: args.body || "",
			iconName: args.appIcon || "view-calendar",
			urgency: args.urgency,
			resident: !!args.resident,
		}, actionList, args.expireTimeout === 0, answer, args.expireTimeout > 0 ? args.expireTimeout : 0, args.closeAt || 0)
		if (handle) {
			handle.logId = logId
		}
		return handle
	}
}
