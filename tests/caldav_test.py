from __future__ import annotations

import base64
import datetime
import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS_DIR = pathlib.Path(__file__).parents[1] / "package/contents/scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
SPEC = importlib.util.spec_from_file_location("eventcalendar_caldav", SCRIPTS_DIR / "caldav.py")
caldav = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(caldav)
icsjson = caldav.icsjson

from icalendar import Calendar  # noqa: E402

HAS_RIE = icsjson.recurring_ical_events is not None
BASE = "https://cloud.example.com"

# Undo files (delete / restore) in a directory of the tests, not the user's runtime directory.
UNDO_HOME = tempfile.TemporaryDirectory()


def setUpModule():
	os.environ["XDG_RUNTIME_DIR"] = UNDO_HOME.name


def tearDownModule():
	UNDO_HOME.cleanup()


class FakeClient(caldav.Client):
	"""A Client whose _send answers from a list of (status, headers, body) and records requests."""

	def __init__(self, responses):
		super().__init__("user", "s3cret")
		self.responses = list(responses)
		self.sent = []

	def _send(self, method, url, body, headers):
		self.sent.append((method, url, body, dict(headers or {})))
		status, headers, data = self.responses.pop(0)
		return status, headers, data.encode("utf-8") if isinstance(data, str) else data


def ms(*responses):
	return '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" ' \
		'xmlns:cs="http://calendarserver.org/ns/" xmlns:ical="http://apple.com/ns/ical/" ' \
		'xmlns:oc="http://owncloud.org/ns">' + "".join(responses) + "</d:multistatus>"


def resp(href, props, status="HTTP/1.1 200 OK", missing=""):
	extra = ""
	if missing:
		extra = "<d:propstat><d:prop>{}</d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>".format(missing)
	return "<d:response><d:href>{}</d:href><d:propstat><d:prop>{}</d:prop><d:status>{}</d:status></d:propstat>{}</d:response>".format(
		href, props, status, extra)


def vcal(body):
	return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\n" + body + "END:VCALENDAR\r\n"


def xml_escape(text):
	return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


PUBLIC = [("93.184.216.34",)]


def fake_resolve(host):
	return {
		"cloud.example.com": ["93.184.216.34"],
		"public.example.com": ["93.184.216.34"],
		"nas.lan": ["192.168.1.10"],
		"ula.lan": ["fd00::5"],
		"mixed.example.com": ["192.168.1.10", "93.184.216.34"],
	}.get(host, [host])


NEXTCLOUD_HOME = ms(
	resp("/remote.php/dav/calendars/user/", "<d:resourcetype><d:collection/></d:resourcetype>"),
	resp("/remote.php/dav/calendars/user/personal/",
		"<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
		"<d:displayname>Personal</d:displayname>"
		"<ical:calendar-color>#0082C9FF</ical:calendar-color>"
		"<c:supported-calendar-component-set><c:comp name=\"VEVENT\"/><c:comp name=\"VTODO\"/></c:supported-calendar-component-set>"
		"<d:current-user-privilege-set><d:privilege><d:read/></d:privilege><d:privilege><d:write/></d:privilege></d:current-user-privilege-set>"
		"<cs:getctag>http://sabre.io/ns/sync/42</cs:getctag>"),
	resp("/remote.php/dav/calendars/user/tasks/",
		"<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
		"<d:displayname>Tasks</d:displayname>"
		"<c:supported-calendar-component-set><c:comp name=\"VTODO\"/></c:supported-calendar-component-set>"),
	resp("/remote.php/dav/calendars/user/shared_by_bob/",
		"<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
		"<d:displayname>Bob's</d:displayname>"
		"<ical:calendar-color>#ff0000</ical:calendar-color>"
		"<d:current-user-privilege-set><d:privilege><d:read/></d:privilege><d:privilege><d:read-free-busy/></d:privilege></d:current-user-privilege-set>"),
	resp("/remote.php/dav/calendars/user/inbox/", "<d:resourcetype><d:collection/><c:schedule-inbox/></d:resourcetype>"),
)

RADICALE_HOME = ms(
	resp("/test/", "<d:resourcetype><d:principal/><d:collection/></d:resourcetype>"),
	resp("/test/cal/",
		"<d:resourcetype><c:calendar/><d:collection/></d:resourcetype>"
		"<ical:calendar-color>#ABC</ical:calendar-color>"
		"<d:current-user-privilege-set><d:privilege><d:all/></d:privilege></d:current-user-privilege-set>"
		"<cs:getctag>\"abc\"</cs:getctag>",
		missing="<d:displayname/>"),
)


@mock.patch.object(caldav, "resolve_host", fake_resolve)
class UrlRuleTests(unittest.TestCase):
	def test_https_is_accepted(self):
		self.assertEqual(caldav.check_url("https://cloud.example.com/remote.php/dav"), "https://cloud.example.com/remote.php/dav")

	def test_http_on_loopback_and_private_is_accepted(self):
		self.assertEqual(caldav.check_url("http://127.0.0.1:5232/"), "http://127.0.0.1:5232/")
		self.assertEqual(caldav.check_url("http://nas.lan/dav"), "http://nas.lan/dav")
		self.assertEqual(caldav.check_url("http://ula.lan/dav"), "http://ula.lan/dav")
		self.assertEqual(caldav.check_url("http://[::1]:8080/x"), "http://[::1]:8080/x")

	def test_http_on_public_host_is_refused(self):
		for url in ("http://public.example.com/dav", "http://mixed.example.com/dav"):
			with self.assertRaises(caldav.CalDavError) as ctx:
				caldav.check_url(url)
			self.assertEqual(ctx.exception.kind, "INPUT")
			self.assertIn("http only for local servers", ctx.exception.message)

	def test_other_schemes_and_userinfo(self):
		for url in ("ftp://cloud.example.com/", "file:///etc/passwd", "", "https:///nohost"):
			with self.assertRaises(caldav.CalDavError) as ctx:
				caldav.check_url(url)
			self.assertEqual(ctx.exception.kind, "INPUT")
		self.assertEqual(caldav.check_url("https://me:pw@cloud.example.com/dav"), "https://cloud.example.com/dav")

	def test_redirect_to_other_host_is_refused(self):
		client = FakeClient([(301, {"Location": "https://evil.example.org/dav/"}, "")])
		with self.assertRaises(caldav.CalDavError) as ctx:
			client.request("PROPFIND", BASE + "/dav/", "<x/>", ok=(207,))
		self.assertEqual(ctx.exception.kind, "PROTOCOL")
		self.assertNotIn("evil.example.org", ctx.exception.message)

	def test_redirect_to_other_scheme_is_refused(self):
		client = FakeClient([(301, {"Location": "http://cloud.example.com/dav/"}, "")])
		with self.assertRaises(caldav.CalDavError) as ctx:
			client.request("GET", BASE + "/dav/")
		self.assertEqual(ctx.exception.kind, "PROTOCOL")

	def test_same_host_redirect_keeps_method_and_body(self):
		client = FakeClient([
			(301, {"Location": "/remote.php/dav/"}, ""),
			(308, {"Location": "https://cloud.example.com/remote.php/dav/x/"}, ""),
			(207, {}, ms()),
		])
		_, _, _, final = client.request("PROPFIND", BASE + "/.well-known/caldav", "<body/>", {"Depth": "0"}, ok=(207,))
		self.assertEqual(final, BASE + "/remote.php/dav/x/")
		self.assertEqual([s[0] for s in client.sent], ["PROPFIND"] * 3)
		self.assertTrue(all(s[2] == b"<body/>" for s in client.sent))

	def test_too_many_redirects(self):
		client = FakeClient([(302, {"Location": "/a"}, "")] * 10)
		with self.assertRaises(caldav.CalDavError) as ctx:
			client.request("GET", BASE + "/a")
		self.assertEqual(ctx.exception.kind, "PROTOCOL")
		self.assertEqual(len(client.sent), caldav.MAX_REDIRECTS + 1)


@mock.patch.object(caldav, "resolve_host", fake_resolve)
class ErrorKindTests(unittest.TestCase):
	def test_status_mapping(self):
		for status, kind in ((401, "AUTH"), (403, "AUTH"), (404, "NOTFOUND"), (410, "NOTFOUND"),
				(412, "CONFLICT"), (500, "PROTOCOL"), (409, "PROTOCOL")):
			client = FakeClient([(status, {}, "")])
			with self.assertRaises(caldav.CalDavError) as ctx:
				client.request("GET", BASE + "/dav/x.ics", ok=(200,))
			self.assertEqual(ctx.exception.kind, kind, status)
			self.assertNotIn("cloud.example.com", ctx.exception.message)

	def test_network_errors(self):
		import urllib.error
		client = caldav.Client("u", "p")
		with mock.patch.object(client._opener, "open", side_effect=urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))):
			with self.assertRaises(caldav.CalDavError) as ctx:
				client.request("GET", BASE + "/")
		self.assertEqual(ctx.exception.kind, "NETWORK")
		with mock.patch.object(client._opener, "open", side_effect=TimeoutError()):
			with self.assertRaises(caldav.CalDavError) as ctx:
				client.request("GET", BASE + "/")
		self.assertEqual(ctx.exception.kind, "NETWORK")

	def test_bad_xml_and_doctype(self):
		for data in (b"not xml", b"<?xml version='1.0'?><!DOCTYPE x [<!ENTITY a 'b'>]><x>&a;</x>", b"", b"<a xmlns='DAV:'/>"):
			with self.assertRaises(caldav.CalDavError) as ctx:
				caldav.parse_multistatus(data, BASE)
			self.assertEqual(ctx.exception.kind, "PROTOCOL")

	def test_cli_errors_are_one_line_and_never_show_the_password(self):
		cases = [
			(["query", "--url", BASE, "--user", "u", "--password-stdin", "2026-01-01"], "INPUT"),
			(["query", "--url", BASE, "--user", "u", "--password-stdin", "2026-13-01", "2026-01-02"], "INPUT"),
			(["nope"], "INPUT"),
			(["discover", "--url", "http://public.example.com/", "--user", "u", "--password-stdin"], "INPUT"),
			(["delete", "--href", BASE + "/x.ics", "--etag", "e", "--user", "u"], "INPUT"),  # no --account
		]
		for argv, kind in cases:
			out, err = io.StringIO(), io.StringIO()
			code = caldav.main(argv, stdin=io.StringIO("hunter2\n"), stdout=out, stderr=err)
			self.assertNotEqual(code, 0)
			self.assertEqual(out.getvalue(), "")
			lines = err.getvalue().splitlines()
			self.assertEqual(len(lines), 1, lines)
			self.assertTrue(lines[0].startswith(kind + ": "), lines[0])

		class Boom(caldav.Client):
			def _send(self, method, url, body, headers):
				raise RuntimeError("leak hunter2 here")
		err = io.StringIO()
		with mock.patch.object(caldav, "Client", Boom):
			code = caldav.main(["discover", "--url", BASE, "--user", "u", "--password-stdin"],
				stdin=io.StringIO("hunter2\n"), stdout=io.StringIO(), stderr=err)
		self.assertEqual(code, 1)
		self.assertNotIn("hunter2", err.getvalue())
		self.assertTrue(err.getvalue().startswith("PROTOCOL: "))

	def test_keyring_lookup_attributes(self):
		with mock.patch("secret_store.lookup_secret_detailed", return_value=(True, "pw", "")) as lookup:
			self.assertEqual(caldav.keyring_password("acc_1"), "pw")
		lookup.assert_called_once_with([
			("app", "plasma-applet-eventcalendar"), ("scope", "caldav-account"),
			("key", "password"), ("account_id", "acc_1"),
		])
		with mock.patch("secret_store.lookup_secret_detailed", return_value=(False, "", "")):
			with self.assertRaises(caldav.CalDavError) as ctx:
				caldav.keyring_password("acc_1")
		self.assertEqual(ctx.exception.kind, "AUTH")
		# Not answering (eg: at login, before the Secret Service is up): try again later.
		with mock.patch("secret_store.lookup_secret_detailed", return_value=(False, "", "Secret storage backend unavailable.")):
			with self.assertRaises(caldav.CalDavError) as ctx:
				caldav.keyring_password("acc_1")
		self.assertEqual(ctx.exception.kind, "KEYRING")


@mock.patch.object(caldav, "resolve_host", fake_resolve)
class DiscoveryTests(unittest.TestCase):
	def test_nextcloud_like_discovery_via_well_known(self):
		client = FakeClient([
			(404, {}, ""),  # the entered URL is not DAV
			(301, {"Location": "/remote.php/dav/"}, ""),
			(207, {}, ms(resp("/remote.php/dav/", "<d:current-user-principal><d:href>/remote.php/dav/principals/users/user/</d:href></d:current-user-principal>"))),
			(207, {}, ms(resp("/remote.php/dav/principals/users/user/", "<c:calendar-home-set><d:href>/remote.php/dav/calendars/user/</d:href></c:calendar-home-set>"))),
			(207, {}, NEXTCLOUD_HOME),
		])
		result = caldav.discover(client, BASE + "/index.php")
		self.assertEqual(result["principal"], BASE + "/remote.php/dav/principals/users/user/")
		self.assertEqual(result["home"], BASE + "/remote.php/dav/calendars/user/")
		self.assertEqual(client.sent[1][1], BASE + "/.well-known/caldav")
		self.assertEqual(client.sent[-1][3]["Depth"], "1")
		self.assertEqual(client.sent[3][3]["Depth"], "0")
		cals = result["calendars"]
		# Task lists (VTODO only) too, marked: events no, tasks yes.
		self.assertEqual([c["name"] for c in cals], ["Personal", "Tasks", "Bob's"])
		self.assertEqual(cals[0], {
			"href": BASE + "/remote.php/dav/calendars/user/personal/",
			"name": "Personal",
			"color": "#0082c9",
			"writable": True,
			"components": ["VEVENT", "VTODO"],
			"events": True,
			"tasks": True,
			"ctag": "http://sabre.io/ns/sync/42",
		})
		self.assertEqual((cals[1]["events"], cals[1]["tasks"]), (False, True))
		self.assertFalse(cals[2]["writable"])
		self.assertEqual(cals[2]["color"], "#ff0000")
		self.assertEqual(cals[2]["components"], [])
		self.assertEqual((cals[2]["events"], cals[2]["tasks"]), (True, False), "no component set: events")

	def test_radicale_like_discovery_on_entered_url(self):
		client = FakeClient([
			(207, {}, ms(resp("/", "<d:current-user-principal><d:href>/test/</d:href></d:current-user-principal>"))),
			(207, {}, ms(resp("/test/", "<c:calendar-home-set><d:href>/test/</d:href></c:calendar-home-set>"))),
			(207, {}, RADICALE_HOME),
		])
		result = caldav.discover(client, "https://dav.example.com/")
		self.assertEqual(len(client.sent), 3)
		cal = result["calendars"][0]
		self.assertEqual(cal["href"], "https://dav.example.com/test/cal/")
		self.assertEqual(cal["name"], "cal")  # no displayname: the last path segment
		self.assertEqual(cal["color"], "#aabbcc")
		self.assertTrue(cal["writable"])
		self.assertEqual(cal["ctag"], '"abc"')

	def test_auth_failure_is_not_retried(self):
		client = FakeClient([(401, {}, "")])
		with self.assertRaises(caldav.CalDavError) as ctx:
			caldav.discover(client, BASE + "/")
		self.assertEqual(ctx.exception.kind, "AUTH")
		self.assertEqual(len(client.sent), 1)

	def test_no_principal(self):
		client = FakeClient([(207, {}, ms(resp("/", "<d:displayname>x</d:displayname>"))), (404, {}, "")])
		with self.assertRaises(caldav.CalDavError) as ctx:
			caldav.discover(client, BASE + "/")
		self.assertEqual(ctx.exception.kind, "NOTFOUND")

	def test_color_normalization(self):
		self.assertEqual(caldav.normalize_color("#FF00AA80"), "#ff00aa")
		self.assertEqual(caldav.normalize_color("#123456"), "#123456")
		self.assertEqual(caldav.normalize_color("red"), "")
		self.assertEqual(caldav.normalize_color(""), "")

	def test_missing_privileges_mean_writable(self):
		entries = caldav.parse_multistatus(ms(resp("/h/c/", "<d:resourcetype><c:calendar/></d:resourcetype>")).encode(), BASE)
		self.assertTrue(caldav.parse_calendar_list(entries, BASE + "/h/")[0]["writable"])
		entries = caldav.parse_multistatus(ms(resp("/h/c/", "<d:resourcetype><c:calendar/></d:resourcetype>"
			"<d:current-user-privilege-set><d:privilege><d:write-content/></d:privilege></d:current-user-privilege-set>")).encode(), BASE)
		self.assertTrue(caldav.parse_calendar_list(entries, BASE + "/h/")[0]["writable"])


SINGLE = vcal(
	"BEGIN:VEVENT\r\nUID:single-1\r\nDTSTAMP:20261101T000000Z\r\nDTSTART:20261103T100000Z\r\n"
	"DTEND:20261103T110000Z\r\nSUMMARY:Standup\r\nLOCATION:Room 1\r\nEND:VEVENT\r\n")
ALLDAY = vcal(
	"BEGIN:VEVENT\r\nUID:allday-1\r\nDTSTAMP:20261101T000000Z\r\nDTSTART;VALUE=DATE:20261104\r\n"
	"DTEND;VALUE=DATE:20261106\r\nSUMMARY:Trip\r\nEND:VEVENT\r\n")
ZONED = vcal(
	"BEGIN:VEVENT\r\nUID:zoned-1\r\nDTSTAMP:20261101T000000Z\r\nDTSTART;TZID=America/New_York:20261105T090000\r\n"
	"DTEND;TZID=America/New_York:20261105T093000\r\nSUMMARY:NY call\r\nEND:VEVENT\r\n")
RECURRING = vcal(
	"BEGIN:VEVENT\r\nUID:weekly-1\r\nDTSTAMP:20261101T000000Z\r\nDTSTART:20261102T080000Z\r\n"
	"DTEND:20261102T090000Z\r\nRRULE:FREQ=DAILY;COUNT=10\r\nEXDATE:20261104T080000Z\r\nSUMMARY:Daily\r\nEND:VEVENT\r\n"
	"BEGIN:VEVENT\r\nUID:weekly-1\r\nDTSTAMP:20261101T000000Z\r\nRECURRENCE-ID:20261103T080000Z\r\n"
	"DTSTART:20261103T120000Z\r\nDTEND:20261103T130000Z\r\nSUMMARY:Daily (moved)\r\nEND:VEVENT\r\n")

ALARMED = vcal(
	"BEGIN:VEVENT\r\nUID:alarm-1\r\nDTSTAMP:20261101T000000Z\r\nDTSTART:20261102T080000Z\r\n"
	"DTEND:20261102T090000Z\r\nRRULE:FREQ=DAILY;COUNT=3\r\nSUMMARY:Standup\r\n"
	"BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Standup\r\nTRIGGER:-PT10M\r\nEND:VALARM\r\nEND:VEVENT\r\n")

def report_response(*resources):
	return ms(*[
		resp(href, "<d:getetag>{}</d:getetag><c:calendar-data>{}</c:calendar-data>".format(etag, xml_escape(data)))
		for href, etag, data in resources
	])


def ctag_response(ctag):
	return ms(resp("/cal/", "<cs:getctag>{}</cs:getctag>".format(xml_escape(ctag))))


class MissingModuleTests(unittest.TestCase):
	def test_missing_module_is_a_deps_line(self):
		err = io.StringIO()
		with mock.patch.object(caldav, "MISSING_MODULE", "icalendar"):
			code = caldav.main(["discover", "--url", "https://x/", "--user", "u"], stdin=io.StringIO(""), stdout=io.StringIO(), stderr=err)
		self.assertEqual(code, 1)
		self.assertTrue(err.getvalue().startswith("DEPS: The Python module icalendar is missing"), err.getvalue())


@mock.patch.object(caldav, "resolve_host", fake_resolve)
class CtagTests(unittest.TestCase):
	start = datetime.datetime(2026, 11, 2)
	end = datetime.datetime(2026, 11, 9)

	def test_unchanged_skips_the_events(self):
		client = FakeClient([(207, {}, ctag_response("tag-7"))])
		result = caldav.query(client, BASE + "/cal/", self.start, self.end, "tag-7")
		self.assertEqual(result, {"unchanged": True, "ctag": "tag-7"})
		self.assertEqual([m for m, _, _, _ in client.sent], ["PROPFIND"], "no REPORT")

	def test_changed_fetches_with_the_new_tag(self):
		client = FakeClient([(207, {}, ctag_response("tag-8")), (207, {}, report_response(("/cal/single-1.ics", '"e1"', SINGLE)))])
		result = caldav.query(client, BASE + "/cal/", self.start, self.end, "tag-7")
		self.assertEqual(result["ctag"], "tag-8")
		self.assertEqual(len(result["items"]), 1)

	def test_no_tag_on_the_server_always_fetches(self):
		client = FakeClient([(207, {}, ms(resp("/cal/", "<d:displayname>x</d:displayname>"))), (207, {}, report_response())])
		result = caldav.query(client, BASE + "/cal/", self.start, self.end, "")
		self.assertEqual(result["ctag"], "")
		self.assertEqual([m for m, _, _, _ in client.sent], ["PROPFIND", "REPORT"])

	def test_no_ctag_skips_the_tag_request(self):
		client = FakeClient([(207, {}, report_response())])
		result = caldav.query(client, BASE + "/cal/", self.start, self.end, "", False)
		self.assertEqual([m for m, _, _, _ in client.sent], ["REPORT"])
		self.assertEqual(result["ctag"], "")

	def test_network_error_on_the_tag_is_reported_once(self):
		class Offline(FakeClient):
			def _send(self, method, url, body, headers):
				self.sent.append((method, url, body, dict(headers or {})))
				raise caldav.CalDavError("NETWORK", "unreachable")
		client = Offline([])
		with self.assertRaises(caldav.CalDavError) as ctx:
			caldav.query(client, BASE + "/cal/", self.start, self.end, "tag-7")
		self.assertEqual(ctx.exception.kind, "NETWORK")
		self.assertEqual(len(client.sent), 1, "no second wait for the events")

	def test_tag_starting_with_a_dash_parses(self):
		args = caldav.build_parser().parse_args(["query", "--url", "https://x/", "--if-ctag=-1a2b", "--user", "u", "2026-11-01", "2026-11-08"])
		self.assertEqual(args.if_ctag, "-1a2b")

	def test_tag_request_failing_still_fetches(self):
		client = FakeClient([(500, {}, "oops"), (207, {}, report_response())])
		result = caldav.query(client, BASE + "/cal/", self.start, self.end, "tag-7")
		self.assertEqual(result, {"items": [], "ctag": ""})


@mock.patch.object(caldav, "resolve_host", fake_resolve)
class QueryTests(unittest.TestCase):
	start = datetime.datetime(2026, 11, 2)
	end = datetime.datetime(2026, 11, 9)

	def run_query(self, *resources):
		client = FakeClient([(207, {}, ctag_response('"c1"')), (207, {}, report_response(*resources))])
		result = caldav.query(client, BASE + "/cal/", self.start, self.end)
		self.assertEqual(result["ctag"], '"c1"')
		method, url, body, headers = client.sent[1]
		self.assertEqual((method, url, headers["Depth"]), ("REPORT", BASE + "/cal/", "1"))
		self.assertIn(b'comp-filter name="VEVENT"', body)
		self.assertIn(b"<c:time-range start=", body)
		return result["items"]

	def test_single_event(self):
		items = self.run_query(("/cal/single-1.ics", '"e1"', SINGLE))
		self.assertEqual(len(items), 1)
		item = items[0]
		expected = json.loads(icsjson.events_to_json(Calendar.from_ical(SINGLE).walk("VEVENT"), indent=None))["items"][0]
		for key, value in expected.items():
			self.assertEqual(item[key], value)
		self.assertEqual(item["summary"], "Standup")
		self.assertEqual(item["location"], "Room 1")
		self.assertEqual(item["caldavHref"], BASE + "/cal/single-1.ics")
		self.assertEqual(item["caldavEtag"], '"e1"')
		self.assertEqual(item["caldavUid"], "single-1")
		self.assertFalse(item["recurring"])
		self.assertNotIn("recurringEventId", item)

	def test_match_keeps_events_with_every_word(self):
		krakow = vcal("BEGIN:VEVENT\r\nUID:k-1\r\nDTSTART:20261104T100000Z\r\nDTEND:20261104T110000Z\r\n"
			"SUMMARY:Trip\r\nLOCATION:Kraków\r\nDESCRIPTION:<b>Train</b>&nbsp;tickets\r\nEND:VEVENT\r\n")
		resources = [("/cal/single-1.ics", '"e1"', SINGLE), ("/cal/k-1.ics", '"e2"', krakow)]
		for match, found in (("krakow TRAIN", ["Trip"]), ("standup room", ["Standup"]), ("train tickets", ["Trip"]),
				("trip standup", []), ("b", []), ("", []), ("ROOM 1", ["Standup"])):
			client = FakeClient([(207, {}, report_response(*resources))])
			result = caldav.query(client, BASE + "/cal/", self.start, self.end, probe_ctag=False, match=match)
			self.assertEqual([i["summary"] for i in result["items"]], found, match)
			self.assertEqual(client.sent[0][0], "REPORT") # No change tag asked for

	def test_match_skips_other_resources_and_keeps_the_nearest(self):
		daily = vcal("BEGIN:VEVENT\r\nUID:d-1\r\nDTSTART:20250101T080000Z\r\nDTEND:20250101T083000Z\r\n"
			"RRULE:FREQ=DAILY\r\nSUMMARY:Standup daily\r\nEND:VEVENT\r\n")
		other = vcal("BEGIN:VEVENT\r\nUID:o-1\r\nDTSTART:20250101T080000Z\r\nDTEND:20250101T083000Z\r\n"
			"RRULE:FREQ=DAILY\r\nSUMMARY:Lunch\r\nEND:VEVENT\r\n")
		calls = []
		real = caldav.resource_items

		def counting(href, *args):
			calls.append(href)
			return real(href, *args)
		client = FakeClient([(207, {}, report_response(("/cal/d-1.ics", '"e1"', daily), ("/cal/o-1.ics", '"e2"', other)))])
		with mock.patch.object(caldav, "resource_items", counting):
			result = caldav.query(client, BASE + "/cal/", datetime.datetime(2025, 11, 15), datetime.datetime(2027, 11, 15), probe_ctag=False, match="standup")
		self.assertEqual(calls, [BASE + "/cal/d-1.ics"], "Lunch isn't expanded")
		self.assertEqual(len(result["items"]), caldav.MAX_MATCHES)
		now = datetime.datetime.now(datetime.timezone.utc)
		far = max(abs(datetime.datetime.fromisoformat(i["start"]["dateTime"]) - now) for i in result["items"])
		self.assertLess(far, datetime.timedelta(days=caldav.MAX_MATCHES), "the nearest to now")

	def test_all_day_event(self):
		item = self.run_query(("/cal/allday-1.ics", '"e2"', ALLDAY))[0]
		self.assertEqual(item["start"], {"date": "2026-11-04"})
		self.assertEqual(item["end"], {"date": "2026-11-06"})

	def test_timezone_event(self):
		item = self.run_query(("/cal/zoned-1.ics", '"e3"', ZONED))[0]
		self.assertEqual(item["start"], {"dateTime": "2026-11-05T09:00:00-05:00"})
		self.assertEqual(item["end"], {"dateTime": "2026-11-05T09:30:00-05:00"})

	def test_out_of_range_event_is_dropped(self):
		later = SINGLE.replace("20261103", "20261203")
		self.assertEqual(self.run_query(("/cal/single-1.ics", '"e1"', later)), [])

	@unittest.skipUnless(HAS_RIE, "recurring-ical-events is not installed")
	def test_recurring_expansion(self):
		items = self.run_query(("/cal/weekly-1.ics", '"e4"', RECURRING))
		starts = [i["start"]["dateTime"] for i in items]
		# 2..8 Nov, minus the EXDATE on the 4th; the 3rd moved to 12:00.
		self.assertEqual(len(items), 6, starts)
		self.assertIn("2026-11-03T12:00:00+00:00", starts)
		self.assertNotIn("2026-11-04T08:00:00+00:00", starts)
		self.assertEqual(len({i["id"] for i in items}), 6)
		for item in items:
			self.assertTrue(item["recurring"])
			self.assertEqual(item["caldavUid"], "weekly-1")
			self.assertEqual(item["recurringEventId"], "weekly-1")
			self.assertEqual(item["caldavHref"], BASE + "/cal/weekly-1.ics")
		moved = [i for i in items if i["start"]["dateTime"].startswith("2026-11-03")][0]
		self.assertEqual(moved["summary"], "Daily (moved)")
		# Stable ids: the same query gives the same ids.
		again = self.run_query(("/cal/weekly-1.ics", '"e4"', RECURRING))
		self.assertEqual([i["id"] for i in items], [i["id"] for i in again])

	@unittest.skipUnless(HAS_RIE, "recurring-ical-events is not installed")
	def test_server_alarms_as_reminders(self):
		# The server's alarm (VALARM) on every occurrence: notified as set there (#14).
		items = self.run_query(("/cal/alarm-1.ics", '"e5"', ALARMED))
		self.assertEqual(len(items), 3)
		for item in items:
			self.assertEqual(item["reminderMinutes"], [10])

	def test_without_recurring_ical_events_only_plain_events(self):
		with mock.patch.object(icsjson, "recurring_ical_events", None):
			items = self.run_query(("/cal/weekly-1.ics", '"e4"', RECURRING), ("/cal/single-1.ics", '"e1"', SINGLE))
		self.assertEqual([i["caldavUid"] for i in items], ["single-1"])


@mock.patch.object(caldav, "resolve_host", fake_resolve)
class PutTests(unittest.TestCase):
	stamp = datetime.datetime(2026, 11, 2, 12, 0, tzinfo=datetime.timezone.utc)

	def test_create_timed_with_time_zone(self):
		uid, body = caldav.build_new_calendar({
			"summary": "Lunch", "location": "Cafe",
			"start": {"dateTime": "2026-11-05T12:00:00", "timeZone": "Europe/Oslo"},
			"end": {"dateTime": "2026-11-05T13:00:00+01:00", "timeZone": "Europe/Oslo"},
		}, uid="u-1", stamp=self.stamp)
		event = Calendar.from_ical(body).walk("VEVENT")[0]
		self.assertEqual(str(event["UID"]), "u-1")
		self.assertIn("DTSTAMP", event)
		self.assertEqual(str(event["DTSTART"].params["TZID"]), "Europe/Oslo")
		self.assertEqual(event["DTSTART"].dt.isoformat(), "2026-11-05T12:00:00+01:00")
		self.assertEqual(event["DTEND"].dt.isoformat(), "2026-11-05T13:00:00+01:00")
		self.assertEqual(str(event["LOCATION"]), "Cafe")
		self.assertIn(b"BEGIN:VTIMEZONE", body)

	def test_create_all_day_and_utc(self):
		_, body = caldav.build_new_calendar({"summary": "Off", "start": {"date": "2026-11-05"}, "end": {"date": "2026-11-06"}})
		event = Calendar.from_ical(body).walk("VEVENT")[0]
		self.assertEqual(event["DTSTART"].dt, datetime.date(2026, 11, 5))
		self.assertIn(b"DTSTART;VALUE=DATE:20261105", body)
		_, body = caldav.build_new_calendar({"summary": "Z", "start": {"dateTime": "2026-11-05T10:00:00Z"},
			"end": {"dateTime": "2026-11-05T11:00:00+01:00"}})
		self.assertIn(b"DTSTART:20261105T100000Z", body)
		self.assertIn(b"DTEND:20261105T100000Z", body)

	def test_create_with_recurrence(self):
		_, body = caldav.build_new_calendar({"summary": "Weekly", "start": {"date": "2026-11-02"}, "end": {"date": "2026-11-03"},
			"recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO", "EXDATE;VALUE=DATE:20261109", "RDATE;VALUE=DATE:20261111"]})
		event = Calendar.from_ical(body).walk("VEVENT")[0]
		self.assertEqual(event["RRULE"]["FREQ"], ["WEEKLY"])
		self.assertIn(b"EXDATE;VALUE=DATE:20261109", body)
		self.assertIn(b"RDATE;VALUE=DATE:20261111", body)

	def test_create_validation(self):
		bad = [
			{"start": {"date": "2026-11-05"}, "end": {"date": "2026-11-06"}},
			{"summary": "x", "start": {"date": "2026-11-05"}, "end": {"dateTime": "2026-11-06T10:00:00"}},
			{"summary": "x", "start": {"date": "2026-11-05"}, "end": {"date": "2026-11-05"}},
			{"summary": "x", "start": {"dateTime": "nope"}, "end": {"date": "2026-11-06"}},
			{"summary": "x", "start": {"dateTime": "2026-11-05T10:00", "timeZone": "Mars/Base"}, "end": {"dateTime": "2026-11-05T11:00"}},
			{"summary": "x", "start": {"date": "2026-11-05"}, "end": {"date": "2026-11-06"}, "recurrence": ["SUMMARY:evil"]},
			{"summary": "x", "start": {"date": "2026-11-05"}, "end": {"date": "2026-11-06"}, "recurrence": "RRULE:FREQ=DAILY"},
		]
		for data in bad:
			with self.assertRaises(caldav.CalDavError) as ctx:
				caldav.build_new_calendar(data)
			self.assertEqual(ctx.exception.kind, "INPUT", data)

	EXISTING = vcal(
		"BEGIN:VEVENT\r\nUID:ex-1\r\nDTSTAMP:20261001T000000Z\r\nDTSTART:20261102T080000Z\r\n"
		"DTEND:20261102T090000Z\r\nRRULE:FREQ=WEEKLY\r\nSUMMARY:Old\r\nLOCATION:Here\r\nDESCRIPTION:Keep me\r\n"
		"SEQUENCE:2\r\nATTENDEE;CN=Bob:mailto:bob@example.com\r\nX-CUSTOM:yes\r\n"
		"BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT15M\r\nDESCRIPTION:Reminder\r\nEND:VALARM\r\nEND:VEVENT\r\n"
		"BEGIN:VEVENT\r\nUID:ex-1\r\nRECURRENCE-ID:20261109T080000Z\r\nDTSTART:20261109T100000Z\r\n"
		"DTEND:20261109T110000Z\r\nSUMMARY:Override\r\nEND:VEVENT\r\n")

	def test_update_keeps_unchanged_properties(self):
		uid, body = caldav.build_updated_calendar(self.EXISTING, {"summary": "New", "location": ""}, stamp=self.stamp)
		self.assertEqual(uid, "ex-1")
		cal = Calendar.from_ical(body)
		master, override = cal.walk("VEVENT")
		self.assertEqual(str(master["SUMMARY"]), "New")
		self.assertNotIn("LOCATION", master)
		self.assertEqual(str(master["DESCRIPTION"]), "Keep me")
		self.assertEqual(master["RRULE"]["FREQ"], ["WEEKLY"])
		self.assertIn("ATTENDEE", master)
		self.assertEqual(str(master["X-CUSTOM"]), "yes")
		self.assertEqual(len(master.walk("VALARM")), 1)
		self.assertEqual(int(master["SEQUENCE"]), 3)
		self.assertEqual(master["DTSTART"].dt.isoformat(), "2026-11-02T08:00:00+00:00")
		self.assertEqual(str(override["SUMMARY"]), "Override")

	ALARMS = vcal(
		"BEGIN:VEVENT\r\nUID:al-1\r\nDTSTAMP:20261001T000000Z\r\nDTSTART:20261102T080000Z\r\n"
		"DTEND:20261102T090000Z\r\nSUMMARY:Alarms\r\nTRANSP:OPAQUE\r\nCLASS:PUBLIC\r\nCOLOR:teal\r\n"
		"BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT15M\r\nDESCRIPTION:x\r\nEND:VALARM\r\n"
		"BEGIN:VALARM\r\nACTION:AUDIO\r\nTRIGGER;RELATED=END:-PT2H\r\nEND:VALARM\r\n"
		"BEGIN:VALARM\r\nACTION:EMAIL\r\nTRIGGER:-P1D\r\nSUMMARY:m\r\nDESCRIPTION:m\r\nATTENDEE:mailto:a@example.com\r\nEND:VALARM\r\n"
		"BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:PT5M\r\nDESCRIPTION:after\r\nEND:VALARM\r\nEND:VEVENT\r\n")

	def test_reminders_color_and_availability(self):
		item = caldav.resource_items(BASE + "/cal/al-1.ics", '"e"', self.ALARMS, datetime.datetime(2026, 11, 1), datetime.datetime(2026, 11, 9))[0]
		self.assertEqual(item["reminderMinutes"], [15, 60]) # RELATED=END: 2 h before the end, 1 h long
		self.assertEqual((item["icalColor"], item["visibility"]), ("teal", "public"))
		self.assertNotIn("transparency", item) # Busy, the default
		data = {"reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}, {"method": "popup", "minutes": 0},
			{"method": "email", "minutes": 99}]}, "caldavColor": "red", "transparency": "transparent", "visibility": "private"}
		_, body = caldav.build_updated_calendar(self.ALARMS, data, stamp=self.stamp)
		event = Calendar.from_ical(body).walk("VEVENT")[0]
		alarms = event.walk("VALARM")
		# The two shown as reminders replaced; the email and the one after the start kept.
		self.assertEqual(sorted((str(a["ACTION"]), a["TRIGGER"].dt) for a in alarms), [
			("DISPLAY", datetime.timedelta(minutes=-30)), ("DISPLAY", datetime.timedelta(0)),
			("DISPLAY", datetime.timedelta(minutes=5)), ("EMAIL", datetime.timedelta(days=-1))])
		self.assertEqual([str(a["DESCRIPTION"]) for a in alarms if a["TRIGGER"].dt == datetime.timedelta(minutes=-30)], ["Alarms"])
		self.assertEqual((str(event["COLOR"]), str(event["TRANSP"]), str(event["CLASS"])), ("red", "TRANSPARENT", "PRIVATE"))
		item = caldav.resource_items(BASE + "/cal/al-1.ics", '"e"', body, datetime.datetime(2026, 11, 1), datetime.datetime(2026, 11, 9))[0]
		self.assertEqual((item["reminderMinutes"], item["transparency"], item["visibility"], item["icalColor"]),
			([0, 30], "transparent", "private", "red"))
		# None of them, and the calendar's color: removed; untouched without the keys.
		_, body = caldav.build_updated_calendar(body, {"reminders": {"useDefault": False, "overrides": []},
			"caldavColor": None, "visibility": "default"}, stamp=self.stamp)
		event = Calendar.from_ical(body).walk("VEVENT")[0]
		self.assertEqual(len(event.walk("VALARM")), 2)
		self.assertNotIn("COLOR", event)
		self.assertNotIn("CLASS", event)
		_, body = caldav.build_updated_calendar(body, {"summary": "Only this"}, stamp=self.stamp)
		self.assertEqual(len(Calendar.from_ical(body).walk("VEVENT")[0].walk("VALARM")), 2)
		for bad in ({"caldavColor": "#ff0000"}, {"transparency": "busy"}, {"visibility": "secret"},
				{"reminders": {"overrides": [{"method": "popup", "minutes": -5}]}},
				{"reminders": {"overrides": [{"method": "popup", "minutes": m} for m in range(51)]}}):
			with self.assertRaises(caldav.CalDavError) as ctx:
				caldav.build_updated_calendar(self.ALARMS, bad, stamp=self.stamp)
			self.assertEqual(ctx.exception.kind, "INPUT", bad)

	def test_update_start_keeps_duration_and_recurrence_replacement(self):
		_, body = caldav.build_updated_calendar(self.EXISTING, {"start": {"dateTime": "2026-11-02T10:00:00Z"}})
		master = Calendar.from_ical(body).walk("VEVENT")[0]
		self.assertEqual(master["DTEND"].dt.isoformat(), "2026-11-02T11:00:00+00:00")
		self.assertIn("RRULE", master)  # recurrence absent: kept
		_, body = caldav.build_updated_calendar(self.EXISTING, {"recurrence": ["RRULE:FREQ=DAILY;COUNT=3", "EXDATE:20261103T080000Z"]})
		master = Calendar.from_ical(body).walk("VEVENT")[0]
		self.assertEqual(master["RRULE"]["FREQ"], ["DAILY"])
		self.assertIn(b"EXDATE:20261103T080000Z", body)
		_, body = caldav.build_updated_calendar(self.EXISTING, {"recurrence": []})
		master = Calendar.from_ical(body).walk("VEVENT")[0]
		self.assertNotIn("RRULE", master)
		self.assertNotIn("EXDATE", master)

	def test_put_create_and_update_requests(self):
		client = FakeClient([(201, {"ETag": '"new"'}, "")])
		result = caldav.put_event(client, BASE + "/cal", {"summary": "A", "start": {"date": "2026-11-05"}, "end": {"date": "2026-11-06"}})
		method, url, body, headers = client.sent[0]
		self.assertEqual(method, "PUT")
		self.assertEqual(url, BASE + "/cal/" + result["uid"] + ".ics")
		self.assertEqual(headers["If-None-Match"], "*")
		self.assertEqual(result, {"href": url, "etag": '"new"', "uid": result["uid"]})

		client = FakeClient([
			(200, {"ETag": '"v1"'}, self.EXISTING),
			(204, {}, ""),
			(207, {}, ms(resp("/cal/ex-1.ics", '<d:getetag>"v2"</d:getetag>'))),
		])
		result = caldav.put_event(client, BASE + "/cal/", {"summary": "B"}, href="/cal/ex-1.ics", etag='"v1"')
		self.assertEqual([s[0] for s in client.sent], ["GET", "PUT", "PROPFIND"])
		self.assertEqual(client.sent[1][3]["If-Match"], '"v1"')
		self.assertEqual(result, {"href": BASE + "/cal/ex-1.ics", "etag": '"v2"', "uid": "ex-1"})

	def test_put_stale_etag_is_conflict(self):
		client = FakeClient([(200, {}, self.EXISTING), (412, {}, "")])
		with self.assertRaises(caldav.CalDavError) as ctx:
			caldav.put_event(client, BASE + "/cal/", {"summary": "B"}, href=BASE + "/cal/ex-1.ics", etag="v0")
		self.assertEqual(ctx.exception.kind, "CONFLICT")
		self.assertEqual(client.sent[1][3]["If-Match"], '"v0"')

	def test_event_b64_on_command_line(self):
		event = {"summary": "Æ b64", "start": {"date": "2026-11-05"}, "end": {"date": "2026-11-06"}}
		encoded = base64.b64encode(json.dumps(event).encode("utf-8")).decode("ascii")
		sent = []

		class Recorder(FakeClient):
			def __init__(self, user, password):
				super().__init__([(201, {"ETag": '"x"'}, "")])
				sent.append(self)
		out = io.StringIO()
		with mock.patch.object(caldav, "Client", Recorder):
			code = caldav.main(["put", "--url", BASE + "/cal/", "--user", "u", "--password-stdin", "--event-b64", encoded],
				stdin=io.StringIO("pw\n"), stdout=out, stderr=io.StringIO())
		self.assertEqual(code, 0)
		self.assertIn("SUMMARY:Æ b64", sent[0].sent[0][2].decode("utf-8"))
		self.assertEqual(json.loads(out.getvalue())["etag"], '"x"')
		err = io.StringIO()
		code = caldav.main(["put", "--url", BASE + "/cal/", "--user", "u", "--password-stdin", "--event-b64", "!!"],
			stdin=io.StringIO("pw\n"), stdout=io.StringIO(), stderr=err)
		self.assertEqual(code, 1)
		self.assertTrue(err.getvalue().startswith("INPUT: "))

	def test_delete(self):
		client = FakeClient([(200, {}, self.EXISTING), (204, {}, "")])
		result = caldav.delete_event(client, BASE + "/cal/x.ics", '"e"')
		self.assertEqual([s[:2] for s in client.sent], [("GET", BASE + "/cal/x.ics"), ("DELETE", BASE + "/cal/x.ics")])
		self.assertEqual(client.sent[1][3]["If-Match"], '"e"')
		self.assertTrue(result["ok"])
		self.assertRegex(result["undo"], "^[0-9a-f]{32}$")

	def test_restore_puts_a_deleted_event_back_once(self):
		client = FakeClient([(200, {}, self.EXISTING), (204, {}, "")])
		token = caldav.delete_event(client, BASE + "/cal/x.ics", '"e"')["undo"]
		undo_file = os.path.join(caldav.undo_dir(), token + ".json")
		self.assertEqual(os.stat(undo_file).st_mode & 0o777, 0o600)
		self.assertEqual(os.stat(caldav.undo_dir()).st_mode & 0o777, 0o700)
		client = FakeClient([(201, {"ETag": '"back"'}, "")])
		self.assertEqual(caldav.restore_event(client, token), {"href": BASE + "/cal/x.ics", "etag": '"back"'})
		method, url, body, headers = client.sent[0]
		self.assertEqual((method, url), ("PUT", BASE + "/cal/x.ics"))
		self.assertEqual(headers["If-None-Match"], "*")
		self.assertEqual(body.decode("utf-8"), self.EXISTING)
		self.assertFalse(os.path.exists(undo_file))
		with self.assertRaises(caldav.CalDavError) as ctx:
			caldav.restore_event(FakeClient([]), token)
		self.assertEqual(ctx.exception.kind, "NOTFOUND")
		for bad in ("", "../x", "A" * 32):
			with self.assertRaises(caldav.CalDavError) as ctx:
				caldav.restore_event(FakeClient([]), bad)
			self.assertEqual(ctx.exception.kind, "INPUT")

	def test_move_to_another_calendar(self):
		client = FakeClient([(201, {}, ""), (207, {}, ms(resp("/other/x.ics", '<d:getetag>"m"</d:getetag>')))])
		result = caldav.move_event(client, BASE + "/cal/x.ics", '"e"', BASE + "/other")
		self.assertEqual(result, {"href": BASE + "/other/x.ics", "etag": '"m"'})
		method, url, _, headers = client.sent[0]
		self.assertEqual((method, url), ("MOVE", BASE + "/cal/x.ics"))
		self.assertEqual(headers["Destination"], BASE + "/other/x.ics")
		self.assertEqual(headers["Overwrite"], "F")
		self.assertEqual(headers["If-Match"], '"e"')
		for to in ("https://elsewhere.example.com/cal/", BASE + "/cal/"):
			with self.assertRaises(caldav.CalDavError) as ctx:
				caldav.move_event(FakeClient([]), BASE + "/cal/x.ics", '"e"', to)
			self.assertEqual(ctx.exception.kind, "INPUT")
		# Changed meanwhile, or the name taken there: a conflict, nothing else tried.
		client = FakeClient([(412, {}, "")])
		with self.assertRaises(caldav.CalDavError) as ctx:
			caldav.move_event(client, BASE + "/cal/x.ics", '"e"', BASE + "/other/")
		self.assertEqual(ctx.exception.kind, "CONFLICT")
		self.assertEqual(len(client.sent), 1)

	def test_move_without_server_support_copies_and_deletes(self):
		client = FakeClient([(405, {}, ""), (200, {}, self.EXISTING), (201, {"ETag": '"c"'}, ""), (204, {}, "")])
		result = caldav.move_event(client, BASE + "/cal/x.ics", '"e"', BASE + "/other/")
		self.assertEqual(result, {"href": BASE + "/other/x.ics", "etag": '"c"'})
		self.assertEqual([s[:2] for s in client.sent], [("MOVE", BASE + "/cal/x.ics"), ("GET", BASE + "/cal/x.ics"),
			("PUT", BASE + "/other/x.ics"), ("DELETE", BASE + "/cal/x.ics")])
		self.assertEqual(client.sent[2][3]["If-None-Match"], "*")
		self.assertEqual(client.sent[2][2].decode("utf-8"), self.EXISTING)
		self.assertEqual(client.sent[3][3]["If-Match"], '"e"')
		# 403 for MOVE (not between these calendars): copied too.
		client = FakeClient([(403, {}, ""), (200, {}, self.EXISTING), (201, {"ETag": '"c"'}, ""), (204, {}, "")])
		self.assertEqual(caldav.move_event(client, BASE + "/cal/x.ics", '"e"', BASE + "/other/")["href"], BASE + "/other/x.ics")
		# The original already gone: moved. No answer to its delete: the copy stays (never in neither).
		client = FakeClient([(405, {}, ""), (200, {}, self.EXISTING), (201, {"ETag": '"c"'}, ""), (404, {}, "")])
		self.assertEqual(caldav.move_event(client, BASE + "/cal/x.ics", '"e"', BASE + "/other/")["etag"], '"c"')
		client = FakeClient([(405, {}, ""), (200, {}, self.EXISTING), (201, {"ETag": '"c"'}, "")])
		client._send_orig = client._send

		def timeout_on_delete(method, url, body, headers, client=client):
			if method == "DELETE":
				client.sent.append((method, url, body, dict(headers or {})))
				raise caldav.CalDavError("NETWORK", "The server did not answer in time")
			return client._send_orig(method, url, body, headers)
		client._send = timeout_on_delete
		with self.assertRaises(caldav.CalDavError) as ctx:
			caldav.move_event(client, BASE + "/cal/x.ics", '"e"', BASE + "/other/")
		self.assertEqual(ctx.exception.kind, "NETWORK")
		self.assertEqual([m for m, *_ in client.sent].count("DELETE"), 1, "the copy isn't deleted")
		# The original changed meanwhile: the copy is removed again.
		client = FakeClient([(501, {}, ""), (200, {}, self.EXISTING), (201, {"ETag": '"c"'}, ""), (412, {}, ""), (204, {}, "")])
		with self.assertRaises(caldav.CalDavError) as ctx:
			caldav.move_event(client, BASE + "/cal/x.ics", '"e"', BASE + "/other/")
		self.assertEqual(ctx.exception.kind, "CONFLICT")
		self.assertEqual(client.sent[4][:2], ("DELETE", BASE + "/other/x.ics"))
		self.assertEqual(client.sent[4][3]["If-Match"], '"c"')

	def test_old_undo_files_are_removed(self):
		token = caldav.save_undo({"href": BASE + "/cal/x.ics", "body": "", "etag": ""})
		undo_file = os.path.join(caldav.undo_dir(), token + ".json")
		old = os.path.getmtime(undo_file) - caldav.UNDO_MAX_AGE - 10
		os.utime(undo_file, (old, old))
		caldav.save_undo({"href": BASE + "/cal/y.ics", "body": "", "etag": ""})
		self.assertFalse(os.path.exists(undo_file))


# Weekly on Mondays at 10:00 in Berlin from 19 Oct 2026, over the end of summer time (25 Oct): one
# date skipped (2 Nov, its EXDATE in UTC as Google writes them), one occurrence renamed (26 Oct)
# and one moved to 15:00 (9 Nov).
BERLIN = vcal(
	"BEGIN:VEVENT\r\nUID:berlin-1\r\nDTSTAMP:20261001T000000Z\r\nDTSTART;TZID=Europe/Berlin:20261019T100000\r\n"
	"DTEND;TZID=Europe/Berlin:20261019T110000\r\nRRULE:FREQ=WEEKLY;COUNT=6\r\nEXDATE:20261102T090000Z\r\n"
	"SUMMARY:Team\r\nLOCATION:Room 2\r\nSEQUENCE:1\r\n"
	"BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT10M\r\nDESCRIPTION:Team\r\nEND:VALARM\r\nEND:VEVENT\r\n"
	"BEGIN:VEVENT\r\nUID:berlin-1\r\nDTSTAMP:20261001T000000Z\r\nRECURRENCE-ID;TZID=Europe/Berlin:20261026T100000\r\n"
	"DTSTART;TZID=Europe/Berlin:20261026T100000\r\nDTEND;TZID=Europe/Berlin:20261026T110000\r\n"
	"SUMMARY:Team (renamed)\r\nLOCATION:Room 2\r\nEND:VEVENT\r\n"
	"BEGIN:VEVENT\r\nUID:berlin-1\r\nDTSTAMP:20261001T000000Z\r\nRECURRENCE-ID;TZID=Europe/Berlin:20261109T100000\r\n"
	"DTSTART;TZID=Europe/Berlin:20261109T150000\r\nDTEND;TZID=Europe/Berlin:20261109T160000\r\n"
	"SUMMARY:Team\r\nLOCATION:Room 2\r\nEND:VEVENT\r\n")

WEEKLY_ALLDAY = vcal(
	"BEGIN:VEVENT\r\nUID:allday-weekly\r\nDTSTAMP:20261001T000000Z\r\nDTSTART;VALUE=DATE:20261102\r\n"
	"DTEND;VALUE=DATE:20261103\r\nRRULE:FREQ=WEEKLY;COUNT=4\r\nSUMMARY:Bins\r\nEND:VEVENT\r\n")


def berlin(month, day, hour):
	"""A recurrence id in 2026 as a query gives it: {"dateTime", "timeZone"}."""
	moment = datetime.datetime(2026, month, day, hour, tzinfo=caldav_zone("Europe/Berlin"))
	return caldav.parse_when({"dateTime": moment.isoformat(), "timeZone": "Europe/Berlin"}, "recurrence-id")


def caldav_zone(name):
	import zoneinfo
	return zoneinfo.ZoneInfo(name)


def occurrences(body, start=datetime.datetime(2026, 10, 1), end=datetime.datetime(2026, 12, 31)):
	"""The resource's occurrences as the widget gets them: {local start: summary}."""
	items = caldav.resource_items(BASE + "/cal/r.ics", '"e"', body if isinstance(body, str) else body.decode("utf-8"), start, end)
	out = {}
	for item in items:
		when = item["start"].get("dateTime") or item["start"]["date"]
		if "T" in when:
			when = datetime.datetime.fromisoformat(when).astimezone(caldav_zone("Europe/Berlin")).strftime("%m-%d %H:%M")
		out[when] = item["summary"]
	return out


@unittest.skipUnless(HAS_RIE, "recurring-ical-events is not installed")
@mock.patch.object(caldav, "resolve_host", fake_resolve)
class RecurringEditTests(unittest.TestCase):
	stamp = datetime.datetime(2026, 11, 15, 12, 0, tzinfo=datetime.timezone.utc)

	def test_baseline(self):
		self.assertEqual(occurrences(BERLIN), {
			"10-19 10:00": "Team", "10-26 10:00": "Team (renamed)", "11-09 15:00": "Team",
			"11-16 10:00": "Team", "11-23 10:00": "Team",
		})

	def test_query_gives_each_occurrence_its_recurrence_id(self):
		items = caldav.resource_items(BASE + "/cal/r.ics", '"e"', BERLIN, datetime.datetime(2026, 11, 1), datetime.datetime(2026, 11, 30))
		ids = sorted(i["caldavRecurrenceId"]["dateTime"] for i in items)
		self.assertEqual(ids, ["2026-11-09T10:00:00+01:00", "2026-11-16T10:00:00+01:00", "2026-11-23T10:00:00+01:00"])
		self.assertTrue(all(i["caldavRecurrenceId"]["timeZone"] == "Europe/Berlin" for i in items))
		moved = [i for i in items if i["start"]["dateTime"].startswith("2026-11-09")][0]
		# Round trip: the id from the query finds the override again.
		rid = caldav.parse_when(moved["caldavRecurrenceId"], "recurrence-id")
		self.assertIsNotNone(caldav.find_override(Calendar.from_ical(BERLIN), rid))
		single = caldav.resource_items(BASE + "/cal/s.ics", '"e"', SINGLE, datetime.datetime(2026, 11, 1), datetime.datetime(2026, 11, 30))
		self.assertNotIn("caldavRecurrenceId", single[0])

	def test_change_one_occurrence_makes_an_override(self):
		_, body = caldav.build_updated_occurrence(BERLIN, {
			"summary": "Team (offsite)",
			"start": {"dateTime": "2026-11-16T14:00:00", "timeZone": "Europe/Berlin"},
			"end": {"dateTime": "2026-11-16T16:00:00", "timeZone": "Europe/Berlin"},
		}, berlin(11, 16, 10), stamp=self.stamp)
		cal = Calendar.from_ical(body)
		self.assertEqual(len(cal.walk("VEVENT")), 4)
		override = caldav.find_override(cal, berlin(11, 16, 10))
		self.assertIn(b"RECURRENCE-ID;TZID=Europe/Berlin:20261116T100000", body)
		self.assertNotIn("RRULE", override)
		self.assertNotIn("EXDATE", override)
		self.assertEqual(str(override["LOCATION"]), "Room 2")  # From the series
		self.assertEqual(len(override.walk("VALARM")), 1)
		self.assertEqual(int(override["SEQUENCE"]), 2)
		self.assertEqual(str(caldav.master_event(cal)["SUMMARY"]), "Team")  # The series unchanged
		got = occurrences(body)
		self.assertEqual(got["11-16 14:00"], "Team (offsite)")
		self.assertNotIn("11-16 10:00", got)
		self.assertEqual(got["11-23 10:00"], "Team")

	def test_change_one_occurrence_given_in_utc_or_with_only_its_text(self):
		utc = berlin(11, 16, 10).astimezone(datetime.timezone.utc)
		_, body = caldav.build_updated_occurrence(BERLIN, {"summary": "Short"}, utc)
		self.assertIn(b"RECURRENCE-ID;TZID=Europe/Berlin:20261116T100000", body)  # The series' form
		self.assertEqual(occurrences(body)["11-16 10:00"], "Short")

	def test_change_an_overridden_occurrence_changes_its_override(self):
		_, body = caldav.build_updated_occurrence(BERLIN, {"location": "Cafe"}, berlin(11, 9, 10))
		cal = Calendar.from_ical(body)
		self.assertEqual(len(cal.walk("VEVENT")), 3)
		override = caldav.find_override(cal, berlin(11, 9, 10))
		self.assertEqual(str(override["LOCATION"]), "Cafe")
		self.assertEqual(override["DTSTART"].dt.hour, 15)  # Its own time kept

	def test_one_occurrence_has_no_rule_and_needs_a_series(self):
		with self.assertRaises(caldav.CalDavError) as ctx:
			caldav.build_updated_occurrence(BERLIN, {"recurrence": ["RRULE:FREQ=DAILY"]}, berlin(11, 16, 10))
		self.assertEqual(ctx.exception.kind, "INPUT")
		with self.assertRaises(caldav.CalDavError) as ctx:
			caldav.build_updated_occurrence(SINGLE, {"summary": "x"}, berlin(11, 16, 10))
		self.assertEqual(ctx.exception.kind, "PROTOCOL")

	def test_delete_one_occurrence(self):
		body = caldav.build_deleted_occurrence(BERLIN, berlin(11, 16, 10)).decode("utf-8")
		master = caldav.master_event(Calendar.from_ical(body))
		self.assertEqual(len(caldav.date_values(master, "EXDATE")), 2)
		self.assertIn("EXDATE;TZID=Europe/Berlin:20261116T100000", body)
		self.assertNotIn("11-16 10:00", occurrences(body))
		# An overridden one: its override goes too.
		body = caldav.build_deleted_occurrence(body, berlin(11, 9, 10)).decode("utf-8")
		self.assertEqual(len(Calendar.from_ical(body).walk("VEVENT")), 2)
		self.assertEqual(sorted(occurrences(body)), ["10-19 10:00", "10-26 10:00", "11-23 10:00"])
		# Deleted twice: no second EXDATE.
		again = caldav.build_deleted_occurrence(body, berlin(11, 9, 10))
		self.assertEqual(len(caldav.date_values(caldav.master_event(Calendar.from_ical(again)), "EXDATE")), 3)

	def test_delete_one_all_day_occurrence(self):
		body = caldav.build_deleted_occurrence(WEEKLY_ALLDAY, caldav.parse_when({"date": "2026-11-09"}, "rid"))
		self.assertIn(b"EXDATE;VALUE=DATE:20261109", body)
		self.assertEqual(sorted(occurrences(body)), ["2026-11-02", "2026-11-16", "2026-11-23"])

	def test_series_moved_takes_its_exdates_and_overrides_along(self):
		# Every occurrence a day later at 11:00, over the end of summer time.
		_, body = caldav.build_updated_calendar(BERLIN, {
			"start": {"dateTime": "2026-10-20T11:00:00", "timeZone": "Europe/Berlin"},
			"end": {"dateTime": "2026-10-20T12:00:00", "timeZone": "Europe/Berlin"},
		}, stamp=self.stamp)
		self.assertEqual(occurrences(body), {
			"10-20 11:00": "Team",
			"10-27 11:00": "Team (renamed)",  # Unmoved override: follows the series
			"11-09 15:00": "Team",  # Moved on its own: keeps its time
			"11-17 11:00": "Team", "11-24 11:00": "Team",
		})  # 3 Nov stays skipped
		cal = Calendar.from_ical(body)
		self.assertTrue(caldav.find_override(cal, berlin(11, 10, 11)))
		self.assertTrue(caldav.find_override(cal, berlin(10, 27, 11)))

	def test_series_length_change_reaches_unmoved_overrides(self):
		_, body = caldav.build_updated_calendar(BERLIN, {
			"end": {"dateTime": "2026-10-19T12:30:00", "timeZone": "Europe/Berlin"},
		})
		cal = Calendar.from_ical(body)
		renamed = caldav.find_override(cal, berlin(10, 26, 10))
		moved = caldav.find_override(cal, berlin(11, 9, 10))
		self.assertEqual(renamed["DTEND"].dt.strftime("%H:%M"), "12:30")
		self.assertEqual(moved["DTEND"].dt.strftime("%H:%M"), "16:00")

	def test_series_text_reaches_overrides_that_kept_it(self):
		_, body = caldav.build_updated_calendar(BERLIN, {"summary": "Weekly team", "location": "Room 3"})
		got = occurrences(body)
		self.assertEqual(got["11-09 15:00"], "Weekly team")
		self.assertEqual(got["10-26 10:00"], "Team (renamed)")  # Its own title kept
		renamed = caldav.find_override(Calendar.from_ical(body), berlin(10, 26, 10))
		self.assertEqual(str(renamed["LOCATION"]), "Room 3")

	def test_series_to_all_day(self):
		_, body = caldav.build_updated_calendar(BERLIN, {"start": {"date": "2026-10-19"}, "end": {"date": "2026-10-20"}})
		got = occurrences(body)
		# The occurrence moved to 15:00 on its own keeps its time; the rest become whole days.
		self.assertEqual(sorted(got), ["11-09 15:00", "2026-10-19", "2026-10-26", "2026-11-16", "2026-11-23"])
		self.assertIn(b"EXDATE;VALUE=DATE:20261102", body)
		self.assertIn(b"RECURRENCE-ID;VALUE=DATE:20261109", body)
		self.assertIn(b"RECURRENCE-ID;VALUE=DATE:20261026", body)

	def test_series_moved_keeps_instances_at_other_hours(self):
		# Daily at 9:00 and 15:00 (BYHOUR); 15:00 on the 5th skipped, 15:00 on the 6th changed.
		series = vcal(
			"BEGIN:VEVENT\r\nUID:twice\r\nDTSTART;TZID=Europe/Berlin:20260105T090000\r\nDTEND;TZID=Europe/Berlin:20260105T093000\r\n"
			"RRULE:FREQ=DAILY;BYHOUR=9,15;COUNT=6\r\nEXDATE;TZID=Europe/Berlin:20260105T150000\r\nSUMMARY:Pills\r\nEND:VEVENT\r\n"
			"BEGIN:VEVENT\r\nUID:twice\r\nRECURRENCE-ID;TZID=Europe/Berlin:20260106T150000\r\n"
			"DTSTART;TZID=Europe/Berlin:20260106T150000\r\nDTEND;TZID=Europe/Berlin:20260106T153000\r\nSUMMARY:Pills (with food)\r\nEND:VEVENT\r\n")
		start, end = datetime.datetime(2026, 1, 1), datetime.datetime(2026, 1, 31)
		self.assertEqual(occurrences(series, start, end), {
			"01-05 09:00": "Pills", "01-06 09:00": "Pills", "01-06 15:00": "Pills (with food)",
			"01-07 09:00": "Pills", "01-07 15:00": "Pills",
		})
		# The series a day later, same hours: the skipped and changed 15:00 instances move a day.
		_, body = caldav.build_updated_calendar(series, {
			"start": {"dateTime": "2026-01-06T09:00:00", "timeZone": "Europe/Berlin"},
			"end": {"dateTime": "2026-01-06T09:30:00", "timeZone": "Europe/Berlin"},
		})
		self.assertIn(b"EXDATE;TZID=Europe/Berlin:20260106T150000", body)
		self.assertEqual(occurrences(body, start, end), {
			"01-06 09:00": "Pills", "01-07 09:00": "Pills", "01-07 15:00": "Pills (with food)",
			"01-08 09:00": "Pills", "01-08 15:00": "Pills",
		})

	def test_series_moved_takes_rdates_and_until_along(self):
		series = vcal(
			"BEGIN:VEVENT\r\nUID:until\r\nDTSTART;TZID=Europe/Berlin:20260105T090000\r\nDTEND;TZID=Europe/Berlin:20260105T100000\r\n"
			"RRULE:FREQ=DAILY;UNTIL=20260107T080000Z\r\nRDATE;TZID=Europe/Berlin:20260120T120000\r\nSUMMARY:Daily\r\nEND:VEVENT\r\n"
			"BEGIN:VEVENT\r\nUID:until\r\nRECURRENCE-ID;TZID=Europe/Berlin:20260120T120000\r\n"
			"DTSTART;TZID=Europe/Berlin:20260120T120000\r\nDTEND;TZID=Europe/Berlin:20260120T130000\r\nSUMMARY:Extra (changed)\r\nEND:VEVENT\r\n")
		start, end = datetime.datetime(2026, 1, 1), datetime.datetime(2026, 1, 31)
		self.assertEqual(occurrences(series, start, end), {
			"01-05 09:00": "Daily", "01-06 09:00": "Daily", "01-07 09:00": "Daily", "01-20 12:00": "Extra (changed)",
		})
		# An hour later: the last day stays (UNTIL moves too), the extra date moves with its change.
		_, body = caldav.build_updated_calendar(series, {
			"start": {"dateTime": "2026-01-05T10:00:00", "timeZone": "Europe/Berlin"},
			"end": {"dateTime": "2026-01-05T11:00:00", "timeZone": "Europe/Berlin"},
		})
		self.assertIn(b"UNTIL=20260107T090000Z", body)
		self.assertEqual(occurrences(body, start, end), {
			"01-05 10:00": "Daily", "01-06 10:00": "Daily", "01-07 10:00": "Daily", "01-20 13:00": "Extra (changed)",
		})
		# A new rule from the editor keeps its own UNTIL.
		_, body = caldav.build_updated_calendar(series, {
			"start": {"dateTime": "2026-01-05T10:00:00", "timeZone": "Europe/Berlin"},
			"end": {"dateTime": "2026-01-05T11:00:00", "timeZone": "Europe/Berlin"},
			"recurrence": ["RRULE:FREQ=DAILY;UNTIL=20260106T090000Z"],
		})
		self.assertIn(b"UNTIL=20260106T090000Z", body)

	def test_series_that_no_longer_repeats_loses_its_overrides(self):
		_, body = caldav.build_updated_calendar(BERLIN, {"recurrence": []})
		cal = Calendar.from_ical(body)
		self.assertEqual(len(cal.walk("VEVENT")), 1)
		self.assertNotIn("RRULE", cal.walk("VEVENT")[0])
		items = caldav.resource_items(BASE + "/cal/r.ics", '"e"', body.decode("utf-8"), datetime.datetime(2026, 10, 1), datetime.datetime(2026, 12, 1))
		self.assertEqual([i["summary"] for i in items], ["Team"])
		self.assertFalse(items[0]["recurring"])

	def test_new_rule_drops_the_instances_it_doesnt_produce(self):
		_, body = caldav.build_updated_calendar(BERLIN, {"recurrence": ["RRULE:FREQ=WEEKLY;COUNT=2", "EXDATE:20261102T090000Z"]})
		cal = Calendar.from_ical(body)
		self.assertEqual(len(caldav.overrides(cal)), 1)
		self.assertNotIn("EXDATE", caldav.master_event(cal))
		self.assertEqual(occurrences(body), {"10-19 10:00": "Team", "10-26 10:00": "Team (renamed)"})
		# The same rule again: everything stays.
		_, body = caldav.build_updated_calendar(BERLIN, {"recurrence": ["RRULE:FREQ=WEEKLY;COUNT=6", "EXDATE:20261102T090000Z"]})
		self.assertEqual(len(caldav.overrides(Calendar.from_ical(body))), 2)
		self.assertIn(b"EXDATE", body)

	def test_series_extras_reach_overrides_that_kept_them(self):
		# 26 Oct's override kept the series' alarm (copied); 9 Nov's got its own color.
		body = BERLIN.replace("SUMMARY:Team\r\nLOCATION:Room 2\r\nEND:VEVENT\r\nEND:VCALENDAR",
			"SUMMARY:Team\r\nLOCATION:Room 2\r\nCOLOR:teal\r\nEND:VEVENT\r\nEND:VCALENDAR")
		body = body.replace("SUMMARY:Team (renamed)\r\nLOCATION:Room 2\r\n",
			"SUMMARY:Team (renamed)\r\nLOCATION:Room 2\r\nBEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT10M\r\nDESCRIPTION:x\r\nEND:VALARM\r\n")
		_, out = caldav.build_updated_calendar(body, {"reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 60}]},
			"caldavColor": "red", "transparency": "transparent"}, stamp=datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc))
		master, renamed, moved = Calendar.from_ical(out).walk("VEVENT")
		minutes = lambda e: [a["TRIGGER"].dt for a in e.walk("VALARM")]
		self.assertEqual(minutes(master), [datetime.timedelta(minutes=-60)])
		self.assertEqual(minutes(renamed), [datetime.timedelta(minutes=-60)], "had the series' alarm")
		self.assertEqual(minutes(moved), [], "had none of its own: the series' old one was 10 min, not none")
		self.assertEqual((str(master["COLOR"]), str(renamed["COLOR"]), str(moved["COLOR"])), ("red", "red", "teal"))
		self.assertEqual([str(e["TRANSP"]) for e in (master, renamed, moved)], ["TRANSPARENT"] * 3)
		self.assertEqual(int(moved["SEQUENCE"]), 1, "changed: stamped")

	def test_changed_overrides_are_stamped(self):
		_, body = caldav.build_updated_calendar(BERLIN, {
			"start": {"dateTime": "2026-10-20T11:00:00", "timeZone": "Europe/Berlin"},
			"end": {"dateTime": "2026-10-20T12:00:00", "timeZone": "Europe/Berlin"},
		}, stamp=self.stamp)
		for override in caldav.overrides(Calendar.from_ical(body)):
			self.assertEqual(int(override["SEQUENCE"]), 1)
			self.assertEqual(override["LAST-MODIFIED"].dt, self.stamp)

	def test_overrides_without_their_series_are_read_only(self):
		invited = vcal(
			"BEGIN:VEVENT\r\nUID:inv\r\nRECURRENCE-ID:20261103T080000Z\r\nDTSTART:20261103T090000Z\r\n"
			"DTEND:20261103T100000Z\r\nSUMMARY:Invited once\r\nEND:VEVENT\r\n")
		items = caldav.resource_items(BASE + "/cal/inv.ics", '"e"', invited, datetime.datetime(2026, 11, 1), datetime.datetime(2026, 11, 30))
		self.assertEqual(len(items), 1)
		self.assertTrue(items[0]["caldavReadOnly"])
		items = caldav.resource_items(BASE + "/cal/r.ics", '"e"', BERLIN, datetime.datetime(2026, 11, 1), datetime.datetime(2026, 11, 30))
		self.assertFalse(any("caldavReadOnly" in i for i in items))

	def test_get_series(self):
		client = FakeClient([(200, {"ETag": '"v1"'}, BERLIN)])
		series = caldav.get_series(client, BASE + "/cal/berlin-1.ics")
		self.assertEqual(series["id"], "berlin-1")
		self.assertEqual(series["start"], {"dateTime": "2026-10-19T10:00:00+02:00", "timeZone": "Europe/Berlin"})
		self.assertEqual(series["recurrence"], ["RRULE:FREQ=WEEKLY;COUNT=6", "EXDATE:20261102T090000Z"])
		self.assertEqual((series["caldavHref"], series["caldavEtag"]), (BASE + "/cal/berlin-1.ics", '"v1"'))
		# A single event too (eg: changed right after a move, before it's loaded there).
		single = caldav.get_series(FakeClient([(200, {"ETag": '"v1"'}, SINGLE)]), BASE + "/cal/single-1.ics")
		self.assertEqual((single["id"], single["caldavEtag"]), ("single-1", '"v1"'))
		self.assertNotIn("recurrence", single)

	def test_occurrence_requests(self):
		rid = berlin(11, 16, 10)
		client = FakeClient([(200, {}, BERLIN), (204, {"ETag": '"v2"'}, "")])
		result = caldav.put_event(client, BASE + "/cal/", {"summary": "x"}, href="/cal/berlin-1.ics", etag='"v1"', recurrence_id=rid)
		self.assertEqual([s[0] for s in client.sent], ["GET", "PUT"])
		self.assertEqual(client.sent[1][3]["If-Match"], '"v1"')
		self.assertEqual(result["etag"], '"v2"')
		client = FakeClient([(200, {}, BERLIN), (204, {"ETag": '"v3"'}, "")])
		result = caldav.delete_event(client, BASE + "/cal/berlin-1.ics", '"v2"', rid)
		self.assertEqual([s[0] for s in client.sent], ["GET", "PUT"])
		self.assertEqual(client.sent[1][3]["If-Match"], '"v2"')
		token = result.pop("undo")
		self.assertEqual(result, {"ok": True, "href": BASE + "/cal/berlin-1.ics", "etag": '"v3"'})
		# Undo: the series as it was, unless it changed since the delete.
		client = FakeClient([(204, {"ETag": '"v4"'}, "")])
		self.assertEqual(caldav.restore_event(client, token)["etag"], '"v4"')
		self.assertEqual(client.sent[0][3]["If-Match"], '"v3"')
		self.assertNotIn("If-None-Match", client.sent[0][3])
		self.assertEqual(client.sent[0][2].decode("utf-8"), BERLIN)
		with self.assertRaises(caldav.CalDavError) as ctx:
			caldav.put_event(FakeClient([]), BASE + "/cal/", {"summary": "x"}, recurrence_id=rid)
		self.assertEqual(ctx.exception.kind, "INPUT")

	def test_recurrence_id_on_the_command_line(self):
		sent = []

		class Recorder(FakeClient):
			def __init__(self, user, password):
				super().__init__([(200, {}, BERLIN), (204, {"ETag": '"v2"'}, "")])
				sent.append(self)
		rid = json.dumps({"dateTime": "2026-11-16T10:00:00+01:00", "timeZone": "Europe/Berlin"})
		with mock.patch.object(caldav, "Client", Recorder):
			code = caldav.main(["delete", "--href", BASE + "/cal/berlin-1.ics", "--etag", '"v1"', "--recurrence-id=" + rid,
				"--user", "u", "--password-stdin"], stdin=io.StringIO("pw\n"), stdout=io.StringIO(), stderr=io.StringIO())
		self.assertEqual(code, 0)
		self.assertIn("EXDATE;TZID=Europe/Berlin:20261116T100000", sent[0].sent[1][2].decode("utf-8"))
		err = io.StringIO()
		code = caldav.main(["delete", "--href", BASE + "/cal/x.ics", "--etag", "e", "--recurrence-id=nope",
			"--user", "u", "--password-stdin"], stdin=io.StringIO("pw\n"), stdout=io.StringIO(), stderr=err)
		self.assertEqual(code, 1)
		self.assertTrue(err.getvalue().startswith("INPUT: "), err.getvalue())


IMPORT_ICS = (
	"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nMETHOD:PUBLISH\r\n"
	"BEGIN:VTIMEZONE\r\nTZID:Custom/Zone\r\nBEGIN:STANDARD\r\nDTSTART:19700101T000000\r\nTZOFFSETFROM:+0100\r\n"
	"TZOFFSETTO:+0100\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n"
	"BEGIN:VTIMEZONE\r\nTZID:Unused/Zone\r\nBEGIN:STANDARD\r\nDTSTART:19700101T000000\r\nTZOFFSETFROM:+0300\r\n"
	"TZOFFSETTO:+0300\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n"
	"BEGIN:VEVENT\r\nUID:rec@example.com\r\nDTSTART;TZID=Custom/Zone:20261102T090000\r\nDTEND;TZID=Custom/Zone:20261102T100000\r\n"
	"RRULE:FREQ=DAILY;COUNT=5\r\nSUMMARY:Series\r\nEND:VEVENT\r\n"
	"BEGIN:VEVENT\r\nUID:plain-1\r\nDTSTART;VALUE=DATE:20261110\r\nSUMMARY:Plain\r\nEND:VEVENT\r\n"
	"BEGIN:VEVENT\r\nUID:rec@example.com\r\nRECURRENCE-ID;TZID=Custom/Zone:20261103T090000\r\n"
	"DTSTART;TZID=Custom/Zone:20261103T150000\r\nDTEND;TZID=Custom/Zone:20261103T160000\r\nSUMMARY:Series moved\r\nEND:VEVENT\r\n"
	"BEGIN:VEVENT\r\nUID:dup\r\nDTSTART:20261105T100000Z\r\nSUMMARY:Exists\r\nEND:VEVENT\r\n"
	"END:VCALENDAR\r\n"
)


@mock.patch.object(caldav, "resolve_host", fake_resolve)
class ImportTests(unittest.TestCase):
	def test_grouping_by_uid_with_overrides_and_timezones(self):
		groups, failed = caldav.group_import(Calendar.from_ical(IMPORT_ICS))
		self.assertEqual(failed, [])
		self.assertEqual([uid for uid, _ in groups], ["rec@example.com", "plain-1", "dup"])
		series = groups[0][1]
		self.assertEqual(len(series.walk("VEVENT")), 2)
		self.assertEqual([str(tz["TZID"]) for tz in series.walk("VTIMEZONE")], ["Custom/Zone"])
		self.assertNotIn("METHOD", series)
		self.assertEqual(groups[1][1].walk("VTIMEZONE"), [])

	def test_resource_names(self):
		self.assertEqual(caldav.resource_name("plain-1"), "plain-1")
		name = caldav.resource_name("rec@example.com")
		self.assertRegex(name, r"^rec_example\.com-[0-9a-f]{12}$")
		self.assertNotEqual(caldav.resource_name("a/b"), caldav.resource_name("a_b"))
		self.assertRegex(caldav.resource_name("../../etc"), r"^[A-Za-z0-9._-]+$")
		self.assertNotIn("/", caldav.resource_name("../../etc"))

	def test_import_puts_each_uid_and_reports_existing(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = pathlib.Path(tmp) / "in.ics"
			path.write_text(IMPORT_ICS)
			client = FakeClient([(201, {}, ""), (201, {}, ""), (412, {}, "")])
			result = caldav.import_file(client, BASE + "/cal/", "file://" + str(path))
		self.assertEqual(result, {"imported": 2, "failed": [{"uid": "dup", "error": "exists"}]})
		self.assertTrue(all(s[0] == "PUT" and s[3]["If-None-Match"] == "*" for s in client.sent))
		self.assertEqual(client.sent[1][1], BASE + "/cal/plain-1.ics")
		self.assertEqual(client.sent[0][2].count(b"BEGIN:VEVENT"), 2)

	def test_import_bad_file(self):
		client = FakeClient([])
		for path in ("/nonexistent/x.ics", "/etc/hostname"):
			with self.assertRaises(caldav.CalDavError) as ctx:
				caldav.import_file(client, BASE + "/cal/", path)
			self.assertEqual(ctx.exception.kind, "INPUT")

	def test_import_stops_on_auth_error(self):
		with tempfile.TemporaryDirectory() as tmp:
			path = pathlib.Path(tmp) / "in.ics"
			path.write_text(IMPORT_ICS)
			client = FakeClient([(401, {}, "")])
			with self.assertRaises(caldav.CalDavError) as ctx:
				caldav.import_file(client, BASE + "/cal/", str(path))
		self.assertEqual(ctx.exception.kind, "AUTH")


if __name__ == "__main__":
	unittest.main()
