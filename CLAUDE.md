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

Done (in this repo):
- `app/config.py` — env settings (see `.env.example`).
- `app/clock.py` — single `now()` in clinic-local naive time; `set_now()` for tests.
- `app/db.py` — engine, session, `init_db()`.
- `app/crypto.py` — encrypt/decrypt, phone normalisation, keyed `phone_hash` (last 10 digits) for
  matching inbound SMS, `last4`.
- `app/models.py` — all tables from the spec's Data model.
- `app/estimator.py` — pure estimator (see notes below). **Not yet tested.**

To build next, in order:
1. **Tests for the estimator** (`tests/test_estimator.py`): range is always ≤15 min; formula
   case (everyone present) matches `W = S + (Σr + k·A)/D`; late opening (S); booked patient never
   told earlier than appointment; overdue booked patient doesn't block waiting ones; D=0 safe.
2. **Queue service** (`app/queue.py`): state machine booked → waiting → in_visit → done, plus
   no_show / cancelled; every change writes `audit_log`, updates `clinic_days.last_staff_action_at`,
   recomputes estimates, calls the notifier. Undo within 2 min using audit `from_state`.
   Save `first_estimate_min` at check-in and `actual_wait_min` at start.
3. **Notifier + SMS** (`app/notifier.py`, `app/sms.py`): pre-arrival (lead 90 min, respects quiet
   hours by deferring), check-in, delay (expected start moved > threshold vs
   `last_notified_start_at`, max one per 15 min), inbound WAIT reply (match by `phone_hash`;
   unknown number → walk-in wait). **Every text contains the numbers, never a name.**
4. **Background job** (`app/jobs.py`): every 60 s — due pre-arrival texts, outbox retries
   (up to 30 min), recompute for time-based changes; nightly — clear expired link tokens,
   null name/phone after 30 days, delete visits after 90 days, notifications after 30.
5. **Routes + templates:** staff login (bcrypt, individual users, 5-min idle lock),
   `/staff` console, `/staff/appointments` (day view, book, check in, edit, cancel, print sheet),
   `/staff/settings` (manager), patient page `/w/{token}` (+ `/api/w/{token}` JSON),
   `/api/sms/inbound`, `/dev/outbox` and a dev "send WAIT" form.
6. **CLI** (`app/cli.py`): `create-staff`, `seed-demo`.
7. **Checks against the spec's traceability table:** patient page < 200 KB (aim < 50 KB),
   ≥ 18pt text, refresh 60 s with `<meta refresh>` fallback, no names in any patient response
   or SMS body, altered tokens → "link not found".
8. README, Dockerfile, push to GitHub.

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

## Conventions

- Keep the patient page tiny: inline CSS, no web fonts, no images, no third-party scripts.
- Staff pages: plain forms, one tap per action, big buttons.
- Never put a patient name or full phone number in logs, SMS bodies, or patient-facing HTML.
- Run tests with `pytest -q`. Freeze time in tests with `app.clock.set_now(...)`.
- Placeholders still to confirm with the clinic: 90-min pre-arrival lead, slot length,
  30-day name retention, 5-min idle lock (see Open questions in the spec).
