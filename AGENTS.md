# Event Calendar NG: notes for AI agents

A Plasma 6 panel widget (calendar, agenda, week/day views, Google Tasks/TickTick, meetings, weather),
a complete redesign of Zren's Event Calendar via ALikesToCode's Plasma 6 port. Pure QML/JS plus small
Python helper scripts; no C++ plugin. Plugin Id `org.kde.plasma.eventcalendar` (kept on purpose: it
replaces an installed Event Calendar and keeps its settings), name "Event Calendar NG", version in
`package/metadata.json`. Published on GitHub as `divinae/event-calendar-ng-plasmoid` (site:
https://divinae.github.io/event-calendar-ng-plasmoid/, served from `docs/` on `master`) and on the
KDE Store as https://store.kde.org/p/2379570/. Remotes: `fork` = the user's repo (push here; `master`
tracks `fork/master`), `origin` = upstream ALikesToCode (read only). Commit straight to `master`; the
local `prNN` branches are checked-out contributor PRs.

## Layout

- `package/metadata.json`: Id, name, version, authors. Note: it is listed in `.gitignore` but tracked.
- `package/contents/ui/main.qml`: the `PlasmoidItem` root; wires `ConfigMigration`, `AppletConfig`,
  `NotificationManager`, `Logger`, the models and views.
- `package/contents/ui/*.qml`: views (`MonthView`, `WeekView`, `AgendaView`, `TasksView`, `PopupView`,
  `EventDetailsView`, `EventEditView`, meeting alert windows ...), models (`EventModel`, `AgendaModel`,
  `UpcomingEvents`, `EventCache`), `Logic.qml`.
- `package/contents/ui/*.js`: pure logic, mostly `.pragma library` (Recurrence, QuickParse, FreeSlots,
  MeetingLink, Reminders, Moon, TimeZone ...). These are what the node tests load.
- `package/contents/ui/calendars/`: backends (Google Calendar/Tasks, TickTick, CalDAV, iCal,
  Plasma calendar plugins/Akonadi) behind `CalendarManager.qml`.
- `package/contents/ui/lib/`: shared helpers: `ConfigPage.qml` (every setting's `cfg_` property),
  `ExecUtil.qml` (runs commands via the Plasma5Support "executable" engine), `Logger.qml`,
  `Requests.js`, OAuth (`GoogleOAuthConfig.js`, `GoogleOAuthToken.js`, `Pkce.js`), `KeyringSecret.qml`.
- `package/contents/ui/config/`: settings pages (`ConfigGeneral`, `ConfigCalendars`, `ConfigViews`,
  `ConfigEvents`, `ConfigTasks`, `ConfigWeather`, `ConfigClock`, `ConfigTimezones`); page list in
  `package/contents/config/config.qml`.
- `package/contents/ui/weather/`: weather providers (OpenMeteo, OpenWeatherMap, WeatherCanada, WeatherApi).
- `package/contents/config/main.xml`: config schema (~167 entries, group `General`).
- `package/contents/scripts/*.py`: helpers run with `python3` (CalDAV, .ics to JSON, secret store via
  `secret-tool`, Google OAuth redirect, media pause, detached launch). Paths built by `ui/ScriptPath.js`.
- `package/contents/knotifications/eventcalendar.notifyrc`: notification events.
- `package/translate/`: `template.pot`, 20 `*.po` catalogs, Zren's `merge`/`build` scripts.
- `tests/`: `*.test.js` (node), `*_test.py` (unittest), `qml_compile.sh` + `qml_compile.qml`,
  `qml_compile_oldest_formcard.sh`. `tools/generate_timezones.py` regenerates `ui/code/TimeZoneData.js`.
- `docs/`: GitHub Pages site (index, privacy, terms, oauth_success), screenshots, `kde-store.md`
  (store listing text, updated each release).
- Root scripts: `install`, `update`, `uninstall`, `build`. No Makefile/CMake.

## Build, run, test

- Tests (as in CONTRIBUTING.md and `.github/workflows/ci.yml`):
  - `node --test tests/*.test.js`
  - `python3 -m unittest discover -s tests -p '*_test.py' -v` (needs `icalendar`, `recurring-ical-events`)
  - `bash tests/qml_compile.sh` (loads every QML file offscreen with the installed Qt/KDE modules)
  - `bash tests/qml_compile_oldest_formcard.sh` (FormCard of old Kirigami Addons, default v1.7.0 via
    `KIRIGAMI_ADDONS_VERSION`; downloads from KDE GitLab)
  - `shellcheck --severity=warning install update uninstall build package/translate/build package/translate/merge tests/qml_compile.sh tests/qml_compile_oldest_formcard.sh`
- Package: `./build` runs `translate/merge` + `translate/build`, fails if that changed anything under
  `package/translate` (stale catalogs), then zips `eventcalendar-ng-vX.Y.Z.plasmoid` in the repo root.
- Install locally: `./install` copies `package/` to
  `~/.local/share/plasma/plasmoids/org.kde.plasma.eventcalendar` (`--restart` only calls
  `reloadConfig()` on instances via qdbus; it does not restart plasmashell, despite CONTRIBUTING.md).
  Then `systemctl --user restart plasma-plasmashell` to see QML changes.
- Standalone: `package/translate/plasmoidlocaletest` uses `plasmoidviewer -a <package dir>` (plasma-sdk).
- Logs: enable Settings > debugging (`debugging` key, `Logger` prints only then); read with
  `journalctl --user -b | grep -i eventcalendar`.

## Conventions

- QML/JS: tabs for indentation, no statement-ending semicolons, single or double quotes both used.
  `//` comments in plain explanatory English, often stating the why and the issue/version.
  Pure logic goes in `.pragma library` JS files with a node test that loads them via `vm`
  (strip the `.pragma` line, see `tests/moon.test.js`). No SPDX headers in this repo.
- i18n: `i18n()` / `i18nc("context", ...)` with `%1` placeholders for every user-visible string.
  Domain `plasma_applet_org.kde.plasma.eventcalendar`. After changing strings: `cd package/translate &&
  bash ./merge` (updates `template.pot`, all `.po`, and the status table in `translate/ReadMe.md`),
  then fill the new strings in all 20 languages (history: "Translations: ... in all languages") and
  commit the catalogs, otherwise `./build`/CI fails. `.mo` files are built, never committed.
  CI uses Arch's gettext; an older `xgettext` wraps differently and shows as stale catalogs.
- New config key: add it to `main.xml` and a `property var cfg_<key>` to `ui/lib/ConfigPage.qml`;
  `tests/config_page_keys.test.js` enforces this (widget-held state keys are allowlisted there).
- Settings migrations: `ui/ConfigMigration.qml`, run once at start, guarded by bool flags in
  `main.xml` (`v72Migration`, `v73Migration`, `v182Migration` ...). Add a new flag per migration.
- Release: bump `KPlugin.Version` in `package/metadata.json`, add `## vX.Y.Z - Month D YYYY` at the top
  of `Changelog.md` (sections `### Area`, bold-led bullets, crediting contributors/issues), update the
  `.plasmoid` filename in `ReadMe.md` and `docs/kde-store.md`; commit "Release X.Y.Z: ...", tag
  `vX.Y.Z`, push the tag to `fork`. `.github/workflows/release.yml` builds and publishes the GitHub
  release using that changelog section (fails if missing).
- Commits: `Area: what changed, in plain words` (e.g. `CalDAV: fetched again at login when the
  keyring isn't up yet`, `Glance: ...`, `Settings > General: ...`, `Review fixes: ...`), with a body
  when useful and a `Co-Authored-By:` trailer. Contributor PRs merged as `Merge PR #NN (user): ...`.

## Gotchas

- `./install` runs `sudo` package installs, `pip install --target` into
  `~/.local/share/plasma_org.kde.plasma.eventcalendar/python`, and on a clean tree does
  `git fetch origin` + `git pull --ff-only origin master`; here `origin` is upstream, not the fork.
  `./update` = refuse dirty tree + `./install --restart`. Do not run these without being asked.
- `./install` copies `docs/oauth_success.html` and `docs/css/style.css` into
  `package/contents/scripts/oauth_assets/`; keep them in sync when editing the docs page.
- `tests/docs_command_blocks.test.js` reads `docs/index.html` and `CONTRIBUTING.md`; doc edits can fail tests.
- Guard tests grep the sources: no sensitive Google token logging, no raw Plasma calendar or large
  weather debug logs, no deprecated Qt base64 (`no_*.test.js`).
- Notifications: a plasmoid can't install a notifyrc, so `NotificationManager.qml` copies it to
  `~/.local/share/knotifications6/` at first start; until then it sends with componentName
  `plasma_workspace` (event `notification`), afterwards `eventcalendar` (events in the notifyrc).
  `uninstall` removes that copy and the cache `~/.cache/plasma-eventcalendar`.
- Settings pages must work with Kirigami Addons FormCard 1.7 (Debian 13): a newer-only property
  blanks a whole page. Hence the oldest-FormCard compile check.
- Startup cost matters: Akonadi/calendar plugins load deferred, Qt Multimedia only when the popup opens;
  new installs default to holidays only (`enabledCalendarPlugins`). Layout loops in the Agenda have
  frozen plasmashell at 100% CPU before (1.19/1.20): watch width bindings between tiles and rows.
- Google OAuth: built-in Desktop client id in `lib/GoogleOAuthConfig.js`; legacy Zren client secret is
  kept so old accounts still refresh. Tokens/secrets live in the system keyring (`secret-tool`,
  libsecret). Never log tokens or redirect URLs.
- Runtime deps (from `install`): kpackagetool6, knotifications QML, python3, libsecret tools,
  libcanberra; optional pipewire, python icalendar, Akonadi/kdepim, qdbus.
- Unclear: `metadata.json` `Website`/`BugReportUrl` and `translate/ReadMe.md` still point to
  `divinae/plasma-applet-eventcalendar` (old repo name, presumably redirected); the CI step name says
  FormCard 1.12 while the script defaults to v1.7.0.

## Related

- `../glance-plasmoid`: Event Calendar pushes today's/tomorrow's events and weather to Glance over
  D-Bus (`com.dv.glance` `/Calendar`, via `gdbus`) from `ui/GlancePublisher.qml`/`.js`; the receiving
  side is `glance-plasmoid/plugin/calendarbus.{h,cpp}`. Off by default (`glanceShareEvents`). Change
  the payload on both sides together.
- `.github/FUNDING.yml` is identical in glance, karousel-overview, shift, taskline and uswitch.
  No other source files are shared verbatim with the sibling widgets.
