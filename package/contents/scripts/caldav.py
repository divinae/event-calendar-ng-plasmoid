"""CalDAV client for the widget: discover calendars, query, create/update/delete and import events.

python3 caldav.py <command> ... prints JSON on stdout and exits 0. On failure it exits 1 with one
line on stderr, "KIND: message", KIND being AUTH, NOTFOUND, CONFLICT, NETWORK, PROTOCOL, INPUT,
DEPS or KEYRING (the keyring couldn't be read, eg: not up yet at login; try again later).

Commands (all take --account ID --user USERNAME):
	discover --url URL
	query --url CALENDAR START END            (YYYY-MM-DD, END exclusive)
	put --url CALENDAR [--href RES --etag E [--recurrence-id RID]] [--event-b64 B64]
	                                          (event JSON as base64 of UTF-8, else on stdin)
	delete --href RES --etag E [--recurrence-id RID]
	                                          (gives "undo": a token for restore)
	restore --undo TOKEN                      (puts a deleted event or occurrence back)
	move --href RES --etag E --to CALENDAR    (an event or series to another calendar, same server)
	get --href RES                            (an event, or a repeating event's series, as an import gives it)
	import --url CALENDAR --file PATH

Repeating events: a query gives each occurrence its caldavRecurrenceId (the original start, as
{"date"} or {"dateTime", "timeZone"}). put / delete with --recurrence-id (that JSON) change or
remove only that occurrence (an override in the same resource, or an EXDATE); without it they
change or delete the whole series.

The password comes from the keyring (secret_store.py, scope "caldav-account", key "password",
account_id ID). For tests and command-line use only, --password-stdin reads it from the first
line of stdin instead (for put, the event JSON follows on the next lines). The password is never
taken from argv and never printed.
"""

import argparse
import base64
import copy
import datetime
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import time
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
if SCRIPT_DIR not in sys.path:
	sys.path.insert(0, SCRIPT_DIR)

# The .ics modules (icalendar, recurring-ical-events): missing (an install from the KDE Store), every
# command answers "DEPS: ..." (the widget offers to install them), not a traceback.
try:
	import icsjson  # noqa: E402  (also puts the bundled icalendar / recurring_ical_events on sys.path)
	from icalendar import Calendar, Event  # noqa: E402
except ImportError as import_error:
	icsjson = None
	Calendar = Event = None
	MISSING_MODULE = getattr(import_error, "name", None) or str(import_error)
else:
	MISSING_MODULE = ""

REQUEST_TIMEOUT = 20
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 5
REDIRECT_CODES = (301, 302, 307, 308)
USER_AGENT = "eventcalendar-ng-caldav/1.0"
PRODID = "-//Event Calendar NG//CalDAV//EN"
SECRET_SCOPE = "caldav-account"
SECRET_KEY = "password"

NS_DAV = "DAV:"
NS_CALDAV = "urn:ietf:params:xml:ns:caldav"
NS_CS = "http://calendarserver.org/ns/"
NS_ICAL = "http://apple.com/ns/ical/"
PREFIXES = {NS_DAV: "d", NS_CALDAV: "c", NS_CS: "cs", NS_ICAL: "ical"}

ERROR_KINDS = ("AUTH", "NOTFOUND", "CONFLICT", "NETWORK", "PROTOCOL", "INPUT", "DEPS", "KEYRING")


def tag(ns, name):
	return "{%s}%s" % (ns, name)


class CalDavError(Exception):
	def __init__(self, kind, message):
		assert kind in ERROR_KINDS
		super().__init__(message)
		self.kind = kind
		self.message = message

	def line(self):
		return "{}: {}".format(self.kind, " ".join(str(self.message).split()))


def url_path(url):
	"""A URL's path only, for messages (no host, no userinfo, no query)."""
	try:
		return urllib.parse.urlsplit(url).path or "/"
	except ValueError:
		return "?"


def error_for_status(status, url):
	path = url_path(url)
	if status in (401, 403):
		return CalDavError("AUTH", "Access denied (HTTP {}) for {}".format(status, path))
	if status in (404, 410):
		return CalDavError("NOTFOUND", "Not found (HTTP {}): {}".format(status, path))
	if status == 412:
		return CalDavError("CONFLICT", "Changed on the server (HTTP 412): {}".format(path))
	return CalDavError("PROTOCOL", "Unexpected HTTP {} for {}".format(status, path))


#--- URL rules

def resolve_host(hostname):
	return [info[4][0] for info in socket.getaddrinfo(hostname, None)]


def is_local_address(address):
	ip = ipaddress.ip_address(address.split("%", 1)[0])
	if ip.version == 6 and ip.ipv4_mapped is not None:
		ip = ip.ipv4_mapped
	return ip.is_loopback or ip.is_private


def check_url(url):
	"""Normalized absolute URL (no userinfo, no fragment) or CalDavError INPUT/NETWORK.

	https anywhere; plain http only when every address of the host is loopback or private."""
	if not isinstance(url, str) or not url.strip():
		raise CalDavError("INPUT", "URL is empty")
	try:
		parts = urllib.parse.urlsplit(url.strip())
		port = parts.port
	except ValueError:
		raise CalDavError("INPUT", "Invalid URL")
	scheme = parts.scheme.lower()
	if scheme not in ("http", "https"):
		raise CalDavError("INPUT", "URL must use https")
	host = parts.hostname
	if not host:
		raise CalDavError("INPUT", "URL has no host")
	if scheme == "http":
		try:
			addresses = resolve_host(host)
		except (socket.gaierror, UnicodeError, OSError) as error:
			raise CalDavError("NETWORK", "Cannot resolve host: {}".format(getattr(error, "strerror", None) or error))
		try:
			local = bool(addresses) and all(is_local_address(a) for a in addresses)
		except ValueError:
			local = False
		if not local:
			raise CalDavError("INPUT", "http only for local servers")
	netloc = "[{}]".format(host) if ":" in host else host
	if port:
		netloc += ":{}".format(port)
	return urllib.parse.urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def origin(url):
	parts = urllib.parse.urlsplit(url)
	scheme = parts.scheme.lower()
	port = parts.port or (443 if scheme == "https" else 80)
	return scheme, (parts.hostname or "").lower(), port


def check_redirect(old_url, new_url):
	if origin(old_url) != origin(new_url):
		raise CalDavError("PROTOCOL", "Redirect to another server refused ({})".format(url_path(new_url)))


#--- HTTP

class _NoRedirect(urllib.request.HTTPRedirectHandler):
	def redirect_request(self, req, fp, code, msg, headers, newurl):
		return None  # handled in Client.request (keeps the method and body)


class Client:
	def __init__(self, user, password):
		self.user = user or ""
		token = base64.b64encode("{}:{}".format(self.user, password or "").encode("utf-8")).decode("ascii")
		self._auth = "Basic " + token
		self._opener = urllib.request.build_opener(_NoRedirect)

	def _send(self, method, url, body, headers):
		"""(status, headers, data) for any HTTP status; CalDavError NETWORK when there's no answer."""
		request = urllib.request.Request(url, data=body, method=method)
		request.add_header("User-Agent", USER_AGENT)
		request.add_header("Authorization", self._auth)
		for key, value in (headers or {}).items():
			request.add_header(key, value)
		try:
			response = self._opener.open(request, timeout=REQUEST_TIMEOUT)
		except urllib.error.HTTPError as error:
			try:
				data = error.read(MAX_RESPONSE_BYTES + 1) if error.fp is not None else b""
			except Exception:
				data = b""
			return error.code, error.headers, data
		except urllib.error.URLError as error:
			reason = error.reason
			text = getattr(reason, "strerror", None) or str(reason)
			raise CalDavError("NETWORK", "Cannot reach the server: {}".format(text))
		except (socket.timeout, TimeoutError):
			raise CalDavError("NETWORK", "The server did not answer in time")
		except OSError as error:
			raise CalDavError("NETWORK", "Cannot reach the server: {}".format(error.strerror or error))
		with response:
			try:
				data = response.read(MAX_RESPONSE_BYTES + 1)
			except (socket.timeout, TimeoutError):
				raise CalDavError("NETWORK", "The server did not answer in time")
			except OSError as error:
				raise CalDavError("NETWORK", "Connection failed: {}".format(error.strerror or error))
			return response.status, response.headers, data

	def request(self, method, url, body=None, headers=None, ok=(200, 201, 204, 207)):
		"""(status, headers, data, final_url) following same-server redirects; other statuses raise."""
		url = check_url(url)
		if isinstance(body, str):
			body = body.encode("utf-8")
		for _ in range(MAX_REDIRECTS + 1):
			status, response_headers, data = self._send(method, url, body, headers)
			if status in REDIRECT_CODES:
				location = response_headers.get("Location") if response_headers is not None else None
				if not location:
					raise CalDavError("PROTOCOL", "Redirect without a location for {}".format(url_path(url)))
				new_url = urllib.parse.urljoin(url, location.strip())
				check_redirect(url, new_url)
				url = check_url(new_url)
				continue
			if status not in ok:
				raise error_for_status(status, url)
			if data is not None and len(data) > MAX_RESPONSE_BYTES:
				raise CalDavError("PROTOCOL", "Response exceeds the size limit")
			return status, response_headers, data or b"", url
		raise CalDavError("PROTOCOL", "Too many redirects for {}".format(url_path(url)))

	def propfind(self, url, props, depth):
		_, _, data, final_url = self.request(
			"PROPFIND", url, propfind_body(props),
			{"Depth": str(depth), "Content-Type": "application/xml; charset=utf-8"},
			ok=(207,),
		)
		return parse_multistatus(data, final_url), final_url


#--- XML

def xml_name(ns, name):
	return "{}:{}".format(PREFIXES[ns], name)


def xml_namespaces():
	return " ".join('xmlns:{}="{}"'.format(prefix, ns) for ns, prefix in PREFIXES.items())


def propfind_body(props):
	inner = "".join("<{}/>".format(xml_name(ns, name)) for ns, name in props)
	return '<?xml version="1.0" encoding="utf-8"?>\n<d:propfind {}><d:prop>{}</d:prop></d:propfind>'.format(
		xml_namespaces(), inner)


def utc_stamp(value):
	return value.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def calendar_query_body(start_utc, end_utc):
	return (
		'<?xml version="1.0" encoding="utf-8"?>\n'
		'<c:calendar-query {}>'
		'<d:prop><d:getetag/><c:calendar-data/></d:prop>'
		'<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
		'<c:time-range start="{}" end="{}"/>'
		'</c:comp-filter></c:comp-filter></c:filter>'
		'</c:calendar-query>'
	).format(xml_namespaces(), utc_stamp(start_utc), utc_stamp(end_utc))


_FORBIDDEN_XML = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def parse_xml(data):
	if not data:
		raise CalDavError("PROTOCOL", "Empty XML response")
	if _FORBIDDEN_XML.search(data):
		raise CalDavError("PROTOCOL", "XML with a DOCTYPE is not accepted")
	try:
		return ET.fromstring(data)
	except ET.ParseError as error:
		raise CalDavError("PROTOCOL", "Invalid XML: {}".format(error))


_OK_STATUS = re.compile(r"\s2\d\d(\s|$)")


def parse_multistatus(data, base_url):
	"""[{"href": absolute url, "props": {tag: element}}] with the 2xx propstats of each response."""
	root = parse_xml(data)
	if root.tag != tag(NS_DAV, "multistatus"):
		raise CalDavError("PROTOCOL", "Expected a multistatus response")
	results = []
	for response in root.findall(tag(NS_DAV, "response")):
		href = response.findtext(tag(NS_DAV, "href"))
		if not href or not href.strip():
			continue
		props = {}
		for propstat in response.findall(tag(NS_DAV, "propstat")):
			status = propstat.findtext(tag(NS_DAV, "status")) or ""
			if not _OK_STATUS.search(" " + status.strip()):
				continue
			prop = propstat.find(tag(NS_DAV, "prop"))
			if prop is None:
				continue
			for child in prop:
				props[child.tag] = child
		results.append({"href": urllib.parse.urljoin(base_url, href.strip()), "props": props})
	return results


def child_href(element, base_url):
	if element is None:
		return ""
	href = element.findtext(tag(NS_DAV, "href"))
	return urllib.parse.urljoin(base_url, href.strip()) if href and href.strip() else ""


def prop_text(props, key):
	element = props.get(key)
	if element is None or element.text is None:
		return ""
	return element.text.strip()


def same_resource(a, b):
	return urllib.parse.urlsplit(a).path.rstrip("/") == urllib.parse.urlsplit(b).path.rstrip("/")


#--- discover

def normalize_color(value):
	value = (value or "").strip()
	if re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", value):
		return value[:7].lower()
	if re.fullmatch(r"#[0-9a-fA-F]{3}", value):
		return "#" + "".join(c * 2 for c in value[1:]).lower()
	return ""


def privileges_writable(element):
	"""None when the server didn't say, else whether write / write-content / all is granted."""
	if element is None:
		return None
	writes = {tag(NS_DAV, "write"), tag(NS_DAV, "write-content"), tag(NS_DAV, "all")}
	for privilege in element.iter(tag(NS_DAV, "privilege")):
		for child in privilege:
			if child.tag in writes:
				return True
	return False


def calendar_from_response(entry):
	props = entry["props"]
	resourcetype = props.get(tag(NS_DAV, "resourcetype"))
	if resourcetype is None or resourcetype.find(tag(NS_CALDAV, "calendar")) is None:
		return None
	components = []
	component_set = props.get(tag(NS_CALDAV, "supported-calendar-component-set"))
	if component_set is not None:
		for comp in component_set.findall(tag(NS_CALDAV, "comp")):
			name = (comp.get("name") or "").strip().upper()
			if name:
				components.append(name)
	if components and "VEVENT" not in components:
		return None
	name = prop_text(props, tag(NS_DAV, "displayname"))
	if not name:
		name = urllib.parse.unquote(urllib.parse.urlsplit(entry["href"]).path.rstrip("/").rsplit("/", 1)[-1])
	writable = privileges_writable(props.get(tag(NS_DAV, "current-user-privilege-set")))
	ctag = ctag_of(props)
	return {
		"href": entry["href"],
		"name": name,
		"color": normalize_color(prop_text(props, tag(NS_ICAL, "calendar-color"))),
		"writable": True if writable is None else writable,
		"components": components,
		"ctag": ctag,
	}


CALENDAR_PROPS = [
	(NS_DAV, "resourcetype"),
	(NS_DAV, "displayname"),
	(NS_ICAL, "calendar-color"),
	(NS_CALDAV, "supported-calendar-component-set"),
	(NS_DAV, "current-user-privilege-set"),
	(NS_CS, "getctag"),
	(NS_DAV, "sync-token"),
]


def parse_calendar_list(entries, home_url):
	calendars = []
	for entry in entries:
		if same_resource(entry["href"], home_url):
			continue
		calendar = calendar_from_response(entry)
		if calendar is not None:
			calendars.append(calendar)
	return calendars


def find_principal(client, url):
	first_error = None
	candidates = [url]
	scheme, netloc = urllib.parse.urlsplit(url)[:2]
	well_known = "{}://{}/.well-known/caldav".format(scheme, netloc)
	if not same_resource(url, well_known):
		candidates.append(well_known)
	for candidate in candidates:
		try:
			entries, final_url = client.propfind(candidate, [(NS_DAV, "current-user-principal")], 0)
		except CalDavError as error:
			if error.kind in ("AUTH", "NETWORK"):
				raise
			first_error = first_error or error
			continue
		for entry in entries:
			principal = child_href(entry["props"].get(tag(NS_DAV, "current-user-principal")), final_url)
			if principal:
				return principal
	if first_error is not None and first_error.kind != "NOTFOUND":
		raise first_error
	raise CalDavError("NOTFOUND", "No CalDAV principal found at this address")


def discover(client, url):
	url = check_url(url)
	principal = find_principal(client, url)
	entries, final_url = client.propfind(principal, [(NS_CALDAV, "calendar-home-set")], 0)
	home = ""
	for entry in entries:
		home = child_href(entry["props"].get(tag(NS_CALDAV, "calendar-home-set")), final_url)
		if home:
			break
	if not home:
		raise CalDavError("NOTFOUND", "The principal has no calendar home")
	entries, final_url = client.propfind(home, CALENDAR_PROPS, 1)
	return {"principal": principal, "home": home, "calendars": parse_calendar_list(entries, final_url)}


#--- query

def is_recurring_component(event):
	return any(event.get(key) is not None for key in ("RRULE", "RDATE", "RECURRENCE-ID"))


def resource_is_recurring(cal):
	return any(is_recurring_component(event) for event in cal.walk("VEVENT"))


def resource_occurrences(cal, start, end):
	if icsjson.recurring_ical_events is not None:
		return list(icsjson.recurring_ical_events.of(cal).between(start, end))
	occurrences = []
	for event in cal.walk("VEVENT"):
		if not is_recurring_component(event) and icsjson.event_within(event, start, end):
			occurrences.append(event)
	return occurrences


def resource_items(href, etag, calendar_data, start, end):
	"""The widget's items (icsjson format) for one CalDAV resource's occurrences in [start, end)."""
	try:
		cal = Calendar.from_ical(calendar_data)
	except Exception as error:
		raise CalDavError("PROTOCOL", "Invalid calendar data in {}: {}".format(url_path(href), error))
	recurring = resource_is_recurring(cal)
	# Overrides without their series (eg: an invitation to one instance): nothing to change them in.
	master = master_event(cal)
	no_series = recurring and (master is None or not is_recurring_component(master))
	items = []
	for occurrence in resource_occurrences(cal, start, end):
		converted = json.loads(icsjson.events_to_json([occurrence], indent=None))["items"]
		for item in converted:
			item["caldavHref"] = href
			item["caldavEtag"] = etag
			item["caldavUid"] = icsjson.stringify(occurrence.get("UID")).strip()
			item["recurring"] = recurring
			if recurring:
				item["recurringEventId"] = item["caldavUid"]
				# The occurrence's original start: what put / delete --recurrence-id take.
				recurrence_id = icsjson.get_event_date(occurrence, "RECURRENCE-ID")
				if recurrence_id is not None:
					item["caldavRecurrenceId"] = icsjson.raw_date_to_json(recurrence_id)
				if no_series:
					item["caldavReadOnly"] = True
			items.append(item)
	return items


def query_items(entries, start, end):
	items = []
	seen = set()
	for entry in entries:
		data = prop_text(entry["props"], tag(NS_CALDAV, "calendar-data"))
		if not data:
			continue
		etag = prop_text(entry["props"], tag(NS_DAV, "getetag"))
		for item in resource_items(entry["href"], etag, data, start, end):
			if item["id"] in seen:
				continue
			seen.add(item["id"])
			items.append(item)
	return items


def local_midnight_utc(day):
	return datetime.datetime.combine(day, datetime.time.min).astimezone(datetime.timezone.utc)


def ctag_of(props):
	"""A calendar's change tag in its PROPFIND props: CalendarServer's getctag, else the sync-token."""
	return prop_text(props, tag(NS_CS, "getctag")) or prop_text(props, tag(NS_DAV, "sync-token"))


def calendar_ctag(client, url):
	"""The calendar's change tag (CalendarServer's getctag, else the sync-token), "" when the server has
	neither or the request fails: the events are then always fetched."""
	try:
		entries, _ = client.propfind(url, [(NS_CS, "getctag"), (NS_DAV, "sync-token")], 0)
	except CalDavError as err:
		if err.kind in ("AUTH", "NETWORK"): # Not the events either: no second wait for them
			raise
		return ""
	for entry in entries:
		ctag = ctag_of(entry["props"])
		if ctag:
			return ctag
	return ""


def query(client, url, start, end, if_ctag="", probe_ctag=True):
	"""start/end: naive datetimes (local midnight), end exclusive. if_ctag: the calendar's change tag
	at the last fetch of the same range: unchanged, the events aren't downloaded again ({"unchanged":
	true}). The tag is read before the events, so a change meanwhile is seen next time."""
	ctag = calendar_ctag(client, url) if probe_ctag else ""
	if if_ctag and ctag and ctag == if_ctag:
		return {"unchanged": True, "ctag": ctag}
	# One day of margin on both sides: servers compare floating and all-day events in UTC.
	range_start = local_midnight_utc(start.date()) - datetime.timedelta(days=1)
	range_end = local_midnight_utc(end.date()) + datetime.timedelta(days=1)
	_, _, data, final_url = client.request(
		"REPORT", url, calendar_query_body(range_start, range_end),
		{"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
		ok=(207,),
	)
	return {"items": query_items(parse_multistatus(data, final_url), start, end), "ctag": ctag}


#--- put

def parse_when(value, key):
	if not isinstance(value, dict):
		raise CalDavError("INPUT", "{} must be an object".format(key))
	if "date" in value and value["date"] is not None:
		try:
			return datetime.date.fromisoformat(str(value["date"]))
		except ValueError:
			raise CalDavError("INPUT", "{}.date is not YYYY-MM-DD".format(key))
	if "dateTime" in value and value["dateTime"] is not None:
		text = str(value["dateTime"]).strip()
		if text.endswith("Z") or text.endswith("z"):
			text = text[:-1] + "+00:00"
		try:
			moment = datetime.datetime.fromisoformat(text)
		except ValueError:
			raise CalDavError("INPUT", "{}.dateTime is not an ISO date-time".format(key))
		zone_name = value.get("timeZone")
		if zone_name:
			import zoneinfo
			try:
				zone = zoneinfo.ZoneInfo(str(zone_name))
			except (zoneinfo.ZoneInfoNotFoundError, ValueError):
				raise CalDavError("INPUT", "Unknown time zone: {}".format(zone_name))
			if moment.tzinfo is None:
				return moment.replace(tzinfo=zone)
			return moment.astimezone(zone)
		if moment.tzinfo is not None:
			return moment.astimezone(datetime.timezone.utc)
		return moment  # floating time
	raise CalDavError("INPUT", "{} needs date or dateTime".format(key))


RECURRENCE_PROPS = ("RRULE", "RDATE", "EXDATE")


def parse_recurrence(lines):
	"""Google-style lines ("RRULE:...", "EXDATE;VALUE=DATE:...") as [(name, value)]."""
	if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
		raise CalDavError("INPUT", "recurrence must be a list of strings")
	props = []
	for line in lines:
		line = line.strip()
		if not line:
			continue
		name = re.split(r"[:;]", line, maxsplit=1)[0].upper()
		if name not in RECURRENCE_PROPS or "\n" in line or "\r" in line:
			raise CalDavError("INPUT", "recurrence lines must be RRULE, RDATE or EXDATE")
		try:
			parsed = Event.from_ical("BEGIN:VEVENT\r\n{}\r\nEND:VEVENT\r\n".format(line))
			value = parsed.get(name)
		except Exception as error:
			raise CalDavError("INPUT", "Invalid recurrence line: {}".format(error))
		if value is None or getattr(parsed, "errors", None):
			raise CalDavError("INPUT", "Invalid recurrence line: {}".format(line))
		for item in (value if isinstance(value, list) else [value]):
			props.append((name, item))
	return props


def set_recurrence(event, lines):
	"""Replace the event's RRULE / RDATE / EXDATE (an empty list makes it non-recurring)."""
	props = parse_recurrence(lines)
	for name in RECURRENCE_PROPS:
		event.pop(name, None)
	for name, value in props:
		event.add(name, value)


def is_all_day(value):
	return isinstance(value, datetime.date) and not isinstance(value, datetime.datetime)


def set_text(event, key, value):
	if value is None:
		return
	if not isinstance(value, str):
		raise CalDavError("INPUT", "{} must be a string".format(key.lower()))
	event.pop(key, None)
	if value != "" or key == "SUMMARY":
		event.add(key, value)


def set_date(event, key, value):
	event.pop(key, None)
	event.add(key, value)


def apply_event_fields(event, data):
	"""Change only the given fields of a VEVENT (summary, location, description, start, end)."""
	set_text(event, "SUMMARY", data.get("summary"))
	set_text(event, "LOCATION", data.get("location"))
	set_text(event, "DESCRIPTION", data.get("description"))
	if data.get("recurrence") is not None:
		set_recurrence(event, data["recurrence"])
	start = parse_when(data["start"], "start") if data.get("start") is not None else None
	end = parse_when(data["end"], "end") if data.get("end") is not None else None
	if start is not None and end is not None:
		if is_all_day(start) != is_all_day(end):
			raise CalDavError("INPUT", "start and end must both be dates or both date-times")
		if icsjson.ensure_date_time(end) < icsjson.ensure_date_time(start) or (is_all_day(start) and end <= start):
			raise CalDavError("INPUT", "end is before start")
	if start is not None:
		old_start = icsjson.get_event_date(event, "DTSTART")
		if end is None and event.get("DTEND") is not None and old_start is not None:
			# Keep the duration when only the start moves (and the kind of value matches).
			old_end = icsjson.get_event_date(event, "DTEND")
			if is_all_day(old_start) == is_all_day(start) and old_end is not None:
				end = start + (old_end - old_start)
		set_date(event, "DTSTART", start)
	if end is not None:
		event.pop("DURATION", None)
		set_date(event, "DTEND", end)


def now_utc():
	return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def add_timezones(cal):
	if hasattr(cal, "add_missing_timezones"):
		try:
			cal.add_missing_timezones()
		except Exception:
			pass


def build_new_calendar(data, uid=None, stamp=None):
	for key in ("summary", "start", "end"):
		if data.get(key) is None:
			raise CalDavError("INPUT", "A new event needs {}".format(key))
	uid = uid or str(uuid.uuid4())
	stamp = stamp or now_utc()
	cal = Calendar()
	cal.add("PRODID", PRODID)
	cal.add("VERSION", "2.0")
	event = Event()
	event.add("UID", uid)
	event.add("DTSTAMP", stamp)
	event.add("CREATED", stamp)
	event.add("LAST-MODIFIED", stamp)
	apply_event_fields(event, data)
	cal.add_component(event)
	add_timezones(cal)
	return uid, cal.to_ical()


def master_event(cal):
	for event in cal.walk("VEVENT"):
		if event.get("RECURRENCE-ID") is None:
			return event
	return None


def parse_existing(existing):
	try:
		return Calendar.from_ical(existing)
	except Exception as error:
		raise CalDavError("PROTOCOL", "Invalid calendar data on the server: {}".format(error))


def touch(event, stamp):
	"""A changed VEVENT: new DTSTAMP / LAST-MODIFIED, SEQUENCE one up."""
	for key in ("DTSTAMP", "LAST-MODIFIED"):
		event.pop(key, None)
		event.add(key, stamp)
	try:
		sequence = int(str(event.get("SEQUENCE", 0)))
	except ValueError:
		sequence = 0
	event.pop("SEQUENCE", None)
	event.add("SEQUENCE", sequence + 1)


TEXT_FIELDS = (("summary", "SUMMARY"), ("location", "LOCATION"), ("description", "DESCRIPTION"))


def build_updated_calendar(existing, data, stamp=None):
	"""The whole event or series changed (the master VEVENT). Its instances move with a new start
	(shift_instances), and overrides that kept the old title, place or notes get the new ones."""
	cal = parse_existing(existing)
	event = master_event(cal)
	if event is None:
		raise CalDavError("PROTOCOL", "The resource has no master event")
	stamp = stamp or now_utc()
	old_bounds = icsjson.get_event_bounds(event)
	old_texts = {key: icsjson.stringify(event.get(key)) for _, key in TEXT_FIELDS}
	apply_event_fields(event, data)
	touch(event, stamp)
	new_bounds = icsjson.get_event_bounds(event)
	if not is_recurring_component(event):
		# No repeat any more (recurrence []): its overrides would stay as stray events.
		for override in overrides(cal):
			cal.subcomponents.remove(override)
	changed = set()
	for override in overrides(cal):
		for name, key in TEXT_FIELDS:
			if data.get(name) is not None and icsjson.stringify(override.get(key)) == old_texts[key]:
				set_text(override, key, data[name])
				changed.add(id(override))
	if None not in old_bounds and None not in new_bounds and old_bounds != new_bounds:
		# A new rule from the editor (recurrence given) has the end the user chose: UNTIL stays.
		changed |= shift_instances(cal, event, old_bounds, new_bounds, shift_until=data.get("recurrence") is None)
	if data.get("recurrence") is not None and is_recurring_component(event):
		# A new rule: overrides and EXDATEs of instances it doesn't produce would stay as stray events.
		exdates = date_values(event, "EXDATE")
		kept = rule_instances(event, exdates)
		if len(kept) != len(exdates):
			set_date_values(event, "EXDATE", kept)
		for override in overrides(cal):
			rid = icsjson.get_event_date(override, "RECURRENCE-ID")
			if not rule_instances(event, [rid]):
				cal.subcomponents.remove(override)
	for override in overrides(cal):
		if id(override) in changed:
			touch(override, stamp)
	add_timezones(cal)
	uid = icsjson.stringify(event.get("UID")).strip()
	return uid, cal.to_ical()


#--- repeating events: one occurrence, or the series' instances moved

def overrides(cal):
	return [event for event in cal.walk("VEVENT") if event.get("RECURRENCE-ID") is not None]


def local_date(value, zone):
	"""The day of a date, or of a time in zone (the series' zone; floating times as they are)."""
	if not isinstance(value, datetime.datetime):
		return value
	if value.tzinfo is not None and zone is not None:
		return value.astimezone(zone).date()
	return value.date()


def same_instance(a, b):
	"""Two RECURRENCE-ID / EXDATE values name the same instance: the same day for dates, else the
	same moment (floating times by their wall clock)."""
	if is_all_day(a) or is_all_day(b):
		return local_date(a, getattr(b, "tzinfo", None)) == local_date(b, getattr(a, "tzinfo", None))
	if (a.tzinfo is None) != (b.tzinfo is None):
		return a.replace(tzinfo=None) == b.replace(tzinfo=None)
	return a == b


def like_start(value, start):
	"""value in the form of the series' DTSTART (a date, a time in its zone, or a floating time), as
	RFC 5545 wants RECURRENCE-ID and EXDATE."""
	if is_all_day(start):
		return local_date(value, None)
	if is_all_day(value):
		return datetime.datetime.combine(value, start.time()).replace(tzinfo=start.tzinfo)
	if start.tzinfo is None:
		return value.replace(tzinfo=None)
	if value.tzinfo is None:
		return value.replace(tzinfo=start.tzinfo)
	return value.astimezone(start.tzinfo)


def wall_clock(value, zone):
	"""A time as the wall clock shows it in zone (the series' zone; floating times as they are)."""
	if value.tzinfo is not None and zone is not None:
		return value.astimezone(zone).replace(tzinfo=None)
	return value.replace(tzinfo=None)


def moved_instance(value, old_start, new_start):
	"""An instance moved as the series' start moved from old_start to new_start, by wall clock in
	the series' zone (a daylight saving change doesn't shift the hour); whole days by days."""
	old_zone = getattr(old_start, "tzinfo", None)
	new_zone = getattr(new_start, "tzinfo", None)
	if is_all_day(old_start) or is_all_day(new_start) or is_all_day(value):
		day = local_date(value, old_zone) + (local_date(new_start, new_zone) - local_date(old_start, old_zone))
		if is_all_day(new_start):
			return day
		return datetime.datetime.combine(day, new_start.time()).replace(tzinfo=new_zone)
	moved = wall_clock(value, old_zone) + (wall_clock(new_start, new_zone) - wall_clock(old_start, old_zone))
	return moved.replace(tzinfo=new_zone)


def date_entries(event, key):
	"""Every value of an EXDATE or RDATE property (one or several lines): dates, times, and for RDATE
	maybe periods ((start, end or duration) tuples)."""
	prop = event.get(key)
	if prop is None:
		return []
	values = []
	for item in (prop if isinstance(prop, list) else [prop]):
		for entry in getattr(item, "dts", []):
			values.append(getattr(entry, "dt", None))
	return values


def date_values(event, key):
	"""The dates / times of an EXDATE or RDATE property."""
	return [value for value in date_entries(event, key) if isinstance(value, datetime.date)]


def moved_until(rule, old_start, new_start):
	"""The RRULE's UNTIL moved like the instances (it's often the last instance's start exactly), in
	the form RFC 5545 wants: a date for a series of whole days, UTC for one in a zone."""
	until = rule.get("UNTIL")
	if not until:
		return
	value = until[0] if isinstance(until, list) else until
	moved = moved_instance(value, old_start, new_start)
	if isinstance(moved, datetime.datetime) and moved.tzinfo is not None:
		moved = moved.astimezone(datetime.timezone.utc)
	rule["UNTIL"] = [moved]


def set_date_values(event, key, values):
	event.pop(key, None)
	for value in values:
		# Dates need VALUE=DATE (RFC 5545), which icalendar writes for DTSTART but not for lists.
		event.add(key, value, parameters={"VALUE": "DATE"} if is_all_day(value) else None)


def shift_instances(cal, master, old_bounds, new_bounds, shift_until=True):
	"""The series' (start, end) changed: EXDATEs, RDATEs, UNTIL (with shift_until) and RECURRENCE-IDs
	move along; overrides not moved on their own follow. Returns the ids of the overrides changed."""
	old_start, old_end = old_bounds
	new_start, new_end = new_bounds
	moves = old_start != new_start
	changed = set()
	if moves:
		set_date_values(master, "EXDATE", [moved_instance(v, old_start, new_start) for v in date_values(master, "EXDATE")])
		rdates = date_entries(master, "RDATE")
		if rdates and all(isinstance(v, datetime.date) for v in rdates):  # Periods are left as they are
			set_date_values(master, "RDATE", [moved_instance(v, old_start, new_start) for v in rdates])
		rules = master.get("RRULE")
		if shift_until and rules is not None:
			for rule in (rules if isinstance(rules, list) else [rules]):
				moved_until(rule, old_start, new_start)
	for override in overrides(cal):
		old_id = icsjson.get_event_date(override, "RECURRENCE-ID")
		new_id = moved_instance(old_id, old_start, new_start) if moves else old_id
		start, end = icsjson.get_event_bounds(override)
		unmoved = start is not None and end is not None and same_instance(start, old_id) \
			and is_all_day(start) == is_all_day(old_start) and end - start == old_end - old_start
		if moves:
			set_date(override, "RECURRENCE-ID", new_id)
			changed.add(id(override))
		if unmoved:
			override.pop("DURATION", None)
			set_date(override, "DTSTART", new_id)
			set_date(override, "DTEND", new_id + (new_end - new_start))
			changed.add(id(override))
	return changed


def rule_instances(master, values):
	"""The values (RECURRENCE-IDs, EXDATEs) that are instances of the master's rule; all of them
	when that can't be told."""
	if icsjson.recurring_ical_events is None or Calendar is None or not values:
		return list(values)
	only = Calendar()
	rule = copy.deepcopy(master)
	rule.pop("EXDATE", None)
	only.add_component(rule)
	kept = []
	for value in values:
		step = datetime.timedelta(days=1) if is_all_day(value) else datetime.timedelta(seconds=1)
		try:
			found = icsjson.recurring_ical_events.of(only).between(value, value + step)
		except Exception:
			kept.append(value)
			continue
		if any(same_instance(icsjson.get_event_date(o, "DTSTART"), value) for o in found):
			kept.append(value)
	return kept


def parse_recurrence_id(text):
	"""--recurrence-id: the occurrence's caldavRecurrenceId JSON from a query."""
	try:
		value = json.loads(text)
	except ValueError:
		raise CalDavError("INPUT", "--recurrence-id is not JSON")
	return parse_when(value, "recurrence-id")


def find_override(cal, recurrence_id):
	for event in overrides(cal):
		if same_instance(icsjson.get_event_date(event, "RECURRENCE-ID"), recurrence_id):
			return event
	return None


def series_parts(cal):
	"""(master, its DTSTART) of a repeating event's resource."""
	master = master_event(cal)
	start = icsjson.get_event_date(master, "DTSTART") if master is not None else None
	if master is None or start is None or not is_recurring_component(master):
		raise CalDavError("PROTOCOL", "The resource has no repeating event")
	return master, start


def new_override(master, recurrence_id):
	"""An override for one instance: a copy of the series without its rule, at that instance."""
	start, end = icsjson.get_event_bounds(master)
	override = copy.deepcopy(master)
	for key in ("RRULE", "RDATE", "EXDATE", "EXRULE", "DTSTART", "DTEND", "DURATION", "RECURRENCE-ID", "CREATED"):
		override.pop(key, None)
	override.add("RECURRENCE-ID", recurrence_id)
	override.add("DTSTART", recurrence_id)
	override.add("DTEND", recurrence_id + (end - start))
	return override


def build_updated_occurrence(existing, data, recurrence_id, stamp=None):
	"""Only one occurrence changed: its override (made from the series if there is none yet)."""
	if data.get("recurrence") is not None:
		raise CalDavError("INPUT", "One occurrence has no repeat rule of its own")
	cal = parse_existing(existing)
	master, start = series_parts(cal)
	recurrence_id = like_start(recurrence_id, start)
	override = find_override(cal, recurrence_id)
	if override is None:
		override = new_override(master, recurrence_id)
		cal.add_component(override)
	apply_event_fields(override, data)
	touch(override, stamp or now_utc())
	add_timezones(cal)
	return icsjson.stringify(master.get("UID")).strip(), cal.to_ical()


def build_deleted_occurrence(existing, recurrence_id, stamp=None):
	"""One occurrence deleted: an EXDATE on the series, and its override (if any) removed."""
	cal = parse_existing(existing)
	master, start = series_parts(cal)
	recurrence_id = like_start(recurrence_id, start)
	override = find_override(cal, recurrence_id)
	if override is not None:
		cal.subcomponents.remove(override)
	exdates = date_values(master, "EXDATE")
	if not any(same_instance(value, recurrence_id) for value in exdates):
		set_date_values(master, "EXDATE", exdates + [recurrence_id])
	touch(master, stamp or now_utc())
	add_timezones(cal)
	return cal.to_ical()


def quote_etag(etag):
	etag = (etag or "").strip()
	if not etag:
		raise CalDavError("INPUT", "--etag is empty")
	if etag.startswith('"') or etag.startswith("W/"):
		return etag
	return '"{}"'.format(etag)


def collection_url(url):
	return url if url.endswith("/") else url + "/"


def fetch_etag(client, href):
	entries, _ = client.propfind(href, [(NS_DAV, "getetag")], 0)
	for entry in entries:
		etag = prop_text(entry["props"], tag(NS_DAV, "getetag"))
		if etag:
			return etag
	return ""


def put_calendar(client, href, body, headers):
	headers = dict(headers)
	headers["Content-Type"] = "text/calendar; charset=utf-8"
	_, response_headers, _, final_url = client.request("PUT", href, body, headers, ok=(200, 201, 204))
	etag = (response_headers.get("ETag") or "").strip() if response_headers is not None else ""
	if not etag:
		etag = fetch_etag(client, final_url)
	return final_url, etag


def put_event(client, cal_url, data, href=None, etag=None, recurrence_id=None):
	"""Create an event, or change one (href + etag): with recurrence_id only that occurrence of its
	series, else the whole event or series."""
	if not isinstance(data, dict):
		raise CalDavError("INPUT", "The event must be a JSON object")
	cal_url = check_url(cal_url)
	if href:
		href = check_url(urllib.parse.urljoin(collection_url(cal_url), href))
		match = quote_etag(etag)
		_, _, existing, href = client.request("GET", href, ok=(200,))
		if recurrence_id is not None:
			uid, body = build_updated_occurrence(existing, data, recurrence_id)
		else:
			uid, body = build_updated_calendar(existing, data)
		final_url, new_etag = put_calendar(client, href, body, {"If-Match": match})
	else:
		if recurrence_id is not None:
			raise CalDavError("INPUT", "--recurrence-id needs --href")
		uid, body = build_new_calendar(data)
		target = collection_url(cal_url) + resource_name(uid) + ".ics"
		final_url, new_etag = put_calendar(client, target, body, {"If-None-Match": "*"})
	return {"href": final_url, "etag": new_etag, "uid": uid}


def delete_event(client, href, etag, recurrence_id=None):
	"""Delete an event or a whole series; with recurrence_id only that occurrence (the series stays,
	changed: its new etag is returned). "undo": a token for restore_event (the resource as it was)."""
	href = check_url(href)
	match = quote_etag(etag)
	_, _, existing, href = client.request("GET", href, ok=(200,))
	if recurrence_id is None:
		client.request("DELETE", href, None, {"If-Match": match}, ok=(200, 202, 204))
		return {"ok": True, "undo": save_undo({"href": href, "body": existing.decode("utf-8", "replace"), "etag": ""})}
	final_url, new_etag = put_calendar(client, href, build_deleted_occurrence(existing, recurrence_id), {"If-Match": match})
	undo = save_undo({"href": final_url, "body": existing.decode("utf-8", "replace"), "etag": new_etag})
	return {"ok": True, "href": final_url, "etag": new_etag, "undo": undo}


def move_event(client, href, etag, to_url):
	"""Move an event or series (its resource) to another calendar of the same server: WebDAV MOVE,
	or where the server can't (405, 501, ...), a copy there and the original deleted."""
	href = check_url(href)
	match = quote_etag(etag)
	to_url = collection_url(check_url(to_url))
	if origin(href) != origin(to_url):
		raise CalDavError("INPUT", "--to is on another server")
	target = to_url + href.rstrip("/").rsplit("/", 1)[-1]
	if same_resource(href, target):
		raise CalDavError("INPUT", "The event is already in that calendar")
	try:
		client.request("MOVE", href, None, {
			"Destination": target, "Overwrite": "F", "If-Match": match}, ok=(201, 204))
	except CalDavError as error:
		if error.kind != "PROTOCOL":
			raise
		return copy_and_delete(client, href, match, target)
	return {"href": target, "etag": fetch_etag(client, target)}


def copy_and_delete(client, href, match, target):
	_, _, existing, href = client.request("GET", href, ok=(200,))
	# Changed meanwhile: the DELETE's If-Match fails, and the copy goes again.
	final_url, new_etag = put_calendar(client, target, existing, {"If-None-Match": "*"})
	try:
		client.request("DELETE", href, None, {"If-Match": match}, ok=(200, 202, 204))
	except CalDavError:
		try:  # Not in both calendars
			client.request("DELETE", final_url, None, {"If-Match": quote_etag(new_etag)} if new_etag else {}, ok=(200, 202, 204))
		except CalDavError:
			pass
		raise
	return {"href": final_url, "etag": new_etag}


#--- Undo of deletes: the resource as it was, kept for a day in a private file (the user's runtime
# directory: memory only, gone at logout), only its token goes to the widget.

UNDO_MAX_AGE = 24 * 3600
UNDO_TOKEN = re.compile(r"[0-9a-f]{32}")


def undo_dir():
	base = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "plasma-eventcalendar")
	path = os.path.join(base, "eventcalendar-caldav-undo")
	os.makedirs(path, mode=0o700, exist_ok=True)
	return path


def prune_undo(path, now):
	for name in os.listdir(path):
		try:
			if now - os.path.getmtime(os.path.join(path, name)) > UNDO_MAX_AGE:
				os.remove(os.path.join(path, name))
		except OSError:
			pass


def save_undo(entry):
	"""The token, or "" when it can't be kept (no undo then, the delete still counts)."""
	try:
		path = undo_dir()
		prune_undo(path, time.time())
		token = secrets.token_hex(16)
		fd = os.open(os.path.join(path, token + ".json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
		with os.fdopen(fd, "w", encoding="utf-8") as out:
			json.dump(entry, out)
		return token
	except OSError:
		return ""


def restore_event(client, token):
	"""Put a deleted event back as it was: a deleted resource again at its href (If-None-Match), a
	deleted occurrence by the series before its EXDATE (If-Match: unless it changed since)."""
	if not UNDO_TOKEN.fullmatch(token or ""):
		raise CalDavError("INPUT", "Not an undo token")
	file_path = os.path.join(undo_dir(), token + ".json")
	try:
		with open(file_path, encoding="utf-8") as source:
			entry = json.load(source)
	except (OSError, ValueError):
		raise CalDavError("NOTFOUND", "Nothing to undo (too old, or already undone)")
	href = check_url(entry["href"])
	headers = {"If-Match": quote_etag(entry["etag"])} if entry.get("etag") else {"If-None-Match": "*"}
	final_url, etag = put_calendar(client, href, entry["body"], headers)
	try:
		os.remove(file_path)
	except OSError:
		pass
	return {"href": final_url, "etag": etag}


def get_series(client, href):
	"""A series (or a single event) in Google style (as the editor takes it), with href and etag."""
	href = check_url(href)
	_, headers, existing, href = client.request("GET", href, ok=(200,))
	master = master_event(parse_existing(existing))
	series = icsjson.event_to_raw(master) if master is not None else None
	if series is None:
		raise CalDavError("PROTOCOL", "The series has no start")
	etag = (headers.get("ETag") or "").strip() if headers is not None else ""
	series["id"] = series["iCalUID"]
	series["caldavHref"] = href
	series["caldavEtag"] = etag or fetch_etag(client, href)
	return series


#--- import

def resource_name(uid):
	"""A safe file name for a UID ("<name>.ics" on the server)."""
	if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", uid) and not uid.endswith("."):
		return uid
	digest = hashlib.sha1(uid.encode("utf-8", "ignore")).hexdigest()[:12]
	safe = re.sub(r"[^A-Za-z0-9._-]+", "_", uid).strip("._-")[:60]
	return "{}-{}".format(safe, digest) if safe else digest


def used_tzids(component):
	tzids = []
	for part in component.walk():
		for _, value in part.property_items(recursive=False):
			params = getattr(value, "params", None)
			if params is None:
				continue
			tzid = params.get("TZID")
			if tzid and str(tzid) not in tzids:
				tzids.append(str(tzid))
	return tzids


def group_import(cal):
	"""([(uid, Calendar)], failed): one VCALENDAR per UID with its overrides and VTIMEZONEs."""
	timezones = {}
	for tz in cal.walk("VTIMEZONE"):
		timezones[str(tz.get("TZID", ""))] = tz
	groups = {}
	failed = []
	for event in cal.walk("VEVENT"):
		uid = icsjson.stringify(event.get("UID")).strip()
		if not uid:
			start, end = icsjson.get_event_bounds(event)
			if start is None:
				failed.append({"uid": "", "error": "event without UID and DTSTART"})
				continue
			uid = icsjson.build_event_uid(event, start, end)
			event.add("UID", uid)
		groups.setdefault(uid, []).append(event)
	result = []
	for uid, events in groups.items():
		out = Calendar()
		out.add("PRODID", PRODID)
		out.add("VERSION", "2.0")
		if cal.get("CALSCALE") is not None:
			out.add("CALSCALE", cal.get("CALSCALE"))
		tzids = []
		for event in events:
			for tzid in used_tzids(event):
				if tzid not in tzids:
					tzids.append(tzid)
		for tzid in tzids:
			if tzid in timezones:
				out.add_component(timezones[tzid])
		for event in events:
			out.add_component(event)
		add_timezones(out)
		result.append((uid, out))
	return result, failed


def import_file(client, cal_url, path):
	try:
		cal = icsjson.read_import_file(path)
	except ValueError as error:
		raise CalDavError("INPUT", str(error))
	except OSError as error:
		raise CalDavError("INPUT", "Cannot read the file: {}".format(error.strerror or error))
	cal_url = check_url(cal_url)
	groups, failed = group_import(cal)
	imported = 0
	for uid, group in groups:
		target = collection_url(cal_url) + resource_name(uid) + ".ics"
		try:
			client.request("PUT", target, group.to_ical(), {
				"Content-Type": "text/calendar; charset=utf-8",
				"If-None-Match": "*",
			}, ok=(200, 201, 204))
		except CalDavError as error:
			if error.kind in ("AUTH", "NETWORK"):
				raise
			failed.append({"uid": uid, "error": "exists" if error.kind == "CONFLICT" else error.line()})
			continue
		imported += 1
	return {"imported": imported, "failed": failed}


#--- command line

class ArgumentParser(argparse.ArgumentParser):
	def error(self, message):
		raise CalDavError("INPUT", message)


def parse_day(value):
	try:
		return icsjson.parse_date(value)
	except ValueError:
		raise CalDavError("INPUT", "Not a date (YYYY-MM-DD): {}".format(value))


def build_parser():
	parser = ArgumentParser(prog="caldav.py", description="CalDAV helper for Event Calendar NG")
	sub = parser.add_subparsers(dest="command", required=True)

	def common(cmd):
		cmd.add_argument("--account", default="", help="Keyring account id (scope caldav-account, key password)")
		cmd.add_argument("--user", default="", help="User name")
		cmd.add_argument("--password-stdin", action="store_true",
			help="Read the password from the first line of stdin (tests / command-line use)")

	cmd = sub.add_parser("discover")
	cmd.add_argument("--url", required=True)
	common(cmd)

	cmd = sub.add_parser("query")
	cmd.add_argument("--url", required=True)
	cmd.add_argument("--if-ctag", default="", help="The change tag of the last fetch: unchanged, no events are downloaded")
	cmd.add_argument("--no-ctag", action="store_true", help="Don't ask for the change tag (the server has none)")
	cmd.add_argument("start")
	cmd.add_argument("end")
	common(cmd)

	cmd = sub.add_parser("put")
	cmd.add_argument("--url", required=True)
	cmd.add_argument("--href", default="")
	cmd.add_argument("--etag", default="")
	cmd.add_argument("--event-b64", default=None, help="The event JSON as base64 of UTF-8 (else read from stdin)")
	cmd.add_argument("--recurrence-id", default=None, help="Only this occurrence of the series (its caldavRecurrenceId JSON)")
	common(cmd)

	cmd = sub.add_parser("delete")
	cmd.add_argument("--href", required=True)
	cmd.add_argument("--etag", required=True)
	cmd.add_argument("--recurrence-id", default=None, help="Only this occurrence of the series (its caldavRecurrenceId JSON)")
	common(cmd)

	cmd = sub.add_parser("restore")
	cmd.add_argument("--undo", required=True, help="The token a delete gave")
	common(cmd)

	cmd = sub.add_parser("move")
	cmd.add_argument("--href", required=True)
	cmd.add_argument("--etag", required=True)
	cmd.add_argument("--to", required=True, help="The other calendar's URL")
	common(cmd)

	cmd = sub.add_parser("get")
	cmd.add_argument("--href", required=True)
	common(cmd)

	cmd = sub.add_parser("import")
	cmd.add_argument("--url", required=True)
	cmd.add_argument("--file", required=True)
	common(cmd)
	return parser


def keyring_password(account):
	import secret_store
	attributes = secret_store.build_attributes(argparse.Namespace(scope=SECRET_SCOPE, key=SECRET_KEY, account_id=account))
	found, value, error = secret_store.lookup_secret_detailed(attributes)
	if found and value:
		return value
	if error:
		raise CalDavError("KEYRING", "The keyring could not be read: {}".format(error))
	raise CalDavError("AUTH", "No password stored for this account")


def read_password(args, stdin):
	if args.password_stdin:
		line = stdin.readline()
		return line.rstrip("\r\n")
	if not args.account:
		raise CalDavError("INPUT", "--account is required")
	return keyring_password(args.account)


def run(args, stdin):
	if args.command == "query":
		start = parse_day(args.start)
		end = parse_day(args.end)
		if end <= start:
			raise CalDavError("INPUT", "END must be after START")
	if args.command == "put" and bool(args.href) != bool(args.etag):
		raise CalDavError("INPUT", "--href and --etag go together")
	recurrence_id = None
	if getattr(args, "recurrence_id", None) is not None:
		recurrence_id = parse_recurrence_id(args.recurrence_id)
	if not args.user:
		raise CalDavError("INPUT", "--user is required")
	password = read_password(args, stdin)
	run.password = password
	client = Client(args.user, password)

	if args.command == "discover":
		return discover(client, args.url)
	if args.command == "query":
		return query(client, args.url, start, end, args.if_ctag, not args.no_ctag)
	if args.command == "put":
		try:
			if args.event_b64 is not None:
				data = json.loads(base64.b64decode(args.event_b64, validate=True).decode("utf-8"))
			else:
				data = json.loads(stdin.read())
		except (ValueError, UnicodeDecodeError) as error:
			raise CalDavError("INPUT", "The event is not valid (base64) JSON: {}".format(error))
		return put_event(client, args.url, data, args.href or None, args.etag or None, recurrence_id)
	if args.command == "delete":
		return delete_event(client, args.href, args.etag, recurrence_id)
	if args.command == "restore":
		return restore_event(client, args.undo)
	if args.command == "move":
		return move_event(client, args.href, args.etag, args.to)
	if args.command == "get":
		return get_series(client, args.href)
	if args.command == "import":
		return import_file(client, args.url, args.file)
	raise CalDavError("INPUT", "Unknown command")


run.password = ""


def scrub(text, secrets):
	for secret in secrets:
		if secret:
			text = text.replace(secret, "***")
	return text


def main(argv=None, stdin=None, stdout=None, stderr=None):
	stdin = stdin if stdin is not None else sys.stdin
	stdout = stdout if stdout is not None else sys.stdout
	stderr = stderr if stderr is not None else sys.stderr
	run.password = ""
	if MISSING_MODULE:
		stderr.write("DEPS: The Python module {} is missing (icalendar, recurring-ical-events)\n".format(MISSING_MODULE))
		return 1
	try:
		args = build_parser().parse_args(argv)
		result = run(args, stdin)
	except CalDavError as error:
		line = error.line()
	except KeyboardInterrupt:
		line = "NETWORK: Interrupted"
	except Exception as error:  # never a traceback (it could show request details)
		line = "PROTOCOL: {}: {}".format(type(error).__name__, error)
	else:
		stdout.write(json.dumps(result, indent=None))
		stdout.write("\n")
		return 0
	line = scrub(" ".join(line.split()), [run.password])
	stderr.write(line + "\n")
	return 1


if __name__ == "__main__":
	sys.exit(main())
