# Clinic Wait Time

A small web app for a community health clinic. Front-desk staff book appointments and run
the day's queue; patients get SMS texts telling them when to leave and how long they'll
wait, plus an optional live page. Texts carry the numbers themselves, so patients without
mobile data still get the answer, and anyone can text **WAIT** for an update.

The design is in [docs/SPEC.md](docs/SPEC.md).

## What's in v1

| Screen / endpoint | Who | What |
| --- | --- | --- |
| `/staff` | Staff | Today's queue: walk-in check-in, Start / End visit, No-show, Undo (2 min), Clinic open, Running late, doctors in session, accuracy figure |
| `/staff/appointments` | Staff | Day view of slots (replaces the paper book): book, check in, edit, cancel, print the day sheet |
| `/staff/settings` | Managers | Clinic settings and staff logins |
| `/w/{token}` | Patients | Position and wait range, refreshes every 60 s (no name, under 3 KB) |
| `/api/w/{token}` | Patients | JSON for the page's refresh |
| `/api/sms/inbound` | SMS provider | Inbound texts (WAIT) |
| `/dev/outbox` | Staff, DEV_MODE only | Simulated texts and a "pretend to text WAIT" form |

Texts sent: pre-arrival (90 min before a booking), check-in, delay (expected start moved
more than 15 min later; max one per 15 min; none 8 pm–7 am) and WAIT replies.

## Run it locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python -m app.cli seed-demo
```

```bash
.venv/bin/uvicorn app.main:app --reload
```

Open http://localhost:8000. `seed-demo` (DEV_MODE only) fills today with fake bookings
and walk-ins and creates a demo manager login; its username and password are in
[app/cli.py](app/cli.py). Texts are simulated: see them under **SMS outbox (dev)**.

Add real staff logins with:

```bash
.venv/bin/python -m app.cli create-staff --username priya --name "Priya S" --role manager
```

## Tests

```bash
.venv/bin/pytest -q
```

Tests freeze time with `app.clock.set_now(...)`. They cover the estimator (range ≤ 15 min,
the spec's formula, late opening, booked patients never told earlier than their
appointment), the queue state machine and undo, every text rule, retention, and the
spec's traceability checks (patient page size and text size, refresh, no names in any
patient response or text, altered links → "Link not found").

## Configuration

Environment variables (see [.env.example](.env.example)):

| Variable | Notes |
| --- | --- |
| `DATABASE_URL` | `sqlite:///./clinic.db` locally; `postgresql+psycopg://…` in production |
| `SECRET_KEY` | Long random string. Signs staff sessions and keys the phone hash |
| `DATA_KEY` | Fernet key that encrypts names and phones. **Back it up** — without it the data can't be read |
| `CLINIC_TZ` | Clinic time zone (default `Asia/Kolkata`) |
| `BASE_URL` | Public URL, used in text links and Twilio signature checks |
| `SMS_BACKEND` | `simulated` (default) or `twilio`, with `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` |
| `DEV_MODE` | `true` enables `/dev/*` and `seed-demo`; the app refuses to start with `false` and the default `SECRET_KEY` |
| `RUN_JOBS` | Runs the every-minute and nightly background job inside the app |
| `HTTPS_ONLY_COOKIES` | `true` in production |

Clinic settings (opening hours, slot length, default visit length, pre-arrival lead,
quiet hours, retention, idle lock) are edited by a manager at `/staff/settings`.

## Deploy

The `Dockerfile` builds a single-process image (`DEV_MODE=false`). Run **one** instance:
the background job lives inside the app. Point Twilio's incoming-message webhook at
`{BASE_URL}/api/sms/inbound` (HTTP POST). Use managed PostgreSQL with backups, and
HTTPS from the host.

Before real patient data: sign a BAA (US) or meet DPDP Act duties (India) with the host,
database and SMS provider, and settle the open questions at the end of the spec
(pre-arrival lead, slot length, retention period, idle lock, walk-in ordering).

## Layout

```
app/
  estimator.py   pure wait-time estimator
  snapshot.py    reads today's state and runs the estimator
  queue.py       the only code that changes visits (state machine, audit, undo)
  notifier.py    which texts to send and their wording
  sms.py         outbox and SMS backends
  jobs.py        every-minute and nightly jobs
  auth.py        staff logins, idle lock
  routes/        staff, patient and dev pages
  templates/     Jinja2 templates
  cli.py         create-staff, seed-demo
tests/
docs/SPEC.md
```
