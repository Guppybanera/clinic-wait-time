# Clinic Wait Time — handoff for Claude Code

A web app for a community health clinic: staff book appointments and run the day's queue;
patients get SMS texts (and an optional live page) telling them when to leave and how long
they'll wait. The full design is in `docs/SPEC.md` — read it before changing behaviour.

## Decisions already made (don't re-ask)

- **Stack:** Python 3.12+ / FastAPI, server-rendered Jinja2 pages, SQLAlchemy 2.0.
  SQLite locally, PostgreSQL in production. No front-end framework.
- **First build = full v1 with SMS simulated.** Texts go to the `notifications` outbox and
  are shown at `/dev/outbox` (DEV_MODE only). A Twilio backend sits behind `SMS_BACKEND=twilio`.
- **Clinic has no appointment system** (pen and paper today), so the app includes the
  Appointments screen that replaces the book.
- **Names are stored, for staff only.** Name and phone are encrypted (Fernet, `DATA_KEY`),
  shown only on staff screens behind individual logins, never on a patient page or in a text.
  Deleted after `name_retention_days` (30).
- **Code will live in GitHub** at `Guppybanera/clinic-wait-time` (private) once created.

## Status

v1 is built and tested (`pytest -q`, ~1000 tests incl. a parametrised range sweep):
estimator, queue service (`app/queue.py`), notifier + SMS outbox (`app/notifier.py`,
`app/sms.py`, Twilio behind `SMS_BACKEND=twilio`), background job (`app/jobs.py`), staff
login / console / appointments / settings, patient page + JSON, inbound SMS webhook (Twilio
signature checked), dev outbox, CLI (`create-staff`, `seed-demo`), README, Dockerfile.
`app/snapshot.py` reads today's state and runs the estimator for everyone else.

Not done yet / next candidates:
- Audit entries for *viewing* (spec mentions "viewed"); only changes are audited now.
- Per-doctor columns on the Appointments screen (one queue for the clinic in v1).
- Languages other than English for texts and the patient page.
- The JSON `/api/appointments`, `/api/visits/...` endpoints from the spec: staff screens
  use plain form posts under `/staff/...` instead.
- Real-world checks from the traceability table: Slow-3G load, outdoor readability,
  timed bookings/check-ins, Twilio end to end.

## Implementation notes (differences from the spec's wording)

- **Estimator is a small simulation**, not the bare formula: D doctors become free at given
  times; patients are taken in queue order (booked by appointment time, walk-ins by check-in
  time, so walk-ins fill gaps). It reduces to the spec's formula when everyone is present,
  and also handles gaps and booked patients who haven't arrived. Booked patients > 30 min past
  their appointment and not arrived go to the back. Update `docs/SPEC.md` to say this.
- **`clinic_days.delay_until`** (a datetime) replaces the spec's `delay_min`; "Running late"
  sets it from either a new opening time or "delayed N min".
- **`visits.last_notified_start_at`** (expected start clock time) replaces
  `last_notified_mid_min`; comparing clock times avoids false alerts as minutes tick down.
- **No visit code.** Inbound texts are matched by the sender's phone (keyed hash), so the short
  code from earlier drafts isn't needed.
- **One queue for the whole clinic** (per-doctor queues are an open question in the spec).
- All datetimes are clinic-local and naive (single clinic). `CLINIC_TZ` defaults to Asia/Kolkata.
- Past opening with no "Clinic open" tap, the assumed slip is capped at 60 min
  (`MAX_ASSUMED_SLIP_MIN`); the first **Start visit** also marks the clinic open.
- A booked patient's clock range never starts before their appointment.
- No delay text to a patient expected within `alert_threshold_min` anyway (avoids noise
  when staff simply haven't tapped yet). Quiet hours defer pre-arrival and delay texts.
- `queue.py` functions commit, then flush the outbox. Staff screens use form posts with a
  SameSite=strict session cookie; the console auto-refreshes with `?auto=1`, which doesn't
  reset the idle lock.

## Conventions

- Keep the patient page tiny: inline CSS, no web fonts, no images, no third-party scripts.
- Staff pages: plain forms, one tap per action, big buttons.
- Never put a patient name or full phone number in logs, SMS bodies, or patient-facing HTML.
- Run tests with `pytest -q`. Freeze time in tests with `app.clock.set_now(...)`.
- Placeholders still to confirm with the clinic: 90-min pre-arrival lead, slot length,
  30-day name retention, 5-min idle lock (see Open questions in the spec).
