# Clinic Wait Time — Technical Spec

Nov 4, 2026 · @Ganapathy

## Approach

Build one small web application, hosted on a managed platform, with one database and one SMS provider. There is nothing to install at the clinic or on patients' phones. The front desk uses a browser on the computer they already have, and the app replaces the paper appointment book. Patients get texts that work without mobile data, with a link for live updates.

Why this shape fits the clinic:

- **One deployable app, not microservices.** A clinic with no IT person can't run several moving parts. One app with a built-in background job is easier to host, monitor and hand over.
- **Server-rendered pages, no front-end framework.** The patient page has to be under 200 KB and load in under 5 seconds on 3G. Plain HTML plus a few lines of JavaScript meets that easily; a framework bundle alone can exceed it.
- **Managed hosting and database.** Backups, HTTPS and patching are handled by the provider, so nobody at the clinic has to do them.
- **SMS as the only channel to patients.** Every phone, smart or basic, can receive a text. The same provider handles outbound links and alerts, and inbound "WAIT" requests from basic phones.
- **Names for staff eyes only.** Staff need names to run the day, so the app stores name, phone and appointment time, encrypted and behind individual staff logins. No name ever appears on a patient-facing page or in a text, so a forwarded link or message reveals nothing.

**Suggested stack** (any equivalent works): Python (FastAPI) or Node (Express) app on a managed platform such as Render, Fly.io or Google Cloud Run; managed PostgreSQL; Twilio for SMS. If the clinic is in the US, pick a host, database and SMS provider that will sign a HIPAA Business Associate Agreement (BAA).

## Architecture

&#91;embedded content: system architecture · 7 parts\]

All three screens and inbound WAIT texts go through the queue service, which is the only part that writes to the database. Each staff action triggers the estimator; the notifier sends pre-arrival texts on a timer and turns any worse estimate into a delay text.

## Components

The system has eight parts: three screens, a queue service at the centre, an estimator and a notifier inside the app, plus the database and the SMS provider outside it.

### 1. Patient status page

- **URL:** `/w/{token}`, where `token` is a random 22-character string (128 bits). It can't be guessed.
- **Content, top to bottom:** queue position ("3rd in line"), wait range ("about 20–35 min"), the word "Estimate", and "Updated 1 min ago". The clinic's phone number sits at the foot of the page. Nothing else is needed above the fold.
- **Build:** server-rendered HTML with inline CSS. Text at least 18pt, dark on white, single column. No web fonts, images or third-party scripts. Target size under 50 KB, well inside the 200 KB limit.
- **Refresh:** a small script calls `/api/w/{token}` every 60 seconds and updates the two numbers. If JavaScript is off, a `<meta http-equiv="refresh" content="60">` tag reloads the page instead.
- **End states:** "It's your turn — please come to the front desk", "Your visit is complete", or "This link has expired" after the day ends. Before check-in, a booked patient sees an expected start time instead of a queue position ("Expected to be seen about 10:45–11:00").

### 2. Appointments screen (replaces the paper book)

- **Where:** `/staff/appointments`, behind the same staff login. Staff use it while on the phone with a caller.
- **Day view:** a column of time slots for the chosen date (one column per doctor if there is more than one). Free slots are blank; booked slots show the patient's name and phone ending ("Ravi K. · …4821"). Arrows move to the next or previous day; bookings can be made for any future date.
- **Book:** tap a free slot, type name and phone, save. Target: under 20 seconds, about as fast as writing in the book. Saving texts the patient a booking confirmation with the date and time (see Notifier).
- **Row actions:** **Check in** (one tap when the patient arrives; moves them into today's queue without retyping anything), **Edit** (time or phone), **Cancel** (no texts are sent).
- **Statuses:** Booked → Texted → Checked in, or No-show / Cancelled.
- **Print day sheet:** a one-page list of the day's bookings, printed each morning. If the internet or app is down, staff carry on with paper and enter the day's visits later.
- **Scope limit:** slots, name, phone and doctor only. No recurring appointments, patient history or reminders for other days in v1.

### 3. Staff console (today's queue)

- **Where:** a browser on the front-desk computer or tablet, at `/staff`. Each staff member signs in with their own login; the session lasts until closing. The screen should face away from the waiting room.
- **Check in:** booked patients are checked in with one tap from the Appointments screen. Walk-ins use a short form: name, phone number, **Check in**. Either way the visit enters the queue and the patient gets a text. Target: under 10 seconds.
- **Today so far:** a row of totals at the top — **Appointments booked** (today's bookings, not counting cancellations), **In clinic now** (checked in or in a visit), **Completed**, **Average visit time** (start to end, completed visits) and **Average wait time** (check-in to start, booked and walk-in). Averages use today's actual times and show "—" until there is one.
- **Queue list:** one row per waiting patient showing name, phone ending, type (booked / walk-in), wait so far, and a single button — **Start visit** (or **End visit** once started). One tap per action.
- **Clinic status:** a **Clinic open** button at the start of the day, and a **Running late** action ("opening at 10:30" or "doctor delayed 30 min") that shifts every estimate.
- **Doctors in session:** a 1–2–3 selector so the estimate knows how many patients are seen in parallel.
- **Housekeeping:** **No-show** and **Undo** on each row, so a mis-tap can be fixed without calling anyone.

### 4. Queue service (the core)

The API both screens talk to. It owns the visit state machine and is the only part that writes to the database.

- **States:** `booked` → `waiting` → `in_visit` → `done`, plus `no_show` and `cancelled`. Walk-ins start at `waiting`.
- **Endpoints:** `POST /api/appointments`, `PATCH /api/appointments/{id}`, `GET /api/appointments?date=`, `POST /api/visits` (walk-in check-in), `POST /api/visits/{id}/check-in` (booked patient arrives), `POST /api/visits/{id}/start`, `POST /api/visits/{id}/end`, `POST /api/visits/{id}/no-show`, `POST /api/clinic/open`, `POST /api/clinic/delay`, `GET /api/queue` (staff), `GET /api/w/{token}` (patient), `POST /api/sms/inbound` (provider webhook).
- **After every state change:** writes the change and an audit entry, then calls the estimator to recompute every booked and waiting patient's estimate for today, then hands any changed estimates to the notifier.

### 5. Estimator

A pure function — no database writes, no side effects — that takes today's queue and returns a position and wait range for each waiting patient. It is a small simulation that reduces to the formula in the Wait-time estimation section when everyone is present (see "How the estimator applies the formula").

### 6. Notifier

Decides who gets a text and sends it through the SMS provider.

- **Every text carries the answer, not just a link.** The numbers are in the message itself, so a patient with no mobile data still gets them; the link is only for live updates.
- **Booking confirmation:** as soon as an appointment is created, sends "Your appointment at \[Clinic\] is booked for Tue 10 Nov, 10:30 am. We'll text you about 90 min before with the expected start time." Same-day bookings also get "Reply WAIT for an update." (WAIT replies only match today's visits), plus the clinic phone if set. The "we'll text you" sentence is left out when the appointment is already within the pre-arrival lead (the pre-arrival text follows straight away). Sent even in quiet hours, because the patient has just asked for the booking. No name, no link (the live page only matters on the day).
- **Text layout:** every text that carries an estimate (pre-arrival, check-in, delay, WAIT reply) has the same three lines, then the link and a note:

  ```
  [Clinic] is running behind. New estimate:
  Scheduled visit time: 7:15 pm
  Estimated delay: 30 min
  Estimated time: 7:40-7:55 pm
  Reply WAIT for an update. Live: [link]
  Times are estimates based on average wait and visit times; actual time may vary.
  ```

  *Estimated delay* is the expected start minus the appointment, to the nearest 5 minutes; when there is none it reads "None, on time". Walk-ins have no scheduled time, so they get "Scheduled visit time: Walk-in (checked in 5:12 pm)" and "Estimated wait: about 20–35 min" in place of the delay line.
- **Plain characters only:** texts use the basic SMS alphabet. Typographic dashes, curly quotes and "…" (including any a manager types) are swapped for plain ones before sending, because a single such character makes the provider send the whole text in Unicode, at 70 characters per message instead of 160: the delay text above bills as 2 messages instead of 5.
- **Pre-arrival (booked patients):** about 90 minutes before the appointment, opening "Your visit at \[Clinic\]:".
- **On check-in:** opening "You're checked in at \[Clinic\]. You're 3rd in line.". Walk-ins get their link here.
- **On a worse estimate:** if a patient's expected start time is more than 15 minutes later than the last one they were told (by any text), sends the delay text above. This covers late opening and doctor delays too.
- **Limits:** at most one delay text per patient every 15 minutes, none during quiet hours (8 pm–7 am by default; pre-arrival texts due then are deferred to 7 am), and none to a patient expected to be seen within the next 15 minutes anyway, so a busy afternoon doesn't produce a flood of texts.
- **Delivery:** a background job runs every minute, sends pre-arrival texts that are due, and retries any queued or failed sends.

### 7. SMS gateway (outside service)

- **Outbound:** booking confirmations, pre-arrival texts, check-in texts, delay alerts, WAIT replies and manager-written texts.
- **Development outbox:** with `DEV_MODE` on, texts are not sent but listed at `/dev/outbox` (staff login required) with the patient's **name** and **complete phone number** ("To"), kind, status and body, so staff can check exactly who would have been texted what. It is never available in production.
- **Inbound:** any patient — smartphone with no data, or basic phone — texts "WAIT" to the clinic number. The provider calls `/api/sms/inbound`, and the reply uses the same three-line layout (scheduled time, estimated delay or wait, estimated time) and the note.
- **Walk-in check before travelling:** someone with no booking can text "WAIT" and get the current walk-in wait ("Walk-in wait now: about 40–55 min").

### 8. Database

Managed PostgreSQL holding the visits, the notification log and the audit log. Tables are in the Data model section.

## How the pieces interact

Every flow goes through the queue service. The screens never talk to the database or the SMS provider directly.

**Flow A — Booking a slot (patient calls the clinic)**

1. Staff open the Appointments screen on the requested date and tap a free slot.
2. They type the caller's name and phone number and save. The console sends `POST /api/appointments`.
3. The queue service creates a visit in state `booked`, with the appointment time, an encrypted name and phone, and a link token. It writes an audit entry.
4. The notifier texts a booking confirmation with the appointment date and time (no name), and the booking appears in the slot.

**Flow B — Pre-arrival text (the "should I leave yet?" moment)**

1. Every minute, the background job looks for `booked` visits whose appointment is about 90 minutes away and that haven't been texted.
2. It asks the estimator for that patient's expected start: the later of their appointment time and when the queue is likely to reach them.
3. The notifier texts the expected start range, the WAIT option and the link. The visit is marked Texted.
4. From then on, any change that pushes their expected start more than 15 minutes later triggers a delay text, so they can leave later.

**Flow C — Check-in on arrival**

1. **Booked patient:** staff tap **Check in** on their row in the Appointments screen (`POST /api/visits/{id}/check-in`). **Walk-in:** staff enter name and phone and tap **Check in** (`POST /api/visits`).
2. The queue service moves the visit to `waiting` (or creates it, for a walk-in), records the check-in time and writes an audit entry.
3. The estimator recomputes the queue; the patient gets a position and wait range.
4. The notifier queues the check-in text with the numbers and the link; the SMS provider delivers it, usually within seconds.
5. The queue list shows the new row with the patient's name.

**Flow D — Start or end a visit**

1. Staff tap **Start visit** (or **End visit**) on a row.
2. The console sends `POST /api/visits/{id}/start` (or `/end`). The server records the timestamp.
3. On **End visit**, the visit's length (end minus start) feeds today's average visit length.
4. The estimator recomputes every waiting patient's position and range.
5. The notifier compares each new estimate with the last one that patient was told. Anyone now more than 15 minutes later gets a delay text.

**Flow E — Patient checks the page**

1. The patient taps the link; the browser requests `/w/{token}`.
2. The queue service looks up the visit by token and returns the page with position, range and "Updated" time. No name.
3. Every 60 seconds the page calls `/api/w/{token}` and redraws the two numbers. Because the estimate is recomputed on every staff action, the page is never more than 60 seconds behind the front desk.

**Flow F — Patient with no mobile data or a basic phone**

1. Every text already contains the position and wait range, so a patient with no data has the answer without opening the link.
2. For a fresh figure, the patient texts "WAIT" to the clinic number. This needs only a mobile signal.
3. The SMS provider forwards the message to `POST /api/sms/inbound`.
4. The queue service matches the sender's phone number (by keyed hash of its last 10 digits, so it matches with or without a country code) to today's booked or waiting visit and replies with the current position and range (or expected start time, if not yet checked in). No name is included.
5. A number with no visit today gets the current walk-in wait instead. With no mobile signal at all, the fallback is calling the clinic, whose number is in every text and on the page.

**Flow G — Clinic opens late, or a doctor is delayed**

1. Opening time is stored in config. Until staff tap **Clinic open**, every estimate counts from opening time, not from now.
2. If opening time passes with no tap, the estimator adds the minutes since opening to every estimate (capped at 60 minutes, since by then staff have more likely forgotten to tap), and the staff console prompts: "Clinic not marked open — running late?" The first **Start visit** of the day also marks the clinic open.
3. Staff can tap **Running late** and enter a new time ("opening at 10:30") or a delay ("doctor delayed 30 min").
4. The estimator shifts every booked and waiting patient's estimate.
5. The notifier texts everyone whose estimate moved by more than 15 minutes, including booked patients who haven't left home yet.

## Wait-time estimation

The estimate is a simple formula based on today's real visit lengths, recomputed on every staff action. It needs no machine learning and no historical data on day one.

**Inputs**

- **S** — minutes until doctors start seeing patients. Before **Clinic open** is tapped, it is opening time minus now (or minutes past opening, up to 60, if late). A **Running late** entry sets a time before which nobody is seen (`delay_until`), and S is at least the minutes until then. Once the clinic is open and running, S = 0.
- **A** — average visit length today, in minutes: the mean of today's completed visits. Until 3 visits have completed, use the clinic's default (set in config, e.g. 15 min).
- **D** — doctors in session, from the staff console selector. Before opening, use the number expected that day from config, so the formula never divides by zero.
- **k** — patients ahead of this one: those already `waiting`, plus booked patients with an earlier appointment who are expected to arrive first.
- **r** — for each visit currently `in_visit`, the time it probably has left: A minus minutes elapsed, never below 2.

**Formula**

```latex
W = S + \frac{\sum r_i + k \cdot A}{D}
```

In words: any wait for the clinic to open, plus the time left on visits in progress, plus a full average visit for each person ahead, shared across the doctors working.

**Turning W into what the patient sees**

- **Range:** low = W − 7, high = W + 8, both rounded to the nearest 5 minutes, low never below 0. That keeps the range at 15 minutes, the requirement's maximum.
- **Checked-in patients** see position (k + 1, "3rd in line") and the wait range.
- **Booked patients not yet arrived** see an expected start time: the later of their appointment time and now + W, shown as a 15-minute clock range ("about 10:45–11:00"). A booked patient is never told to come earlier than their appointment.
- **Queue order (v1):** booked patients by appointment time once checked in; walk-ins by check-in time, filling gaps between appointments. Exactly how walk-ins slot in is an open question for the clinic.

**How the estimator applies the formula**

The estimator is a small simulation rather than the bare formula. Each of the D doctors becomes free at some time (now plus r for a visit in progress, or now plus S if the clinic hasn't started). Patients are taken in queue order — booked patients by appointment time, walk-ins by check-in time — and each starts when the earliest doctor is free, and, for a booked patient who hasn't arrived, not before their appointment. That doctor is then busy for A minutes. When everyone ahead is already in the waiting room this gives exactly W = S + (Σr + k·A)/D; it also handles gaps between appointments (walk-ins fill them) and booked patients who haven't arrived yet. A booked patient more than 30 minutes past their appointment who hasn't arrived no longer holds a place: they're estimated at the back of the line. There is one queue for the whole clinic.

**Measuring accuracy (the ±15 min, 80% target)**

1. At check-in, the estimator's midpoint W is saved on the visit as `first_estimate_min`.
2. When staff tap **Start visit**, the actual wait (start time minus check-in time) is saved as `actual_wait_min`.
3. A nightly job counts the visits where the two differ by 15 minutes or less, and stores the day's percentage.
4. The staff console shows a 7-day figure ("Estimates within 15 min: 84% this week"). If it falls below 80%, the default visit length or the formula needs tuning.

## Data model

Six tables. Name and phone are the only personal details; both are encrypted, shown only to logged-in staff, and deleted 30 days after the visit.

| Table | Key fields | Purpose | Kept for |
| --- | --- | --- | --- |
| `visits` | `id`, `patient_name_encrypted`, `phone_encrypted`, `appointment_at` (empty for walk-ins), `doctor`, `type` (booked / walk-in), `state` (booked / waiting / in\_visit / done / no\_show / cancelled), `link_token` (random, 128-bit), `pre_arrival_sent_at`, `checked_in_at`, `started_at`, `ended_at`, `first_estimate_min`, `actual_wait_min`, `last_notified_start_at`, `last_delay_text_at`, `phone_hash` (keyed hash of the last 10 digits, for matching WAIT texts), `phone_last4` | One row per appointment or walk-in; drives the Appointments screen, the queue and the patient page | Link token cleared at midnight on the visit day; name and phone deleted after 30 days; timing fields kept 90 days for accuracy reporting |
| `clinic_days` | `date`, `opening_time`, `closing_time`, `opened_at`, `delay_until`, `doctors_expected`, `doctors_in_session`, `last_staff_action_at` | Each day's opening, late-start and staffing, used by the estimator | 90 days |
| `clinic_config` | `default_visit_min`, `slot_length_min`, `default_opening_time`, `pre_arrival_lead_min` (90), `alert_threshold_min` (15), `quiet_hours`, `clinic_sms_number`, `clinic_phone` | Settings the manager can change without code | Permanent |
| `notifications` | `id`, `visit_id`, `kind` (booking / pre-arrival / check-in / delay / reply / adhoc), `status` (queued / sent / failed), `attempts`, `sent_at` | Outbox for the notifier; lets failed texts retry | 30 days |
| `audit_log` | `id`, `staff_user`, `visit_id`, `action`, `from_state`, `to_state`, `undone`, `at` | Who booked, viewed or changed what, for Undo and privacy reviews | 1 year |
| `staff_users` | `id`, `name`, `password_hash`, `role` (front desk / manager), `active` | Individual logins, so every action is traceable to a person | While employed |

`daily_accuracy` (date, visits counted, % within 15 min) can be a fifth small table or a database view over `visits`.

## Privacy, security and failure handling

The main privacy protection is keeping names off every patient-facing surface, and storing them encrypted and briefly. The main reliability risk is staff forgetting to tap, so the design watches for that.

**Privacy and security**

- **Names for staff only.** Name and phone are stored because staff need them to book and run the day. They appear only on staff screens, behind a login, and never on a patient page or in a text. No date of birth, reason for visit or clinical notes are collected.
- **Encrypted and time-limited.** Name and phone are encrypted in the database and deleted 30 days after the visit, a shorter life than the paper book they replace.
- **Individual staff logins.** No shared clinic password. Every booking, check-in and edit is written to the audit log against a named staff user.
- **Unguessable links.** The patient link uses a 128-bit random token. Changing characters in the URL lands on "link not found", never on another patient.
- **Links expire.** Tokens are cleared at midnight, so an old forwarded text shows nothing.
- **Inbound texts matched by sender.** A WAIT reply goes only to the phone number on the visit, so nobody can ask about someone else.
- **Screen placement.** The front-desk screen faces away from the waiting room, and the console locks after 5 minutes idle.
- **Transport.** HTTPS only, enforced by the host.
- **Rate limits.** The patient endpoint and inbound SMS are rate-limited per IP or phone number.
- **Vendor agreements and law.** Where HIPAA applies, a signed BAA with the host, database and SMS providers before any real patient data goes in; in India, the equivalent duties under the DPDP Act.

**Failure handling**

| What goes wrong | What the system does |
| --- | --- |
| Clinic opens late | Until **Clinic open** is tapped, estimates count from opening time; once it passes, the console prompts staff and estimates slip automatically. A **Running late** entry shifts everyone and triggers delay texts (Flow G). |
| A doctor is delayed or leaves mid-session | Staff change **Doctors in session** or enter a delay; estimates and texts follow. |
| Patient has no mobile data | Every text carries the position and range; replying WAIT gets a fresh figure by SMS (Flow F). |
| Patient has no mobile signal | Nothing digital reaches them; every text and the page show the clinic's phone number. |
| Staff forget to tap **End visit** | When a visit runs past 2× today's average, the row turns amber with a prompt: "Still in progress?" |
| Staff forget to update for a while | If no staff action for 20 minutes while patients are waiting, the patient page adds "Estimate may be out of date — call \[clinic number\]". |
| SMS provider is down | Texts stay `queued` in the outbox and retry every minute for up to 30 minutes; the console shows "Texts delayed". |
| Clinic internet drops | The console shows an offline banner and blocks taps. Staff use the printed day sheet and paper for new bookings, then enter them when back online. The patient page still loads and shows its last update time. |
| A mis-tap | **Undo** on the row reverses the last action within 2 minutes; the audit log keeps both entries. |
| App crashes or is redeployed | All state is in the database, so a restart loses nothing; the managed platform restarts the app automatically. |

## Requirement traceability

Every measurable line in the product requirement maps to a part of this design and a way to check it.

| Requirement | Delivered by | How to check |
| --- | --- | --- |
| Patient has a written record of their booking | Booking confirmation text (Flow A) | Book a slot; confirm the text arrives within a minute with the right date and time and no name |
| Tells a patient whether to leave for the clinic yet | Pre-arrival text \~90 min before the appointment, plus delay alerts (Flows B, G) | Book a test slot; confirm the text arrives 90 min ahead with an expected start range |
| Walk-ins can check before travelling | Inbound WAIT returns the current walk-in wait (Flow F) | Text WAIT from a number with no booking |
| Nothing to install; no login for patients | Patient status page via SMS link | Open the link on a phone that has never visited the site |
| Works without mobile data | Numbers in every text; WAIT reply (Flow F) | Turn off mobile data; confirm texts and WAIT replies still give the answer |
| Queue position + range no wider than 15 min, labelled as an estimate | Estimator range rule; page layout | Inspect the page across 20 queue states |
| Readable one-handed in a parking lot; answer first; text ≥ 18pt, high contrast | Patient page layout and CSS | Outdoor check on a phone in sunlight; contrast tool (WCAG AA or better) |
| Page under 200 KB | No framework, fonts or images | Browser dev tools, page weight |
| Loads in under 5 s on 3G | Server-rendered HTML, small payload | Browser dev tools with "Slow 3G" throttling |
| Refreshes every 60 s | Polling script + meta refresh fallback | Change the queue; page updates within 60 s |
| Text if estimate grows by more than 15 min, including late opening | Notifier threshold rule; S term in the estimator (Flows D, G) | Tap **Running late** by 30 min; confirm every affected patient is texted |
| Booking as fast as the paper book | Appointments screen | Time 10 bookings; each under 20 s |
| Three one-tap queue actions, each under 10 s | Staff console + Check in on the Appointments screen | Time 10 check-ins and 10 start/end taps |
| Staff record name, phone, appointment time and status; names visible only to logged-in staff | Data model; staff login; patient page and text templates | Review the schema; log out and confirm no name is reachable |
| No patient name on any patient screen or text | Patient page, all SMS templates | Review templates; try altered URLs; text WAIT from another phone |
| Within ±15 min for 80% of patients over a week | Estimator + nightly accuracy job | 7-day figure on the staff console |

## Open questions

- [ ] Which country is the clinic in? This decides the privacy law (HIPAA, India's DPDP Act, etc.) and the SMS provider.
- [ ] How should walk-ins be ordered alongside booked patients: strictly by arrival, or slotted into gaps between appointments?
- [ ] Is there one queue for the whole clinic, or one per doctor?
- [ ] How long do most patients take to reach the clinic? This sets the pre-arrival lead time (90 min is a placeholder).
- [ ] What slot length do bookings use today (10, 15, 20 min)?
- [ ] What is a realistic default visit length for the first few patients of the day?
- [ ] Is 30 days the right time to keep names and phone numbers?
- [ ] Which languages do the texts and patient page need?
- [ ] Who at the clinic owns the staff logins and the monthly SMS bill?
- [x] Does the clinic have an appointment system? No — bookings are on paper, so the app includes an Appointments screen that replaces the book.
- [x] Can staff see patient names? Yes — on staff screens only, behind individual logins.
