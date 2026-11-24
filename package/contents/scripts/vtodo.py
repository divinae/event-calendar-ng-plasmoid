"""CalDAV tasks (VTODO) as the widget's task items, and the widget's changes back into VTODOs.

The items have the fields the task views read (see ui/Tasks.js): id (the UID), title, notes,
status ("needsAction" / "completed"), completed (ISO time, UTC), due ("YYYY-MM-DDT00:00:00.000Z", the
local due day, as Google gives it), dueTime ("HH:mm" local, "" for a day or no due), priority (0, 1,
3, 5), reminders (minutes before the due time; negative: after it), repeatRule ("RRULE:..."),
parentId (the parent's UID, RELATED-TO), sortOrder (X-APPLE-SORT-ORDER, as Nextcloud Tasks and
Apple's Reminders keep the order). Changes come in the same shape (Tasks.editBody): only the keys
given change, the rest of the VTODO stays as it was.
"""

import copy
import datetime
import os
import re

from icalendar import Alarm, Calendar, Todo

import icsjson

COMPLETED_DAYS = 30  # Done tasks shown: completed in the last 30 days (as Tasks.COMPLETED_DAYS)
MAX_REMINDERS = 5
MAX_REMINDER_MINUTES = 40320


class TaskInputError(ValueError):
	pass


def master_todo(cal):
	for todo in cal.walk("VTODO"):
		if todo.get("RECURRENCE-ID") is None:
			return todo
	return None


def utc_iso(value):
	if isinstance(value, datetime.datetime):
		if value.tzinfo is None:
			value = value.astimezone()  # Floating: the local time
		return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
	if isinstance(value, datetime.date):
		return value.strftime("%Y-%m-%dT00:00:00.000Z")
	return ""


def local_due(todo):
	"""(day, "HH:mm" or "", local naive datetime of the due moment) or (None, "", None)."""
	due = icsjson.get_event_date(todo, "DUE")
	if due is None:
		return None, "", None
	if isinstance(due, datetime.datetime):
		local = icsjson.ensure_date_time(due)
		return local.date(), local.strftime("%H:%M"), local
	return due, "", datetime.datetime.combine(due, datetime.time.min)


def widget_priority(value):
	"""iCalendar 1 (highest) .. 9 (lowest), 0 none -> the widget's 5 high, 3 medium, 1 low, 0 none."""
	try:
		n = int(str(value))
	except (TypeError, ValueError):
		return 0
	if 1 <= n <= 4:
		return 5
	if n == 5:
		return 3
	if 6 <= n <= 9:
		return 1
	return 0


def ical_priority(priority):
	return {5: 1, 3: 5, 1: 9}.get(priority, 0)


def alarm_offset(alarm, todo, due_local):
	"""A display / audio alarm's minutes before the due moment (negative: after it), else None."""
	if getattr(alarm, "name", "") != "VALARM" or due_local is None:
		return None
	action = icsjson.stringify(alarm.get("ACTION")).strip().upper()
	if action and action not in ("DISPLAY", "AUDIO"):
		return None
	trigger = alarm.get("TRIGGER")
	value = getattr(trigger, "dt", None)
	if isinstance(value, datetime.timedelta):
		related = icsjson.stringify(trigger.params.get("RELATED") if hasattr(trigger, "params") else "").upper()
		if related == "END":
			when = due_local + value
		else:
			start = icsjson.get_event_date(todo, "DTSTART")
			if start is None:
				return None
			when = icsjson.ensure_date_time(start) + value
	elif isinstance(value, datetime.datetime):
		when = icsjson.ensure_date_time(value)
	else:
		return None
	return int(round((due_local - when).total_seconds() / 60))


def parent_uid(todo):
	related = todo.get("RELATED-TO")
	for value in related if isinstance(related, list) else ([related] if related is not None else []):
		reltype = icsjson.stringify(getattr(value, "params", {}).get("RELTYPE", "PARENT")).upper()
		if reltype == "PARENT":
			return icsjson.stringify(value).strip()
	return ""


def is_done(todo):
	return icsjson.stringify(todo.get("STATUS")).strip().upper() == "COMPLETED" or todo.get("COMPLETED") is not None


def todo_to_item(todo):
	uid = icsjson.stringify(todo.get("UID")).strip()
	day, time, due_local = local_due(todo)
	done = is_done(todo)
	item = {
		"kind": "tasks#task",
		"id": uid,
		"title": icsjson.stringify(todo.get("SUMMARY")),
		"notes": icsjson.stringify(todo.get("DESCRIPTION")),
		"status": "completed" if done else "needsAction",
		"dueTime": time,
		"priority": widget_priority(todo.get("PRIORITY")),
		"reminders": [],
		"repeatRule": "",
		"parentId": parent_uid(todo),
		"subtasks": [],
	}
	if day is not None:
		item["due"] = day.strftime("%Y-%m-%dT00:00:00.000Z")
	completed = icsjson.get_event_date(todo, "COMPLETED")
	if done:
		stamp = completed or icsjson.get_event_date(todo, "LAST-MODIFIED") or icsjson.get_event_date(todo, "DTSTAMP")
		if stamp is not None:
			item["completed"] = utc_iso(stamp)
	reminders = []
	for alarm in todo.subcomponents:
		offset = alarm_offset(alarm, todo, due_local)
		if offset is not None and offset not in reminders:
			reminders.append(offset)
	item["reminders"] = sorted(reminders, reverse=True)
	rrule = todo.get("RRULE")
	if rrule is not None:
		item["repeatRule"] = "RRULE:" + rrule.to_ical().decode("utf-8")
	sort_order = icsjson.stringify(todo.get("X-APPLE-SORT-ORDER")).strip()
	if re.fullmatch(r"-?\d+", sort_order):
		item["sortOrder"] = int(sort_order)
	return item


def resource_task(calendar_data, now=None, completed_days=COMPLETED_DAYS):
	"""The item of one resource's task, or None: not a task, or done before the last completed_days."""
	cal = Calendar.from_ical(calendar_data)
	todo = master_todo(cal)
	if todo is None or not icsjson.stringify(todo.get("UID")).strip():
		return None
	if icsjson.stringify(todo.get("STATUS")).strip().upper() == "CANCELLED":
		return None
	item = todo_to_item(todo)
	if item["status"] == "completed":
		now = now or datetime.datetime.now(datetime.timezone.utc)
		completed = item.get("completed")
		if completed:
			when = datetime.datetime.strptime(completed, "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=datetime.timezone.utc)
			if now - when > datetime.timedelta(days=completed_days):
				return None
	return item


#--- Changes

def zone_of(name):
	if not name:
		return None
	try:
		import zoneinfo
		return zoneinfo.ZoneInfo(name)
	except Exception:
		raise TaskInputError("Unknown time zone: {}".format(name))


def local_zone():
	"""The system's zone: by its IANA name (TZ, /etc/localtime), else the current UTC offset."""
	name = os.environ.get("TZ", "").lstrip(":")
	if not name:
		target = os.path.realpath("/etc/localtime")
		if "/zoneinfo/" in target:
			name = target.split("/zoneinfo/", 1)[1]
	try:
		return zone_of(name) if name else datetime.datetime.now().astimezone().tzinfo
	except TaskInputError:
		return datetime.datetime.now().astimezone().tzinfo


def parse_due_day(value):
	"""Google's due ("YYYY-MM-DDT00:00:00.000Z", or a day) -> a date; None for none."""
	if value is None or value == "":
		return None
	if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}", value):
		raise TaskInputError("due must be a day (YYYY-MM-DD...)")
	try:
		return datetime.date.fromisoformat(value[:10])
	except ValueError:
		raise TaskInputError("due must be a day (YYYY-MM-DD...)")


def parse_time(value):
	if not value:
		return None
	m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value))
	if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
		raise TaskInputError("dueTime must be HH:mm")
	return datetime.time(int(m.group(1)), int(m.group(2)))


def set_text(todo, key, value):
	if value is None:
		return
	if not isinstance(value, str):
		raise TaskInputError("{} must be a string".format(key.lower()))
	todo.pop(key, None)
	if value != "" or key == "SUMMARY":
		todo.add(key, value)


def set_due(todo, data):
	"""due / dueTime given: DUE as a day, or a time in data's timeZone (else the local zone)."""
	if "due" not in data and "dueTime" not in data:
		return
	if "due" in data:
		day = parse_due_day(data["due"])
	else:
		current, _, _ = local_due(todo)
		day = current
	if "dueTime" in data:
		time = parse_time(data.get("dueTime"))
	else:
		_, current_time, _ = local_due(todo)
		time = parse_time(current_time) if current_time else None
	todo.pop("DUE", None)
	if day is None:
		# Alarms relative to the due (RELATED=END) need one (RFC 5545).
		todo.subcomponents = [c for c in todo.subcomponents if not due_related(c)]
		return
	if time is None:
		due = day
	else:
		zone = zone_of(data.get("timeZone")) or local_zone()
		due = datetime.datetime.combine(day, time).replace(tzinfo=zone)
	todo.add("DUE", due)
	# DTSTART must be of the same kind and not after DUE (RFC 5545): one that isn't goes.
	start = icsjson.get_event_date(todo, "DTSTART")
	if start is not None:
		same_kind = isinstance(start, datetime.datetime) == isinstance(due, datetime.datetime)
		if not same_kind or icsjson.ensure_date_time(start) > icsjson.ensure_date_time(due):
			todo.pop("DTSTART", None)


def due_related(alarm):
	trigger = alarm.get("TRIGGER") if getattr(alarm, "name", "") == "VALARM" else None
	params = getattr(trigger, "params", None) or {}
	return trigger is not None and icsjson.stringify(params.get("RELATED", "")).upper() == "END"


def next_instance(todo):
	"""A repeating task's next instance after its current one: (DTSTART, DUE), each maybe None; None
	when it doesn't repeat or the series ends. TaskInputError when the next instance can't be found
	(the rule can't be expanded): the task isn't completed for good then, which would end the series
	for every client."""
	if todo.get("RRULE") is None:
		return None
	start = icsjson.get_event_date(todo, "DTSTART")
	due = icsjson.get_event_date(todo, "DUE")
	base = start if start is not None else due
	if base is None:
		return None
	rie = icsjson.recurring_ical_events
	if rie is None:
		raise TaskInputError("Can't find the next date of a repeating task: the recurring_ical_events module is missing")
	probe = copy.deepcopy(todo)
	if start is None:
		# DUE only (Nextcloud Tasks' default, the widget's own tasks): the rule repeats the DUE. It's
		# expanded as the start (with its TZID or VALUE=DATE), whatever the library makes of a DUE.
		probe["DTSTART"] = probe.pop("DUE")
	cal = Calendar()
	cal.add_component(probe)
	step = datetime.timedelta(seconds=1) if isinstance(base, datetime.datetime) else datetime.timedelta(days=1)
	try:
		found = list(rie.of(cal, components=["VTODO"]).between(base + step, base + datetime.timedelta(days=3660)))
		key = lambda o: icsjson.ensure_date_time(icsjson.get_event_date(o, "DTSTART") or icsjson.get_event_date(o, "DUE"))
		later = [o for o in found if key(o) > icsjson.ensure_date_time(base)]
	except Exception as error:  # eg: UNTIL a date while the start is a date-time
		raise TaskInputError("Can't find the next date of this repeating task: {}".format(error))
	if not later:
		return None  # The series ended (COUNT, UNTIL): done
	first = min(later, key=key)
	if start is None:
		return None, icsjson.get_event_date(first, "DTSTART")
	return icsjson.get_event_date(first, "DTSTART"), icsjson.get_event_date(first, "DUE")


def set_status(todo, data, stamp):
	status = data.get("status")
	if status is None:
		return
	if status not in ("completed", "needsAction"):
		raise TaskInputError("status must be completed or needsAction")
	for key in ("STATUS", "COMPLETED", "PERCENT-COMPLETE"):
		todo.pop(key, None)
	following = next_instance(todo) if status == "completed" else None
	if following is not None:
		# A repeating task done: on to its next instance (as Apple's Reminders and Thunderbird do).
		for key, value in zip(("DTSTART", "DUE"), following):
			if value is not None and todo.get(key) is not None:
				todo.pop(key)
				todo.add(key, value)
		rule = todo.get("RRULE")
		if rule is not None and rule.get("COUNT"):
			rule["COUNT"] = [int(rule["COUNT"][0]) - 1]
		todo.add("STATUS", "NEEDS-ACTION")
		return
	if status == "completed":
		when = stamp
		if data.get("completed"):
			try:
				when = datetime.datetime.fromisoformat(str(data["completed"]).replace("Z", "+00:00")).astimezone(datetime.timezone.utc)
			except ValueError:
				raise TaskInputError("completed must be an ISO time")
		todo.add("STATUS", "COMPLETED")
		todo.add("COMPLETED", when.replace(microsecond=0))
		todo.add("PERCENT-COMPLETE", 100)
	else:
		todo.add("STATUS", "NEEDS-ACTION")


def set_priority(todo, data):
	if data.get("priority") is None:
		return
	priority = data["priority"]
	if priority not in (0, 1, 3, 5):
		raise TaskInputError("priority must be 0, 1, 3 or 5")
	todo.pop("PRIORITY", None)
	if priority:
		todo.add("PRIORITY", ical_priority(priority))


def set_reminders(todo, data):
	"""The reminders the widget shows replaced; other alarms (email, ...) stay. None without a due."""
	if data.get("reminders") is None:
		return
	reminders = data["reminders"]
	if not isinstance(reminders, list):
		raise TaskInputError("reminders must be a list of minutes")
	minutes = []
	for value in reminders:
		if not isinstance(value, int) or isinstance(value, bool) or abs(value) > MAX_REMINDER_MINUTES:
			raise TaskInputError("A reminder's minutes must be a whole number up to {}".format(MAX_REMINDER_MINUTES))
		if value not in minutes:
			minutes.append(value)
	if len(minutes) > MAX_REMINDERS:
		raise TaskInputError("At most {} reminders".format(MAX_REMINDERS))
	_, _, due_local = local_due(todo)
	todo.subcomponents = [c for c in todo.subcomponents if alarm_offset(c, todo, due_local) is None]
	if due_local is None:
		return
	summary = icsjson.stringify(todo.get("SUMMARY")) or "Reminder"
	for value in sorted(minutes, reverse=True):
		alarm = Alarm()
		alarm.add("ACTION", "DISPLAY")
		alarm.add("DESCRIPTION", summary)
		alarm.add("TRIGGER", datetime.timedelta(minutes=-value), parameters={"RELATED": "END"})
		todo.add_component(alarm)


def set_parent(todo, data):
	if data.get("parentId") is None:
		return
	parent = data["parentId"]
	if not isinstance(parent, str):
		raise TaskInputError("parentId must be a string")
	related = todo.get("RELATED-TO")
	kept = [v for v in (related if isinstance(related, list) else ([related] if related is not None else []))
		if icsjson.stringify(getattr(v, "params", {}).get("RELTYPE", "PARENT")).upper() != "PARENT"]
	todo.pop("RELATED-TO", None)
	for value in kept:
		todo.add("RELATED-TO", value)
	if parent:
		todo.add("RELATED-TO", parent)


def set_repeat(todo, data):
	rule = data.get("repeatRule")
	if rule is None:
		return
	if not isinstance(rule, str):
		raise TaskInputError("repeatRule must be a string")
	todo.pop("RRULE", None)
	rule = rule.strip()
	if rule:
		if rule.upper().startswith("RRULE:"):
			rule = rule[6:]
		try:
			from icalendar import vRecur
			todo.add("RRULE", vRecur.from_ical(rule))
		except Exception:
			raise TaskInputError("Not a repeat rule: {}".format(rule))


def apply_task_fields(todo, data, stamp):
	set_text(todo, "SUMMARY", data.get("title"))
	set_text(todo, "DESCRIPTION", data.get("notes"))
	set_due(todo, data)
	set_status(todo, data, stamp)
	set_priority(todo, data)
	set_parent(todo, data)
	set_repeat(todo, data)
	set_reminders(todo, data)  # After the due: they're relative to it


def touch(todo, stamp):
	for key in ("DTSTAMP", "LAST-MODIFIED"):
		todo.pop(key, None)
		todo.add(key, stamp)
	try:
		sequence = int(str(todo.get("SEQUENCE", 0)))
	except ValueError:
		sequence = 0
	todo.pop("SEQUENCE", None)
	todo.add("SEQUENCE", sequence + 1)


def build_new_task(data, uid, stamp, prodid):
	if not isinstance(data, dict):
		raise TaskInputError("The task must be a JSON object")
	if not isinstance(data.get("title"), str):
		raise TaskInputError("A new task needs a title")
	cal = Calendar()
	cal.add("PRODID", prodid)
	cal.add("VERSION", "2.0")
	todo = Todo()
	todo.add("UID", uid)
	todo.add("DTSTAMP", stamp)
	todo.add("CREATED", stamp)
	todo.add("LAST-MODIFIED", stamp)
	data = dict(data)
	data.setdefault("status", "needsAction")
	apply_task_fields(todo, data, stamp)
	cal.add_component(todo)
	add_timezones(cal)
	return cal.to_ical()


def build_updated_task(existing, data, stamp):
	if not isinstance(data, dict):
		raise TaskInputError("The task must be a JSON object")
	cal = Calendar.from_ical(existing)
	todo = master_todo(cal)
	if todo is None:
		raise TaskInputError("The resource has no task")
	apply_task_fields(todo, data, stamp)
	touch(todo, stamp)
	add_timezones(cal)
	return icsjson.stringify(todo.get("UID")).strip(), cal.to_ical()


def add_timezones(cal):
	if hasattr(cal, "add_missing_timezones"):
		try:
			cal.add_missing_timezones()
		except Exception:
			pass
