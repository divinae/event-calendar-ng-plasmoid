# Event Calendar NG: network audit

Scope: the widget's source under `package/` (QML, JavaScript, Python helper scripts),
2026-11-10. Method: a source review of every URL, web request (`XMLHttpRequest` /
`Requests.js`), Python network code (`urllib`, `http.server`, `socket`) and external command. No
live traffic capture.

**Result:** the widget sends no hidden telemetry, analytics or update checks. Every outgoing
connection is either a service you configured or a link you click.

## Connections the widget makes itself

| Service | Host | When | What is sent |
|---|---|---|---|
| Google Calendar / Tasks | `www.googleapis.com` (calendar/v3, tasks/v1) | Each refresh, and when you edit | Reads events, calendars and task lists. Sends your changes when you create, edit, delete, RSVP or complete. |
| Google sign-in | `accounts.google.com`, `oauth2.googleapis.com/token` | Signing in and refreshing the token | Login and token exchange. |
| TickTick / Dida365 | `api.ticktick.com`, `api.dida365.com` | Only with a TickTick account set up | Reads and changes tasks. |
| CalDAV | The server you entered | Only if configured | Reads and writes calendars. Plain `http` is allowed only for local or private addresses (`scripts/caldav.py`). |
| iCal subscriptions | The URL you entered | Only if configured | A download only. Refused if it points to a local or private address (`scripts/icsjson.py`). |
| Weather: Open-Meteo (default provider) | `api.open-meteo.com`, `geocoding-api.open-meteo.com` | At each weather refresh, and the city search in settings | Your configured coordinates, units and language. The search sends the city name you type. |
| Weather: OpenWeatherMap | `api.openweathermap.org` | Only if selected | City id or coordinates, units, language and your app id. |
| Weather: Environment Canada | `dd.weatheroffice.ec.gc.ca`, `weather.gc.ca` | Only if selected | City id. Over plain http. |
| Python module installer | `pypi.org` (via pip, uv or a direct download) | Only when you click "Install" (CalDAV settings or .ics import) | Downloads `icalendar` and its dependencies. The direct download checks each file against PyPI's SHA-256 (`scripts/install_python_deps.py`). |

Event data leaves your machine only to the calendar or task service it belongs to (Google,
TickTick, your CalDAV server). Weather providers get only a location, never event data.

## Local only

These connections never leave your machine:

- **Google login redirect:** goes to `127.0.0.1:53682` (`scripts/google_redirect.py`). The listener
  runs only during a sign-in (at most 5 minutes), answers only `GET` requests and accepts only the
  redirect carrying the one-time state of the sign-in the widget started. It allows no requests from
  web pages (no CORS headers, no `POST`). If the browser can't reach it, you paste the address into
  the widget's settings instead.
- **Keyring helper:** runs a one-request server on `127.0.0.1` for storing and reading secrets
  (`scripts/secret_store.py`).
- **Akonadi and the other Plasma calendar plugins:** read locally through Plasma. Akonadi's own
  syncing with its accounts is outside the widget.
- **D-Bus:** Glance (if switched on), notifications, media pause and Do Not Disturb.

## Things that load from the internet indirectly

These open in your browser or a meeting app, so the widget itself connects to nothing.

- **Google Fonts:** the login success page shown in your browser
  (`scripts/oauth_assets/style.css`) loads `fonts.googleapis.com`. That gives Google your IP and
  the fact that you opened that page. This is the only third-party load that isn't strictly
  needed; the fonts could be bundled or dropped.
- **Clicked links:**
  - Google Calendar or Tasks on the web
  - Google Maps search with the event's location
  - meteoblue (weather details)
  - OpenWeatherMap and Environment Canada city pages
  - Google Meet or Zoom "new meeting"
  - the privacy policy on `divinae.github.io` (a link in the Google settings)
  - links in an event's text (`http`/`https` and `mailto:` only, see `Shared.js`
    `isSafeExternalUrl`)
- **Meeting links:** Join and auto-join (if switched on) open the event's meeting link.
- **Commands you set yourself** (meeting start and end commands, mouse-wheel commands) run as you
  wrote them.

The URLs to GitHub, Qt docs, Stack Overflow and similar in the code are only in comments.

## The project website

The website (`docs/`, served by GitHub Pages) isn't part of the widget. Its pages load fonts from
`fonts.googleapis.com` and the home page loads its code highlighting from `cdn.jsdelivr.net`;
GitHub's privacy statement applies to the pages themselves.
It no longer has a sign-in helper: nothing on it receives or forwards Google authorization codes.

## Changes since the first review

- **Hosted sign-in helper removed:** the widget had an unused option to let Google redirect to a
  helper page on `divinae.github.io`, which then sent the code to the widget's listener. The
  option, the listener's `POST` endpoint and its permission for that site, the settings' link to
  the page and the page's section on the website are gone. The sign-in uses only the listener on
  `127.0.0.1`.
