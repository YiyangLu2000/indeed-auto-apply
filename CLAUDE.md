# CLAUDE.md — Indeed Auto-Apply (minimal backend module)

Shared reference for this project. Read this before writing or changing code.

## 1. What this is

A small, reusable backend module that drives a minimal **end-to-end Indeed
auto-apply workflow**:

1. A human logs into Indeed **once** in a visible browser.
2. The module **captures** that authenticated session and stores it
   **encrypted at rest**. No browser stays running.
3. Later runs **restore** the session, search Indeed for a few relevant
   postings, and drive the Indeed Apply form using a local candidate profile.
4. Every application's status is tracked through an explicit state machine and
   persisted to SQLite, with full status history.
5. Whenever Indeed asks for a human (SMS code, email code, CAPTCHA, passkey
   prompt, "verify it's you", anti-bot wall), the workflow **stops**, records
   `MANUAL_ACTION_REQUIRED`, and waits. A human solves it and calls
   `resume(job_id)`.

It is a take-home-sized module, **not** a production system. Favor clarity and
readability over robustness, retries, and scale.

## 2. Hard constraints (do not violate)

- **Never bypass, weaken, or evade** any Indeed verification or anti-bot
  mechanism: CAPTCHA, SMS/OTP, email passcode, passkey/2FA, device checks,
  rate-limit or bot walls. No CAPTCHA solvers, no OTP interception, no
  fingerprint spoofing, no `playwright-stealth` / evasion plugins.
- On encountering any such check: transition to `MANUAL_ACTION_REQUIRED`,
  persist state and a human-readable reason, close the browser, stop.
- **Only the owner's real personal data.** One real candidate profile. Apply
  only to roles reasonably relevant to that background.
- Session capture only happens **after a human has manually completed login**
  (email + emailed passcode + dismiss passkey prompt) in a headed browser.
- Keep the surface area small. No web frontend. No account creation code.

## 3. Stack

- **Python 3.12**, standard `venv` + `pip` + `requirements.txt`
- **Playwright** (async API), Chromium
- **SQLite** via stdlib `sqlite3`
- **cryptography** (`Fernet`) for session encryption at rest
- **Typer** (or stdlib `argparse`) for a minimal CLI
- Config/secrets via environment variables and a git-ignored `.secrets/` dir

## 4. Architecture

```
                       profile.json                .secrets/session.key
                            │                             │
                            ▼                             ▼
  CLI ──> profile ──────────────────────┐        session_manager
   │        (candidate data)            │      (encrypt / decrypt +
   │                                    │       storage_state I/O)
   │                                    ▼             │
   ├──> job_selector ──> [3-5 job URLs]─┼─────────────┤
   │       (search Indeed using          │            │ restored context
   │        restored session)            ▼            ▼
   └──> apply_runner ───────────> Playwright (Chromium, ephemeral)
             │                             │
             │  status transitions         │  manual check detected
             ▼                             ▼
        state_machine ───────────────> storage (SQLite)
        (validates transitions)        applications + status_history
```

Data flow: the **CLI** wires modules together. `profile` and `session_manager`
provide inputs. `job_selector` produces a short list of postings.
`apply_runner` opens one ephemeral Playwright context per run (or per job),
walks the Indeed Apply form, and asks `state_machine` to move each application
between states. `storage` persists applications and every transition.

## 5. Modules

### 5.1 `session_manager`
Purpose: capture and restore the Indeed login session **without keeping a
browser running**, encrypted at rest.

- `capture()` — launches a **headed** Chromium, navigates to Indeed, and waits
  for the human to finish logging in (detects a logged-in signal, e.g. the
  account menu / a known authenticated cookie). Then calls
  `context.storage_state()` (cookies + localStorage + origins), encrypts the
  JSON, and writes it to `.secrets/session.enc`. Prints a confirmation.
- `restore()` — decrypts `.secrets/session.enc` to an in-memory dict and
  returns it for `browser.new_context(storage_state=...)`. Never writes
  plaintext to disk.
- `check_validity(context) -> (bool, reason)` — after a restored context is
  opened, this is how the workflow decides "still logged in" vs "expired /
  logged out" **before** doing any real work. It is a read-only probe: it
  navigates and inspects, never interacts with a challenge.

  Procedure:
  1. `page.goto("https://www.indeed.com/", wait_until="domcontentloaded")`.
  2. **URL signal.** If the landing URL's host/path matches a login or
     challenge surface — `secure.indeed.com/auth`, `secure.indeed.com/account/login`,
     `*/account/login`, contains `/auth` or `?__ceViewedRedirect`, or the
     Cloudflare/anti-bot interstitial (`/blocked`, `challenge`, hCaptcha /
     "Additional Verification Required" text) — return `(False, "...")`. A
     bot/CAPTCHA wall here is reported by the CLI as a manual step, not
     treated as a normal "expired" (human re-runs `capture()` in a headed
     browser and solves it there).
  3. **Cookie signal.** Read `context.cookies()`. Treat the session as
     *candidate-valid* only if the Indeed auth cookies are present and
     unexpired: `CTK` and at least one of `SHOE`, `PPID`, or `SURF` (the
     login-bearing cookies Indeed sets post-auth), each with `expires` in the
     future. Absence of `CTK`, or only anonymous cookies (`CSRF`, `INDEED_CSRF_TOKEN`
     alone), → `(False, "no auth cookie")`.
  4. **DOM signal (authoritative).** On the loaded homepage, look for a
     logged-in marker with a short timeout (~5s):
     - present ⇒ logged in: `[data-gnav-element-name="AccountMenu"]`, the
       account avatar button `#AccountMenu`, or a `gnav-*` element whose text
       is the profile's first name / email.
     - present ⇒ logged out: a "Sign in" link
       (`a[data-gnav-element-name="SignIn"]`, `text=/^sign in$/i`).
     DOM beats cookies: cookies present but "Sign in" showing ⇒
     `(False, "session cookie stale — Indeed shows signed-out")`.
  5. Optional confirm: `page.goto("https://myjobs.indeed.com/applied")` (or
     `https://profile.indeed.com/`) — a 200 with profile content confirms;
     a redirect back to `secure.indeed.com/auth` confirms expired.
  Return `(True, "ok")` only when the DOM logged-in marker is found (steps 3
  and 5 are corroborating, not sufficient alone). Any `(False, reason)` from
  a non-challenge cause ⇒ CLI tells the human to re-run `login` +
  `capture-session`; a challenge cause ⇒ `MANUAL_ACTION_REQUIRED` semantics.
- `is_present()` / `age()` — helpers for the CLI to report session status.
- **Encryption**: symmetric `Fernet`. Key resolution order:
  `INDEED_SESSION_KEY` (raw urlsafe-base64 key) → `INDEED_SESSION_KEY_FILE`
  path → default `.secrets/session.key`. `.secrets/` is git-ignored. A
  `session_manager keygen` helper creates the key file if missing.
- Session is assumed to expire; when `check_validity()` or a restored call
  hits a login wall, `apply_runner` reports it and the human re-runs
  `capture()`.

### 5.2 `profile`
Purpose: load and validate the candidate profile from `profile.json`.

- Fields: `contact` (name, email, phone, location), `resume_path`,
  `work_experience[]`, `education[]`, `answers` (reusable screening answers:
  work authorization, sponsorship, years of experience, notice period,
  desired pay, etc.), `job_preferences` (`titles[]`, `location`, `remote`,
  `min_salary`, `keywords_exclude[]`, `limit`).
- `load(path="profile.json") -> Profile` — parse, check the resume file
  exists, fail fast with a clear message on missing required fields.
- `profile.example.json` is committed; the real `profile.json` is git-ignored.

### 5.3 `job_selector`
Purpose: given search criteria, return **3-5** relevant Indeed postings.

- Input: `job_preferences` from `profile` plus optional CLI overrides
  (`--query`, `--location`, `--limit`).
- Uses the **restored authenticated context** to load Indeed search results,
  reads posting cards, and returns `[{job_key, title, company, location, url,
  indeed_apply: bool}]`.
- **Only keeps postings that use Indeed's in-platform apply** ("Easily apply" /
  Indeed Apply). Postings that redirect to an external ATS are dropped (they
  cannot be completed by this module) — logged, not applied.
- De-dupes against `storage`: never returns a `job_key` that already has an
  application row.
- If Indeed shows a bot wall / CAPTCHA here, it raises a typed
  `ManualActionRequired` so the CLI can report it cleanly (no application rows
  are created).

### 5.4 `apply_runner`
Purpose: drive Playwright through the Indeed Apply flow for one job using
profile data.

- `apply(job, profile, *, confirm=False, headless=False)`:
  1. Create/transition the application row to `IN_PROGRESS`.
  2. Open an ephemeral context from the restored session, go to the job,
     click Apply.
  3. Step through the wizard: contact info, resume (upload `resume_path` or
     pick the stored Indeed resume), work experience, education.
  4. **Screening questions** — *conservative matching, pause by default*.
     Auto-answer a question **only** when the match is high-confidence, and
     leave everything else for a human:
     - The question's label maps to a known key in `profile.answers` by an
       exact or near-exact normalized match (lowercased, trimmed, punctuation
       stripped) against that key's label / known aliases — not a fuzzy or
       substring guess.
     - **And** the control is a closed type we can fill unambiguously:
       yes/no, single-select radio, dropdown, or a numeric field, where the
       profile value maps cleanly onto exactly one available option.
     - Any of the following → `MANUAL_ACTION_REQUIRED` (reason names the
       question verbatim), save, stop — no best-effort answer:
       free-text / textarea, multi-select, a required question with no
       mapped key, an ambiguous or partial label match, a mapped value that
       does not correspond to exactly one offered option, or a question type
       the runner does not explicitly handle.
     When in doubt, pause. It is always acceptable to stop and ask the human;
     it is never acceptable to submit a guessed screening answer.
  5. Reach Indeed's **review** screen.
     - `confirm=True` → click the final submit, verify the confirmation
       screen, transition to `SUBMITTED`.
     - `confirm=False` (default) → transition to `MANUAL_ACTION_REQUIRED`
       (reason: "awaiting human submit at review screen").
  6. Any detected verification/anti-bot/login wall at any step →
     `MANUAL_ACTION_REQUIRED` with a specific reason.
  7. Unexpected error (selector missing, navigation failure, timeout) →
     `FAILED` with the exception summary.
- `resume(job_id, *, confirm=False, headless=False)`: reload the application,
  transition `MANUAL_ACTION_REQUIRED -> IN_PROGRESS`, re-open the flow, and
  continue from the review/next step. Must be **idempotent** — safe to call
  again if it stops once more.
- Detection helpers: `_looks_like_captcha(page)`, `_looks_like_otp(page)`,
  `_looks_like_login_wall(page)` — checked after every navigation. These only
  *detect*; they never interact with the challenge.
- Screenshots + a short DOM snapshot are saved to `runs/<job_id>/` on every
  stop (manual or failed) to help the human.

### 5.5 `state_machine`
Purpose: the single authority on application status and legal transitions.

States: `PENDING`, `IN_PROGRESS`, `MANUAL_ACTION_REQUIRED`, `SUBMITTED`,
`FAILED`.

Allowed transitions:

| from                     | to                       |
|--------------------------|--------------------------|
| PENDING                  | IN_PROGRESS              |
| PENDING                  | FAILED                  |
| IN_PROGRESS              | MANUAL_ACTION_REQUIRED   |
| IN_PROGRESS              | SUBMITTED                |
| IN_PROGRESS              | FAILED                  |
| MANUAL_ACTION_REQUIRED  | IN_PROGRESS              |
| MANUAL_ACTION_REQUIRED  | FAILED                  |

- `SUBMITTED` and `FAILED` are **terminal** — no transitions out.
- `PENDING -> FAILED` covers a job that vanished or proved invalid before any
  work started (posting 404s, `job_key` no longer resolves).
- `transition(app, to_state, reason) ` validates against the table, raises
  `InvalidTransition` otherwise, stamps `updated_at`, and appends a
  `status_history` row. All status changes go through here — modules never
  write `status` directly.

### 5.6 `storage`
Purpose: SQLite persistence for applications and their status history.

- `applications`
  - `id` INTEGER PK
  - `job_key` TEXT UNIQUE  (Indeed `jk`)
  - `title`, `company`, `location`, `url` TEXT
  - `status` TEXT  (current state)
  - `manual_reason` TEXT NULL
  - `created_at`, `updated_at` TEXT (ISO-8601 UTC)
- `status_history`
  - `id` INTEGER PK
  - `application_id` INTEGER FK -> applications.id
  - `from_status` TEXT NULL
  - `to_status` TEXT
  - `reason` TEXT NULL
  - `created_at` TEXT
- API: `init_db()`, `upsert_job(...)`, `get(app_id)`, `get_by_job_key(...)`,
  `list(status=None)`, `record_transition(app_id, from, to, reason)`.
- Schema created idempotently on startup. DB path: `data/applications.db`
  (git-ignored), override with `INDEED_DB_PATH`.

## 6. CLI surface

```
python -m indeed_apply keygen                 # create .secrets/session.key
python -m indeed_apply login                  # headed browser; human logs in
python -m indeed_apply capture-session        # store storage_state, encrypted
python -m indeed_apply session-status         # present? age? valid?
python -m indeed_apply unblock                # visible browser; human clears an anti-bot wall, session re-saved
python -m indeed_apply select-jobs [--query .. --location .. --limit 5] [--headed]
python -m indeed_apply apply-all [--confirm] [--headless]
python -m indeed_apply apply <job_id> [--confirm] [--headless]
python -m indeed_apply resume <job_id> [--confirm] [--headless]
python -m indeed_apply status [--id N | --all]   # show apps + history
```

`login` and `capture-session` may be one command with a "press Enter when
done" pause. Default is **headed** and **no auto-submit** (`--confirm`
required to actually send an application).

`unblock` is the interactive form of the §7 anti-bot stop: when Indeed shows a
"Request Blocked" / "verify you are human" / "just a moment" wall, the command
opens a **visible** browser on Indeed with the restored session, waits for the
human to clear the wall themselves (or wait out a hard block) and press Enter,
then re-encrypts the now-cleared session so later headless commands reuse it.
It never interacts with the challenge — no auto-click, no solver, no token
injection.

`--browser chrome|msedge` (env `INDEED_BROWSER_CHANNEL`) drives the real
installed Chrome/Edge instead of Playwright's bundled Chromium. This is a
legitimate browser choice, **not** fingerprint spoofing — no stealth args, no
`navigator.webdriver` patching, no `--disable-blink-features`. It exists because
the bundled build is more often caught in a Cloudflare managed-challenge loop.

## 7. Manual verification & failure handling

- Detected challenge (CAPTCHA / OTP / email code / passkey / "verify it's
  you" / bot wall): `-> MANUAL_ACTION_REQUIRED`, store a specific
  `manual_reason`, dump screenshot + DOM to `runs/<job_id>/`, close browser,
  exit non-zero-but-clean. The CLI prints what the human must do.
- Human does the step themselves (in their own browser or by re-running
  `login`), then `resume <job_id>`.
- If a fresh login was needed, `capture-session` is re-run first so the new
  session is what `resume` restores.
- Genuine errors (missing selector, timeout, upload failure): `-> FAILED`
  with the exception summary; terminal.
- No automatic retries. A human decides whether to re-create the row.

## 8. Extending to multiple users (design sketch, not built)

- Add a `users` table (`id`, `label`, `email`); every `applications` and
  `session` row gets a `user_id` FK. `profile` becomes per-user
  (`profiles/<user_id>.json`).
- `session_manager` stores one encrypted blob per user
  (`.secrets/session_<user_id>.enc`) with a per-user key (or one KMS/master
  key + per-user derived key). Nothing else changes conceptually.
- CLI gains `--user <id>`; a worker/queue can iterate users, each run fully
  isolated (own context, own session, own DB rows).
- `MANUAL_ACTION_REQUIRED` becomes a per-user notification (email/Slack) with
  a resume link; the state machine and storage layer are already multi-user
  once the FK exists.
- Concurrency: one browser context per user; never share a context. SQLite is
  fine at this size; swap for Postgres if it grows.

## 9. Proposed file/folder structure

```
indeed-auto-apply/
├── CLAUDE.md
├── README.md                     # architecture / session / manual+failure / multi-user
├── requirements.txt
├── .gitignore                    # .secrets/, data/, runs/, profile.json, .venv/
├── .env.example                  # INDEED_SESSION_KEY_FILE, INDEED_DB_PATH, ...
├── profile.example.json
├── profile.json                  # real profile — git-ignored
├── .secrets/                     # git-ignored
│   ├── session.key
│   └── session.enc
├── data/
│   └── applications.db           # git-ignored
├── runs/                         # git-ignored; screenshots + DOM on stop
│   └── <job_id>/
├── src/
│   └── indeed_apply/
│       ├── __init__.py
│       ├── __main__.py           # CLI entrypoint (python -m indeed_apply)
│       ├── cli.py
│       ├── config.py             # env + path resolution
│       ├── session_manager.py
│       ├── profile.py
│       ├── job_selector.py
│       ├── apply_runner.py
│       ├── state_machine.py
│       ├── storage.py
│       └── errors.py             # ManualActionRequired, InvalidTransition, ...
└── tests/
    ├── test_state_machine.py     # transition table, terminal states
    ├── test_storage.py           # upsert, history, dedup (temp sqlite)
    └── test_profile.py           # load/validate profile.example.json
```

Tests must run **without touching Indeed**: `state_machine`, `storage`, and
`profile` are pure and covered by unit tests. `job_selector` and
`apply_runner` are exercised manually against live Indeed (documented in
README) and kept thin so most logic lives in the testable modules.

## 10. Build order (once this doc is approved)

1. `errors.py`, `config.py`, `storage.py` (+ tests)
2. `state_machine.py` (+ tests)
3. `profile.py` (+ tests), `profile.example.json`
4. `session_manager.py`, `keygen` / `login` / `capture-session` CLI
5. `job_selector.py`
6. `apply_runner.py` + `apply` / `resume` / `status` CLI
7. `README.md`
