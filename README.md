# Indeed Auto-Apply

A small, reusable backend module that drives a minimal end-to-end **Indeed
auto-apply** workflow:

1. A human logs into Indeed **once** in a visible browser.
2. The module **captures** that authenticated session and stores it
   **encrypted at rest** (Fernet). No browser stays running.
3. Later runs **restore** the session, search Indeed for a few relevant
   postings, and drive the Indeed Apply form from a local candidate profile.
4. Every application's status moves through an explicit **state machine** and is
   persisted to SQLite with full status history.
5. Whenever Indeed asks for a human (SMS/email code, CAPTCHA, passkey, "verify
   it's you", bot wall), the workflow **stops**, records
   `MANUAL_ACTION_REQUIRED` with a reason, closes the browser, and waits. A
   human handles it and runs `resume <job_id>`.

It is a take-home-sized module, **not** a production system. See `CLAUDE.md` for
the full design and hard constraints. The most important constraint: this module
**never bypasses, weakens, or evades** any Indeed verification or anti-bot
mechanism — it only *detects* them and hands control back to a human.

---

## Architecture

```
                       profile.json                .secrets/session.key
                            │                             │
                            ▼                             ▼
  CLI ──> profile ──────────────────────┐        session_manager
   │        (candidate data)            │      (encrypt / decrypt +
   │                                    │       storage_state I/O +
   │                                    │       check_validity)
   │                                    ▼             │
   ├──> job_selector ──> [3-5 job rows]─┼─────────────┤ restored context
   │       (search Indeed using          │            │
   │        restored session)            ▼            ▼
   └──> apply_runner ───────────> Playwright (Chromium, ephemeral)
             │                             │
             │  status transitions         │  manual check detected
             ▼                             ▼
        state_machine ───────────────> storage (SQLite)
        (validates transitions)        applications + status_history
```

| module           | responsibility |
|------------------|----------------|
| `config`         | env + path resolution (one place reads `os.environ`) |
| `errors`         | typed exceptions the CLI catches for clean messages |
| `storage`        | SQLite: `applications` + append-only `status_history` |
| `state_machine`  | the **only** writer of `status`; validates every transition |
| `profile`        | load + strict-validate `profile.json` |
| `session_manager`| capture / restore / **check_validity** the Indeed session |
| `job_selector`   | 3-5 fresh, Indeed-Apply-able postings, deduped against storage |
| `apply_runner`   | drive the Apply wizard; conservative screening answers |
| `cli`            | wires it together (`python -m indeed_apply ...`) |

Pure, unit-tested modules: `state_machine`, `storage`, `profile`, plus the
`session_manager` crypto round-trip and the `apply_runner.resolve_answer`
matcher. `job_selector` and the browser parts of `apply_runner` /
`session_manager` are exercised **manually** against live Indeed (below).

---

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # one-time browser download

cp .env.example .env                 # optional; defaults are fine
cp profile.example.json profile.json # then edit with YOUR real data
```

`profile.json`, `.secrets/`, `data/`, `runs/` are git-ignored.

### Configuration (all optional)

| env var                   | default                 | meaning |
|---------------------------|-------------------------|---------|
| `INDEED_SESSION_KEY`      | —                       | raw urlsafe-base64 Fernet key (highest priority) |
| `INDEED_SESSION_KEY_FILE` | `.secrets/session.key`  | file holding that key |
| `INDEED_SESSION_ENC`      | `.secrets/session.enc`  | encrypted `storage_state` blob |
| `INDEED_DB_PATH`          | `data/applications.db`  | SQLite file |
| `INDEED_PROFILE_PATH`     | `profile.json`          | candidate profile |
| `INDEED_RUNS_DIR`         | `runs/`                 | screenshots + DOM on every stop |

Key resolution order: `INDEED_SESSION_KEY` → `INDEED_SESSION_KEY_FILE` →
`.secrets/session.key`.

---

## Usage

```bash
python -m indeed_apply keygen                 # create .secrets/session.key
python -m indeed_apply login                  # headed browser: log in manually;
                                              #   session is captured + encrypted
python -m indeed_apply session-status [--check]   # present? age? (--check probes Indeed)

python -m indeed_apply select-jobs [--query .. --location .. --limit 5]
python -m indeed_apply apply-all [--confirm] [--headless]
python -m indeed_apply apply  <job_id> [--confirm] [--headless]
python -m indeed_apply resume <job_id> [--confirm] [--headless]
python -m indeed_apply status [--id N | --all]
```

* `login` and `capture-session` are the **same command** — capture happens while
  the authenticated browser is still open. Log in fully (email + emailed
  passcode + dismiss the passkey prompt); the command detects the logged-in
  signal, stores the encrypted session, and closes the browser.
* Default is **headed** and **no submit**. Nothing is sent to Indeed unless you
  pass `--confirm`; without it the runner stops at the review screen with
  `MANUAL_ACTION_REQUIRED` ("awaiting human submit at review screen").
* `select-jobs` persists each match as a `PENDING` row; `apply-all` then works
  through every `PENDING` row.

### Typical run

```bash
python -m indeed_apply keygen
python -m indeed_apply login                       # do the manual login
python -m indeed_apply session-status --check      # confirm "validity: OK"
python -m indeed_apply select-jobs --limit 4
python -m indeed_apply status                      # note the [id]s
python -m indeed_apply apply 1                     # stops at review (no --confirm)
python -m indeed_apply apply 1 --confirm           # actually submit
```

---

## Session capture & validity

`session_manager.capture()` launches a **headed** Chromium, waits for you to
finish logging in (account-menu DOM marker, or the `CTK` + `SHOE`/`PPID`/`SURF`
auth cookies), then serialises `context.storage_state()` (cookies + localStorage
+ origins), Fernet-encrypts the JSON, and writes `.secrets/session.enc` with
mode `600`. `restore()` decrypts it to an **in-memory dict** for
`browser.new_context(storage_state=...)` — plaintext never touches disk.

`check_validity(context)` is the concrete "still logged in vs expired" probe run
before any real work (full spec in `CLAUDE.md` §5.1):

1. `goto("https://www.indeed.com/")`.
2. **URL** — landed on `secure.indeed.com/auth`, `*/account/login`, `/auth?` ⇒
   expired. Landed on `/blocked` / a CAPTCHA / "additional verification
   required" ⇒ **`ManualActionRequired`** (needs a human in a headed browser,
   not just a fresh capture).
3. **Cookies** — needs live (`expires` in the future) `CTK` **and** at least one
   of `SHOE` / `PPID` / `SURF`.
4. **DOM (authoritative)** — account-menu marker present ⇒ logged in; a "Sign
   in" link present and no account marker ⇒ stale session. DOM beats cookies.
5. **Corroborate** — `goto("https://myjobs.indeed.com/applied")`; a redirect
   back to `secure.indeed.com/auth` confirms expired.

Returns `(True, "ok")` only when the logged-in DOM marker is found. Any
non-challenge `(False, reason)` ⇒ re-run `login`. The selectors and cookie names
are Indeed-version-specific and live in `session_manager.py` constants — update
them there if Indeed changes its markup.

---

## Screening questions — conservative by default (§5.4)

`apply_runner` auto-answers a screening question **only** when:

* the question label is an **exact normalized match** (lowercase, trimmed,
  punctuation-stripped) for a key in `profile.answers` — or one of a tiny,
  curated alias set for the standard work-authorization / sponsorship yes/no
  questions — **not** a fuzzy or substring guess; **and**
* the control is a closed type filled unambiguously: **yes/no**, **single-select
  radio**, **dropdown**, or **numeric**, where the profile value maps onto
  exactly one available option.

Everything else — free text / textarea, multi-select, an unmapped required
question, an ambiguous or partial label match, a value that doesn't land on
exactly one option, or an unrecognised control — transitions to
`MANUAL_ACTION_REQUIRED` with the question text as the reason. **When in doubt,
it pauses.** The matcher is `apply_runner.resolve_answer` and is unit-tested in
`tests/test_apply_matching.py`.

---

## Manual verification & failure handling

* **Detected challenge** (CAPTCHA / OTP / email code / passkey / "verify it's
  you" / bot wall) at any step → `MANUAL_ACTION_REQUIRED`, a specific
  `manual_reason`, screenshot + DOM dumped to `runs/<job_id>/`, browser closed,
  clean non-zero exit. The CLI prints what to do.
* **Session expired** on restore → `MANUAL_ACTION_REQUIRED` with reason
  "session expired — re-run login + capture-session". Do that, then `resume`.
* **Human handles the step** themselves (their own browser, or re-run `login`),
  then `python -m indeed_apply resume <job_id>`. `resume` is **idempotent** —
  if it stops again it just records `MANUAL_ACTION_REQUIRED` again.
* **Genuine error** (missing selector, timeout, upload failure) → `FAILED` with
  the exception summary. Terminal. No automatic retries; a human decides whether
  to recreate the row.

State machine (see `CLAUDE.md` §5.5 for the table). `SUBMITTED` and `FAILED` are
terminal. `PENDING -> FAILED` covers a posting that 404s or whose `job_key` no
longer resolves before any work starts.

---

## Manual test procedure (job_selector + apply_runner)

These touch live Indeed and are **not** in the automated suite.

1. `python -m indeed_apply login` and complete the real login.
2. `python -m indeed_apply session-status --check` → expect `validity: OK`.
3. `python -m indeed_apply select-jobs --limit 3` → expect 1-3 `PENDING` rows
   printed with `[id]`s; external-ATS postings are logged as `[skip]` and not
   queued.
4. `python -m indeed_apply status` → confirm the rows.
5. `python -m indeed_apply apply <id>` (no `--confirm`) → the browser walks the
   wizard and stops at the review screen; `status --id <id>` shows
   `MANUAL_ACTION_REQUIRED` = "awaiting human submit at review screen"; a
   screenshot exists under `runs/<id>/`.
6. Eyeball the review screen, then `python -m indeed_apply apply <id> --confirm`
   → `SUBMITTED`, and `status --id <id>` shows the full transition history.
7. To exercise the manual path: run against a posting with a free-text screening
   question → expect `MANUAL_ACTION_REQUIRED` naming that question, then
   `resume <id> --confirm` after answering it yourself.

---

## Running the tests

```bash
pytest -q
```

All tests run **without touching Indeed** (temp SQLite, the committed
`profile.example.json`, in-process crypto).

---

## Extending to multiple users (sketch, not built)

Add a `users` table (`id`, `label`, `email`); give every `applications` row a
`user_id` FK. `profile` becomes `profiles/<user_id>.json`. `session_manager`
stores one encrypted blob per user (`.secrets/session_<user_id>.enc`) with a
per-user key (or a master key + per-user derived key). The CLI gains `--user
<id>`; a worker iterates users, each run fully isolated (own context, own
session, own rows). `MANUAL_ACTION_REQUIRED` becomes a per-user notification
with a resume link. One browser context per user, never shared. SQLite is fine
at this size; swap for Postgres if it grows. See `CLAUDE.md` §8.
