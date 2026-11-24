from __future__ import annotations

import base64
import datetime
import importlib.util
import io
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

SCRIPTS_DIR = pathlib.Path(__file__).parents[1] / "package/contents/scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
SPEC = importlib.util.spec_from_file_location("eventcalendar_caldav_tasks", SCRIPTS_DIR / "caldav.py")
caldav = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(caldav)
vtodo = caldav.vtodo

from icalendar import Calendar  # noqa: E402

BASE = "https://cloud.example.com"
STAMP = datetime.datetime(2026, 11, 15, 12, 0, tzinfo=datetime.timezone.utc)
NOW = datetime.datetime(2026, 11, 15, 12, 0, tzinfo=datetime.timezone.utc)


def vcal(body):
	return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\n" + body + "END:VCALENDAR\r\n"


# A Nextcloud Tasks task: due 20 Nov 17:00 Berlin, high priority, a subtask of p-1, an alarm an hour
# before the due time and an email alarm, sorted.
FULL = vcal(
	"BEGIN:VTODO\r\nUID:t-1\r\nDTSTAMP:20261101T000000Z\r\nSUMMARY:Buy milk\r\nDESCRIPTION:2 litres\r\n"
	"DUE;TZID=Europe/Berlin:20261120T170000\r\nDTSTART;TZID=Europe/Berlin:20261118T090000\r\nPRIORITY:1\r\n"
	"RELATED-TO:p-1\r\nX-APPLE-SORT-ORDER:123\r\nX-CUSTOM:keep\r\nSTATUS:NEEDS-ACTION\r\n"
	"BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER;RELATED=END:-PT1H\r\nDESCRIPTION:x\r\nEND:VALARM\r\n"
	"BEGIN:VALARM\r\nACTION:EMAIL\r\nTRIGGER;RELATED=END:-P1D\r\nSUMMARY:m\r\nDESCRIPTION:m\r\nATTENDEE:mailto:a@example.com\r\nEND:VALARM\r\n"
	"END:VTODO\r\n")

DAY = vcal(
	"BEGIN:VTODO\r\nUID:t-2\r\nDTSTAMP:20261101T000000Z\r\nSUMMARY:Call\r\nDUE;VALUE=DATE:20261121\r\n"
	"PRIORITY:5\r\nBEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER;VALUE=DATE-TIME:20261121T080000Z\r\nDESCRIPTION:x\r\nEND:VALARM\r\nEND:VTODO\r\n")


def in_berlin(test):
	"""The test case's tests in Europe/Berlin (the C library's local time too), whatever the host's."""
	import time
	patcher = mock.patch.dict(os.environ, {"TZ": "Europe/Berlin"})
	patcher.start()
	time.tzset()

	def restore():
		patcher.stop()
		time.tzset()
	test.addCleanup(restore)


def todo_of(body):
	return vtodo.master_todo(Calendar.from_ical(body))


class ReadTests(unittest.TestCase):
	def setUp(self):
		in_berlin(self)

	def test_full_task(self):
		item = vtodo.resource_task(FULL, NOW)
		self.assertEqual(item, {
			"kind": "tasks#task", "id": "t-1", "title": "Buy milk", "notes": "2 litres", "status": "needsAction",
			"due": "2026-11-20T00:00:00.000Z", "dueTime": "17:00", "priority": 5, "reminders": [60], "repeatRule": "",
			"parentId": "p-1", "subtasks": [], "sortOrder": 123,
		})

	def test_day_task_and_priorities(self):
		item = vtodo.resource_task(DAY, NOW)
		self.assertEqual((item["due"], item["dueTime"], item["priority"]), ("2026-11-21T00:00:00.000Z", "", 3))
		self.assertEqual(item["reminders"], [-540], "9:00 local on its day: after the day's start")
		self.assertEqual([vtodo.widget_priority(p) for p in (0, 1, 4, 5, 6, 9, "x", None)], [0, 5, 5, 3, 1, 1, 0, 0])
		self.assertEqual([vtodo.ical_priority(p) for p in (5, 3, 1, 0)], [1, 5, 9, 0])

	def test_done_tasks_of_the_last_30_days(self):
		done = lambda when: vcal("BEGIN:VTODO\r\nUID:d\r\nSUMMARY:x\r\nSTATUS:COMPLETED\r\nCOMPLETED:{}\r\nEND:VTODO\r\n".format(when))
		item = vtodo.resource_task(done("20261101T100000Z"), NOW)
		self.assertEqual((item["status"], item["completed"]), ("completed", "2026-11-01T10:00:00.000Z"))
		self.assertIsNone(vtodo.resource_task(done("20261001T100000Z"), NOW))
		# COMPLETED without STATUS counts as done (some clients write only that).
		self.assertEqual(vtodo.resource_task(vcal("BEGIN:VTODO\r\nUID:d\r\nSUMMARY:x\r\nCOMPLETED:20261110T000000Z\r\nEND:VTODO\r\n"), NOW)["status"], "completed")
		self.assertIsNone(vtodo.resource_task(vcal("BEGIN:VTODO\r\nUID:c\r\nSUMMARY:x\r\nSTATUS:CANCELLED\r\nEND:VTODO\r\n"), NOW))
		self.assertIsNone(vtodo.resource_task(vcal("BEGIN:VEVENT\r\nUID:e\r\nDTSTART:20261101T100000Z\r\nEND:VEVENT\r\n"), NOW))

	def test_repeat_rule_and_alarm_from_the_start(self):
		body = vcal("BEGIN:VTODO\r\nUID:r\r\nSUMMARY:x\r\nDTSTART;VALUE=DATE:20261120\r\nDUE;VALUE=DATE:20261121\r\n"
			"RRULE:FREQ=WEEKLY;BYDAY=FR\r\nBEGIN:VALARM\r\nACTION:AUDIO\r\nTRIGGER:PT0S\r\nEND:VALARM\r\nEND:VTODO\r\n")
		item = vtodo.resource_task(body, NOW)
		self.assertEqual(item["repeatRule"], "RRULE:FREQ=WEEKLY;BYDAY=FR")
		self.assertEqual(item["reminders"], [1440], "at the start, a day before the due day")


class WriteTests(unittest.TestCase):
	def setUp(self):
		in_berlin(self)

	def test_new_task(self):
		body = vtodo.build_new_task({"title": "New", "notes": "n", "due": "2026-11-20T00:00:00.000Z", "dueTime": "09:30",
			"timeZone": "Europe/Berlin", "priority": 3, "reminders": [15, -30], "parentId": "p-1"}, "u-1", STAMP, "-//x//EN")
		todo = todo_of(body)
		self.assertEqual(str(todo["UID"]), "u-1")
		self.assertEqual(todo["DUE"].dt.isoformat(), "2026-11-20T09:30:00+01:00")
		self.assertEqual(todo["DUE"].params["TZID"], "Europe/Berlin")
		self.assertEqual((int(todo["PRIORITY"]), str(todo["STATUS"]), str(todo["RELATED-TO"])), (5, "NEEDS-ACTION", "p-1"))
		self.assertEqual(sorted(a["TRIGGER"].dt for a in todo.walk("VALARM")), [datetime.timedelta(minutes=-15), datetime.timedelta(minutes=30)])
		self.assertTrue(all(a["TRIGGER"].params["RELATED"] == "END" for a in todo.walk("VALARM")))
		self.assertIn(b"BEGIN:VTIMEZONE", body)
		item = vtodo.resource_task(body, NOW)
		self.assertEqual((item["dueTime"], item["reminders"], item["priority"]), ("09:30", [15, -30], 3))
		# A day, no time: VALUE=DATE; without a zone the system's (TZ here).
		todo = todo_of(vtodo.build_new_task({"title": "D", "due": "2026-11-21T00:00:00.000Z"}, "u-2", STAMP, "x"))
		self.assertEqual(todo["DUE"].dt, datetime.date(2026, 11, 21))
		todo = todo_of(vtodo.build_new_task({"title": "T", "due": "2026-11-21", "dueTime": "8:05"}, "u-3", STAMP, "x"))
		self.assertEqual((todo["DUE"].dt.isoformat(), todo["DUE"].params["TZID"]), ("2026-11-21T08:05:00+01:00", "Europe/Berlin"))

	def test_update_changes_only_what_is_given(self):
		uid, body = vtodo.build_updated_task(FULL, {"title": "Buy oat milk"}, STAMP)
		todo = todo_of(body)
		self.assertEqual((uid, str(todo["SUMMARY"]), str(todo["X-CUSTOM"]), int(todo["SEQUENCE"])), ("t-1", "Buy oat milk", "keep", 1))
		self.assertEqual(len(todo.walk("VALARM")), 2)
		self.assertEqual(todo["DUE"].dt.isoformat(), "2026-11-20T17:00:00+01:00")
		# Done, then open again.
		_, body = vtodo.build_updated_task(body, {"status": "completed", "completed": "2026-11-15T10:00:00.000Z"}, STAMP)
		todo = todo_of(body)
		self.assertEqual((str(todo["STATUS"]), todo["COMPLETED"].dt.isoformat(), int(todo["PERCENT-COMPLETE"])),
			("COMPLETED", "2026-11-15T10:00:00+00:00", 100))
		_, body = vtodo.build_updated_task(body, {"status": "needsAction", "completed": None}, STAMP)
		todo = todo_of(body)
		self.assertEqual(str(todo["STATUS"]), "NEEDS-ACTION")
		self.assertNotIn("COMPLETED", todo)
		self.assertNotIn("PERCENT-COMPLETE", todo)

	def test_due_time_and_day_changes(self):
		# Only the time: the same day. A start after the new due goes.
		_, body = vtodo.build_updated_task(FULL, {"dueTime": "08:00"}, STAMP)
		self.assertEqual(todo_of(body)["DUE"].dt.isoformat(), "2026-11-20T08:00:00+01:00")
		_, body = vtodo.build_updated_task(FULL, {"due": "2026-11-17T00:00:00.000Z"}, STAMP)
		todo = todo_of(body)
		self.assertEqual(todo["DUE"].dt.isoformat(), "2026-11-17T17:00:00+01:00", "the day, keeping the time")
		self.assertNotIn("DTSTART", todo)
		# No time any more: a day; the timed start goes (DUE and DTSTART of one kind).
		_, body = vtodo.build_updated_task(FULL, {"dueTime": ""}, STAMP)
		todo = todo_of(body)
		self.assertEqual(todo["DUE"].dt, datetime.date(2026, 11, 20))
		self.assertNotIn("DTSTART", todo)
		# No due: none, and no alarms relative to it (RFC 5545).
		_, body = vtodo.build_updated_task(FULL, {"due": None}, STAMP)
		self.assertNotIn("DUE", todo_of(body))
		self.assertEqual(todo_of(body).walk("VALARM"), [])

	def test_repeating_task_done_moves_on(self):
		weekly = FULL.replace("STATUS:NEEDS-ACTION\r\n", "STATUS:NEEDS-ACTION\r\nRRULE:FREQ=WEEKLY;COUNT=3\r\n")
		_, body = vtodo.build_updated_task(weekly, {"status": "completed"}, STAMP)
		todo = todo_of(body)
		self.assertEqual(str(todo["STATUS"]), "NEEDS-ACTION")
		self.assertNotIn("COMPLETED", todo)
		self.assertEqual(todo["DUE"].dt.isoformat(), "2026-11-27T17:00:00+01:00")
		self.assertEqual(todo["DTSTART"].dt.isoformat(), "2026-11-25T09:00:00+01:00")
		self.assertEqual(todo["RRULE"]["COUNT"], [2])
		# Its last instance: done.
		last = FULL.replace("STATUS:NEEDS-ACTION\r\n", "STATUS:NEEDS-ACTION\r\nRRULE:FREQ=WEEKLY;COUNT=1\r\n")
		_, body = vtodo.build_updated_task(last, {"status": "completed"}, STAMP)
		self.assertEqual(str(todo_of(body)["STATUS"]), "COMPLETED")

	def test_repeating_task_with_a_due_only_moves_on(self):
		# Nextcloud Tasks' and the widget's own repeating tasks: DUE, no DTSTART.
		weekly = vcal("BEGIN:VTODO\r\nUID:r-1\r\nDTSTAMP:20261101T000000Z\r\nSUMMARY:Bins\r\n"
			"DUE;TZID=Europe/Berlin:20261120T170000\r\nRRULE:FREQ=WEEKLY\r\nSTATUS:NEEDS-ACTION\r\nEND:VTODO\r\n")
		_, body = vtodo.build_updated_task(weekly, {"status": "completed"}, STAMP)
		todo = todo_of(body)
		self.assertEqual(str(todo["STATUS"]), "NEEDS-ACTION")
		self.assertNotIn("COMPLETED", todo)
		self.assertNotIn("DTSTART", todo, "no start added")
		self.assertEqual(todo["DUE"].dt.isoformat(), "2026-11-27T17:00:00+01:00")
		self.assertEqual(todo["DUE"].params["TZID"], "Europe/Berlin")
		# Until a day after the next instance: moves on once, then the series ends (done).
		until = weekly.replace("RRULE:FREQ=WEEKLY", "RRULE:FREQ=WEEKLY;UNTIL=20261128T000000Z")
		_, body = vtodo.build_updated_task(until, {"status": "completed"}, STAMP)
		todo = todo_of(body)
		self.assertEqual((str(todo["STATUS"]), todo["DUE"].dt.isoformat()), ("NEEDS-ACTION", "2026-11-27T17:00:00+01:00"))
		_, body = vtodo.build_updated_task(body, {"status": "completed"}, STAMP)
		self.assertEqual(str(todo_of(body)["STATUS"]), "COMPLETED", "past UNTIL: the series ends")

	def test_repeating_day_task_with_a_count(self):
		daily = vcal("BEGIN:VTODO\r\nUID:r-2\r\nDTSTAMP:20261101T000000Z\r\nSUMMARY:Water plants\r\n"
			"DUE;VALUE=DATE:20261120\r\nRRULE:FREQ=DAILY;INTERVAL=2;COUNT=2\r\nEND:VTODO\r\n")
		_, body = vtodo.build_updated_task(daily, {"status": "completed"}, STAMP)
		todo = todo_of(body)
		self.assertEqual((str(todo["STATUS"]), todo["DUE"].dt), ("NEEDS-ACTION", datetime.date(2026, 11, 22)))
		self.assertEqual(todo["RRULE"]["COUNT"], [1])
		_, body = vtodo.build_updated_task(body, {"status": "completed"}, STAMP)
		todo = todo_of(body)
		self.assertEqual((str(todo["STATUS"]), todo["DUE"].dt), ("COMPLETED", datetime.date(2026, 11, 22)), "its last instance")

	def test_repeating_task_that_cant_be_expanded_stays_open(self):
		weekly = vcal("BEGIN:VTODO\r\nUID:r-3\r\nSUMMARY:x\r\nDUE;VALUE=DATE:20261120\r\nRRULE:FREQ=WEEKLY\r\nEND:VTODO\r\n")
		broken = mock.Mock()
		broken.of.side_effect = ValueError("bad rule")
		with mock.patch.object(vtodo.icsjson, "recurring_ical_events", broken):
			with self.assertRaises(vtodo.TaskInputError):
				vtodo.build_updated_task(weekly, {"status": "completed"}, STAMP)
		with mock.patch.object(vtodo.icsjson, "recurring_ical_events", None):
			with self.assertRaises(vtodo.TaskInputError):
				vtodo.build_updated_task(weekly, {"status": "completed"}, STAMP)
		# A task that doesn't repeat is done whether the module is there or not.
		once = weekly.replace("RRULE:FREQ=WEEKLY\r\n", "")
		with mock.patch.object(vtodo.icsjson, "recurring_ical_events", None):
			_, body = vtodo.build_updated_task(once, {"status": "completed"}, STAMP)
		self.assertEqual(str(todo_of(body)["STATUS"]), "COMPLETED")

	def test_reminders_replace_only_their_alarms(self):
		_, body = vtodo.build_updated_task(FULL, {"reminders": [10]}, STAMP)
		alarms = todo_of(body).walk("VALARM")
		self.assertEqual(sorted((str(a["ACTION"]), a["TRIGGER"].dt) for a in alarms),
			[("DISPLAY", datetime.timedelta(minutes=-10)), ("EMAIL", datetime.timedelta(days=-1))])
		_, body = vtodo.build_updated_task(FULL, {"reminders": []}, STAMP)
		self.assertEqual([str(a["ACTION"]) for a in todo_of(body).walk("VALARM")], ["EMAIL"])

	def test_parent_priority_and_validation(self):
		_, body = vtodo.build_updated_task(FULL, {"parentId": "", "priority": 0}, STAMP)
		todo = todo_of(body)
		self.assertNotIn("RELATED-TO", todo)
		self.assertNotIn("PRIORITY", todo)
		for bad in ({"due": "tomorrow"}, {"dueTime": "25:00"}, {"priority": 2}, {"status": "done"}, {"reminders": [1.5]},
				{"reminders": list(range(6))}, {"title": 5}, {"timeZone": "Mars/Base", "due": "2026-11-20", "dueTime": "10:00"},
				{"repeatRule": "RRULE:FREQ=SOMETIMES"}):
			with self.assertRaises(vtodo.TaskInputError, msg=bad):
				vtodo.build_updated_task(FULL, bad, STAMP)
		with self.assertRaises(vtodo.TaskInputError):
			vtodo.build_new_task({"notes": "no title"}, "u", STAMP, "x")


class FakeClient(caldav.Client):
	def __init__(self, responses):
		super().__init__("user", "s3cret")
		self.responses = list(responses)
		self.sent = []

	def _send(self, method, url, body, headers):
		self.sent.append((method, url, body, dict(headers or {})))
		status, headers, data = self.responses.pop(0)
		return status, headers, data.encode("utf-8") if isinstance(data, str) else data


def ms(*responses):
	return '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">' + "".join(responses) + "</d:multistatus>"


def xml_escape(text):
	return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def components(*names):
	"""A PROPFIND answer for /l/ with these supported components (none: no component set)."""
	found = "".join('<c:comp name="{}"/>'.format(name) for name in names)
	prop = "<c:supported-calendar-component-set>{}</c:supported-calendar-component-set>".format(found) if names else ""
	return ms("<d:response><d:href>/l/</d:href><d:propstat><d:prop>{}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>".format(prop))


def task_resource(href, etag, body):
	return "<d:response><d:href>{}</d:href><d:propstat><d:prop><d:getetag>{}</d:getetag><c:calendar-data>{}</c:calendar-data></d:prop>" \
		"<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>".format(href, etag, xml_escape(body))


@mock.patch.object(caldav, "resolve_host", lambda host: [("93.184.216.34",)])
class RequestTests(unittest.TestCase):
	def setUp(self):
		in_berlin(self)

	def test_query_tasks(self):
		broken = "BEGIN:VCALENDAR\r\nnot really\r\n"
		client = FakeClient([(207, {}, ms(task_resource("/l/t-1.ics", '"e1"', FULL), task_resource("/l/x.ics", '"e2"', broken),
			task_resource("/l/t-2.ics", '"e3"', DAY)))])
		result = caldav.query_tasks(client, BASE + "/l/", probe_ctag=False, now=NOW)
		self.assertEqual([(i["id"], i["caldavHref"], i["caldavEtag"]) for i in result["items"]],
			[("t-1", BASE + "/l/t-1.ics", '"e1"'), ("t-2", BASE + "/l/t-2.ics", '"e3"')], "a broken resource is skipped")
		method, url, body, headers = client.sent[0]
		self.assertEqual((method, headers["Depth"]), ("REPORT", "1"))
		self.assertIn(b'<c:comp-filter name="VTODO"/>', body)

	def test_put_task_create_and_update(self):
		client = FakeClient([(201, {"ETag": '"n"'}, "")])
		item = caldav.put_task(client, BASE + "/l", {"title": "New"})
		method, url, body, headers = client.sent[0]
		self.assertEqual((method, url, headers["If-None-Match"]), ("PUT", BASE + "/l/" + item["id"] + ".ics", "*"))
		self.assertEqual((item["title"], item["status"], item["caldavEtag"]), ("New", "needsAction", '"n"'))
		client = FakeClient([(200, {}, FULL), (204, {"ETag": '"v2"'}, "")])
		item = caldav.put_task(client, BASE + "/l/", {"status": "completed"}, href="/l/t-1.ics", etag='"v1"')
		self.assertEqual(client.sent[1][3]["If-Match"], '"v1"')
		self.assertEqual((item["status"], item["caldavHref"], item["caldavEtag"]), ("completed", BASE + "/l/t-1.ics", '"v2"'))
		# Cancelled elsewhere meanwhile: saved, the item still given.
		cancelled = FULL.replace("STATUS:NEEDS-ACTION", "STATUS:CANCELLED")
		item = caldav.put_task(FakeClient([(200, {}, cancelled), (204, {"ETag": '"v3"'}, "")]), BASE + "/l/", {"title": "Renamed"},
			href="/l/t-1.ics", etag='"v2"')
		self.assertEqual((item["title"], item["caldavEtag"]), ("Renamed", '"v3"'))
		with self.assertRaises(caldav.CalDavError) as ctx:
			caldav.put_task(FakeClient([]), BASE + "/l/", {"notes": "x"})
		self.assertEqual(ctx.exception.kind, "INPUT")

	def test_put_task_on_the_command_line(self):
		sent = []

		class Recorder(FakeClient):
			def __init__(self, user, password):
				super().__init__([(201, {"ETag": '"x"'}, "")])
				sent.append(self)
		encoded = base64.b64encode(json.dumps({"title": "Ä task"}).encode("utf-8")).decode("ascii")
		out = io.StringIO()
		with mock.patch.object(caldav, "Client", Recorder):
			code = caldav.main(["put-task", "--url", BASE + "/l/", "--user", "u", "--password-stdin", "--task-b64", encoded],
				stdin=io.StringIO("pw\n"), stdout=out, stderr=io.StringIO())
		self.assertEqual(code, 0)
		self.assertIn("SUMMARY:Ä task", sent[0].sent[0][2].decode("utf-8"))
		self.assertEqual(json.loads(out.getvalue())["title"], "Ä task")

	def test_lists(self):
		client = FakeClient([(201, {}, "")])
		made = caldav.make_list(client, BASE + "/cal/home", " Shopping & more ", "#ff0000")
		method, url, body, _ = client.sent[0]
		self.assertEqual(method, "MKCALENDAR")
		self.assertTrue(url.startswith(BASE + "/cal/home/") and url.endswith("/"))
		self.assertIn(b"<d:displayname>Shopping &amp; more</d:displayname>", body)
		self.assertIn(b'<c:comp name="VTODO"/>', body)
		self.assertIn(b"<ical:calendar-color>#ff0000</ical:calendar-color>", body)
		self.assertEqual((made["href"], made["name"], made["tasks"], made["events"]), (url, "Shopping & more", True, False))
		ok = ms("<d:response><d:href>/l/</d:href><d:propstat><d:prop><d:displayname/></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")
		client = FakeClient([(207, {}, components("VTODO")), (207, {}, ok)])
		self.assertEqual(caldav.set_list(client, BASE + "/l/", name="Renamed"), {"ok": True})
		self.assertEqual([sent[0] for sent in client.sent], ["PROPFIND", "PROPPATCH"])
		self.assertEqual(client.sent[0][3]["Depth"], "0")
		self.assertIn(b"supported-calendar-component-set", client.sent[0][2])
		refused = ok.replace("200 OK", "403 Forbidden")
		with self.assertRaises(caldav.CalDavError):
			caldav.set_list(FakeClient([(207, {}, components("VTODO")), (207, {}, refused)]), BASE + "/l/", color="#00ff00")
		for name, color in ((" ", None), (None, "red"), (None, None)):
			with self.assertRaises(caldav.CalDavError):
				caldav.set_list(FakeClient([]), BASE + "/l/", name=name, color=color)
		client = FakeClient([(207, {}, components("VTODO")), (204, {}, "")])
		self.assertEqual(caldav.remove_list(client, BASE + "/l/"), {"ok": True})
		self.assertEqual(client.sent[1][:2], ("DELETE", BASE + "/l/"))

	def test_lists_with_events_are_left_alone(self):
		# A calendar for events and tasks (Nextcloud's "Personal"), or one without a component set
		# (all components): never renamed, recolored or deleted as a task list.
		for found in (components("VEVENT", "VTODO"), components()):
			for change in (lambda c: caldav.remove_list(c, BASE + "/l/"), lambda c: caldav.set_list(c, BASE + "/l/", name="X"),
					lambda c: caldav.set_list(c, BASE + "/l/", color="#00ff00")):
				client = FakeClient([(207, {}, found)])
				with self.assertRaises(caldav.CalDavError) as ctx:
					change(client)
				self.assertEqual(ctx.exception.kind, "INPUT")
				self.assertEqual([sent[0] for sent in client.sent], ["PROPFIND"], "no PROPPATCH or DELETE")


if __name__ == "__main__":
	unittest.main()
