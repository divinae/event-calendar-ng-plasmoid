import QtQuick
import org.kde.notification

// KNotification side of NotificationManager, in its own file so a missing org.kde.notification
// QML module only disables notifications instead of breaking the widget.
QtObject {
	id: backend

	property Component notificationComponent: Component {
		Notification {
			autoDelete: false
		}
	}
	property Component actionComponent: Component {
		NotificationAction {}
	}
	property Component cleanupTimerComponent: Component {
		Timer {
			running: true
			interval: 60 * 60 * 1000
		}
	}

	function urgencyFor(name) {
		switch (name) {
			case 'low': return Notification.LowUrgency
			case 'critical': return Notification.CriticalUrgency
			case 'normal': return Notification.NormalUrgency
			default: return Notification.DefaultUrgency
		}
	}

	property Component closeTimerComponent: Component {
		Timer {
			running: true
		}
	}

	// See NotificationManager.notify. answer(actionId, closedBy) is called by the manager's wrapper
	// (closedBy only with 'closed').
	// closeAfterMs > 0: closed by us after that time (KNotification has no per-notification
	// timeout); then it isn't kept in Plasma's history.
	// closeAtMs > 0 (eg: the event's end): a notification still shown then is closed, sooner than
	// closeAfterMs. Only one that stays shown: once Plasma's timeout moved it to the history, its
	// "closed" came and KNotification can't close it any more.
	// Returns a handle for one still shown (all no-ops once closed): setCloseAt(ms) moves closeAtMs
	// (eg: the event's end changed; a time gone by closes it now), update(title, text) changes the
	// shown popup (KNotification sends the change), close().
	function send(props, actionList, persistent, answer, closeAfterMs, closeAtMs) {
		var n = notificationComponent.createObject(backend, {
			componentName: props.componentName,
			eventId: props.eventId,
			title: props.title,
			text: props.text,
			iconName: props.iconName,
			urgency: urgencyFor(props.urgency),
			// Persistent: stays until dismissed. Otherwise Plasma's popup timeout applies, and an
			// expired notification stays in the history.
			flags: persistent || closeAfterMs > 0 ? Notification.Persistent : Notification.CloseOnTimeout,
		})
		if (props.resident) {
			n.setHint("resident", true) // Plasma: not closed by a click or an action
		}

		var actions = []
		for (var i = 0; i < actionList.length; i++) {
			var action = actionComponent.createObject(n, { label: actionList[i].label })
			action.activated.connect(answer.bind(null, actionList[i].id))
			actions.push(action)
		}
		n.actions = actions
		var defaultAction = actionComponent.createObject(n, { label: "" })
		defaultAction.activated.connect(answer.bind(null, 'default'))
		n.defaultAction = defaultAction

		// "closed" also comes when the popup times out into the history: keep the object (and its
		// actions) a while longer, then free it.
		var closeTimer = null
		var isClosed = false
		// Why it was closed, for the log: 'end' (closeAtMs), 'timeout' (closeAfterMs), 'widget'
		// (close()), '' = in Plasma (dismissed, or its popup timed out into the history).
		var closedBy = ''
		function closeFor(reason) {
			closedBy = reason
			n.close()
		}
		var keptShown = persistent || closeAfterMs > 0
		var closeAfterAt = closeAfterMs > 0 ? Date.now() + closeAfterMs : 0
		function scheduleClose(atMs, sending) {
			if (closeTimer) {
				closeTimer.destroy()
				closeTimer = null
			}
			var at = closeAfterAt
			var reason = 'timeout'
			// When sending, an end already gone by doesn't count (eg: a snoozed reminder).
			if (keptShown && atMs > 0 && !(sending && atMs <= Date.now()) && (at === 0 || atMs < at)) {
				at = atMs
				reason = 'end'
			}
			if (at === 0) {
				return
			}
			var closeIn = Math.min(at - Date.now(), 2147483647) // A Timer's int
			if (closeIn <= 0) {
				closeFor(reason)
				return
			}
			closeTimer = closeTimerComponent.createObject(n, { interval: closeIn })
			closeTimer.triggered.connect(function() { closeFor(reason) })
		}
		n.closed.connect(function() {
			isClosed = true
			if (closeTimer) {
				closeTimer.stop()
			}
			answer('closed', closedBy)
			var cleanup = cleanupTimerComponent.createObject(n)
			cleanup.triggered.connect(function() { n.destroy() })
		})
		n.sendEvent()
		scheduleClose(closeAtMs, true)
		return {
			setCloseAt: function(atMs) {
				if (!isClosed) {
					scheduleClose(atMs, false)
				}
			},
			update: function(title, text) {
				if (!isClosed) {
					n.title = title
					n.text = text
				}
			},
			close: function() {
				if (!isClosed) {
					closeFor('widget')
				}
			},
		}
	}
}
