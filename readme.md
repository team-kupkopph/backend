# Kupkop PH — Backend API

The Django/DRF backend for **Kupkop PH**, a mobile app for Filipino fur parents (animal welfare) —
stray rescue (Sagip), adoption, donations (Abot-tulong), volunteering (Kawang-Gawa), and community.
Launching Metro Manila first.

This service implements **Sprint 1 — Identity & Onboarding** for all three identities: **Journey A
(Pet Owner)**, **Journey B (Community rescue · tier 1)**, and **Journey C (Registered NGO · tier 2)**.
It is the account spine the rest of the product is built on, and the API the Expo app in
`../mobile_app/` talks to.

> **Status:** pre-launch. All three onboarding journeys are built and tested (**81 tests**, 0 warnings
> at last run); Journey A has been exercised end-to-end from the mobile app on a simulator. The
> **reviewer/admin** side (approve/reject/needs-info) is **Sprint 2** (`../dev/sprint-2-stories.md`).
> Several pieces are intentionally **stubbed for local dev** — see [Deferred & stubbed](#deferred--stubbed).

---

## What's implemented

| Area | Endpoints / behaviour |
|---|---|
| **Email signup + verify** | `POST /auth/signup` → email OTP → `POST /auth/email/verify` (issues JWT). No tokens before verify. |
| **Sign in / sessions** | `POST /auth/login`, `POST /auth/refresh` (revocation-aware), `POST /auth/logout`, `POST /auth/logout-all` |
| **Password recovery** | `POST /auth/password/forgot` (generic), `POST /auth/password/reset` (revokes all sessions) |
| **Social login** | `POST /auth/social/{provider}` behind a **mockable** token verifier (endpoint only; native flow deferred → `503 social_not_configured`) |
| **Profile** | `GET /me`, `PATCH /me`, `GET|PATCH /me/settings`, `PUT /me/location` (city only, no coordinate), `POST /me/phone` (+ `/verify`, SMS OTP) |
| **Verified Member** | `POST /media/presign` (dev stub) + `POST /verifications` (consent-persisted, creates a *pending* capability) |
| **Shelter (B/C)** | `POST/PATCH /shelter/profile`, `GET /shelter/dashboard` (derived gates), and `POST /verifications type=shelter_org` with **server-side, tier-derived** doc validation (`tier1→tier2` order, min-3 photos, `bai_pending`) |
| **Guest browse** | `GET /listings` (public; verified-poster predicate: shelter **OR** Verified Member) + `GET /reports/map` (public; shape-only until Sagip) + the `401 auth_required` wall on gated writes |

---

## Tech stack

- **Python 3.12** ⚠️ (do **not** use 3.14 — see [Gotchas](#gotchas--conventions))
- **Django 5.1** · **Django REST Framework 3.15** · **djangorestframework-simplejwt 5.3**
- **PostgreSQL 16** (via `psycopg` 3), with the `citext` and `pgcrypto` extensions
- **argon2-cffi** password hashing
- **pytest** + **pytest-django** + **factory-boy** for tests

Full pins are in [`requirements.txt`](./requirements.txt).

---

## Prerequisites

- **Python 3.12** available (e.g. `brew install python@3.12`). The default interpreter on this machine is
  3.14, which is too new for the pinned Django/psycopg wheels.
- **PostgreSQL 16** running locally (`brew install postgresql@16 && brew services start postgresql@16`).
  Verify with `pg_isready`.

---

## Setup (from scratch)

> The `.venv` is git-ignored and is **not** part of the repo — recreate it locally with the steps below.

```bash
cd backend

# 1. Databases + required extensions (dev + test)
createdb kupkop_dev  2>/dev/null || true
createdb kupkop_test 2>/dev/null || true
psql kupkop_dev  -c 'CREATE EXTENSION IF NOT EXISTS citext; CREATE EXTENSION IF NOT EXISTS pgcrypto;'
psql kupkop_test -c 'CREATE EXTENSION IF NOT EXISTS citext; CREATE EXTENSION IF NOT EXISTS pgcrypto;'

# 2. Virtualenv on Python 3.12 + dependencies
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# 3. Environment (see "Configuration" for the vars)
cp .env.example .env   # if .env.example is missing, create .env from the Configuration table

# 4. Migrate + seed a little demo data
.venv/bin/python manage.py migrate
.venv/bin/python seed.py     # a verified "rescuer" + a few available listings, for the guest feed
```

## Run

```bash
.venv/bin/python manage.py runserver 0.0.0.0:8000
```

The API is then at **`http://localhost:8000/api/v1/`**. Quick smoke test:

```bash
curl -s http://localhost:8000/api/v1/health            # {"status":"ok"}
curl -s "http://localhost:8000/api/v1/listings?city=Marikina"
```

> **Reading OTP codes in dev.** There is no email/SMS provider wired. When `DEBUG=1`, the console
> "sender" prints the raw 6-digit code to the server log (e.g. `[DEV OTP] email you@example.com: 123456`).
> That is how you complete a signup/verify locally. With `DEBUG=0` the code is never printed.

## Test

```bash
.venv/bin/pytest -q          # 81 tests; uses the kupkop_test database
.venv/bin/pytest accounts/tests/test_login.py -v   # a single file
```

Tests run against `kupkop_test` (created above). The suite is expected to be **green with zero warnings** —
warnings are treated as failures during review.

---

## Project layout

```
backend/
├── config/               # project: settings, urls, wsgi/asgi, health check
├── common/               # cross-cutting helpers
│   ├── errors.py         #   DRF exception handler → the { "error": {...} } envelope
│   ├── otp.py            #   issue_code / verify_code (hashed, 5-min, 5-attempt lock)
│   ├── senders.py        #   Sender interface + ConsoleSender (dev); swap in a real provider here
│   └── throttles.py      #   per-minute + per-hour OTP-resend throttles
├── accounts/             # identity: Account, AccountSettings, AccountIdentity, Address
│   ├── models.py         #   the account spine (email identity, sessions_revoked_at, …)
│   ├── authentication.py #   AccountJWTAuthentication (resolves request.user + enforces revocation)
│   ├── tokens.py         #   tokens_for(account) → SimpleJWT pair with an account_id claim
│   ├── social.py         #   verify_token() seam (raises SocialNotConfigured → 503; tests monkeypatch it)
│   ├── serializers.py / views.py / urls.py
│   └── tests/            #   signup, email verify, login, password reset, social, /me, location, guest
├── verifications/        # Verified Member: VerificationRequest/Document, AccountCapability, VerificationCode
├── shelter/              # ShelterProfile (B/C): org setup, contact, tier, dashboard gates
├── listings/             # AdoptionListing + public /listings feed + /reports/map (+ visibility.py)
├── conftest.py           # autouse cache.clear() so throttle state doesn't leak between tests
├── seed.py               # idempotent demo data
├── manage.py · pytest.ini · requirements.txt
```

Apps are registered in `config/settings.py → INSTALLED_APPS`; all routes are mounted under `/api/v1/`
in `config/urls.py`.

---

## API reference

Base path **`/api/v1`**. JSON in/out. Auth via `Authorization: Bearer <access-jwt>`.
Errors use a single envelope: `{ "error": { "code": "snake_case", "message": "...", "field"?, "details"? } }`.

### Auth & identity

| Method · Path | Auth | Purpose / notes |
|---|---|---|
| `POST /auth/signup` | Public | `{account_type, display_name, email, password}` → `201 {account_id, email, next:"verify_email"}`. **`account_type` restricted to `personal`/`shelter`** (never `admin`). Issues an email OTP. `409 email_taken` on a duplicate. |
| `POST /auth/email/verify` | Public | `{email, code}` → `200 {access, refresh, account}`, sets `email_verified_at`. `400 code_invalid {attempts_left}` · `410 code_expired` · `423 code_locked`. |
| `POST /auth/email/resend` | Public · throttled | `{email}` → `202 {}` (generic). Throttled 1/min + 5/hour. |
| `POST /auth/login` | Public | `{email, password}` → `200 {access, refresh, account}`. Wrong password **and** unknown email both return the **same** generic `401 invalid_credentials`. Correct password on an unverified account → `403 email_unverified` (and re-sends the code). |
| `POST /auth/refresh` | Refresh token | `{refresh}` → `200 {access}`. Rejects refresh tokens issued before a `logout-all`/password reset. |
| `POST /auth/logout` | User | `{refresh}` → `204` (blacklists that one refresh). |
| `POST /auth/logout-all` | User | `204`. Sets `sessions_revoked_at = now()` → invalidates every token issued before now. |
| `POST /auth/password/forgot` | Public | `{email}` → `200 {}` **always** (never reveals whether the account exists). Issues a reset OTP if it does. |
| `POST /auth/password/reset` | Public | `{email, code, new_password}` → `200 {}`. Sets the password **and** revokes all existing sessions. |
| `POST /auth/social/{provider}` | Public | `{id_token, account_type?}` → `200 {access, refresh, is_new, account}`. Links by provider-sub, else by email, else creates (email pre-verified). **Endpoint only** — real provider verification is deferred (the `verify_token` seam raises `SocialNotConfigured` → clean `503 social_not_configured`; tests monkeypatch it). Blocked on the Apple/Google credentials (sprint-0 S0-05/S0-06), not on code. |

### Me

| Method · Path | Auth | Purpose |
|---|---|---|
| `GET /me` | User | Profile: `account_type`, `display_name`, `email`, `email_verified_at`, `capabilities[]`, `shelter` (`{tier, verification_status}` for shelter accounts, else null), `settings{}`. |
| `PATCH /me` | User | `{display_name?, photo_file_url?}` (validated). |
| `GET · PATCH /me/settings` | User | The four booleans: `marketing_emails`, `approximate_location`, `masked_contact`, `push_enabled`. |
| `PUT /me/location` | User | `{city, barangay?}` → stores **city only**, no coordinate (decision 11). |

### Verified Member

| Method · Path | Auth | Purpose |
|---|---|---|
| `POST /media/presign` | User | **Dev stub** — returns a placeholder `{upload_url, fields, file_url}` (no real S3). |
| `POST /verifications` | User | `{type:"rescuer", social_proof_url, consent_version, documents:[{doc_type:"gov_id", file_url}]}` → `201 {verification_id, status:"pending"}`. Persists **consent** (`consent_at`/`consent_version`) and creates a **pending** `account_capability`. Also accepts `type="shelter_org"` with tier-derived doc validation (`409 tier1_incomplete`, `422 missing_docs`). `422 consent_missing` without consent. Approval is Sprint 2. |

### Public / guest

| Method · Path | Auth | Purpose |
|---|---|---|
| `GET /listings` | Public | `?city=&species=&page=` — only listings whose **poster is verified** — a verified shelter **OR** a Verified Member (approved `rescuer` capability) — and `listing_status='available'`. Helper: `listings/visibility.py`. |
| `GET /health` | Public | `{"status":"ok"}`. |
| *(any gated write, no token)* | — | `401 { error:{ code:"auth_required" \| "not_authenticated" } }` — the mobile app turns this into a signup wall. |

---

## Data model

Django ORM models are the source of truth for these tables; `../kupkop_mvp_schema.sql` is the broader
cross-project schema reference. Ten tables live here (all UUID PKs):

| Table | App | Notes |
|---|---|---|
| `account` | accounts | **Email is the identifier** (`email` citext, unique). `password_hash` nullable (social-only). `phone` nullable. `sessions_revoked_at` powers logout-all/reset revocation. **No `date_of_birth`** (RA 10173 minimization). |
| `account_settings` | accounts | 1:1 with account; created atomically at signup. |
| `account_identity` | accounts | social links; unique `(provider, provider_user_id)` and `(account, provider)`. |
| `address` | accounts | person addresses store **city/barangay only**; `geom` is a nullable placeholder (no PostGIS this slice). |
| `verification_code` | verifications | hashed OTP store (`code_hash`, `attempts`, `max_attempts`, `expires_at`). |
| `verification_request` | verifications | a submission (`type`, `status`, `social_proof_url`, `consent_at`, `consent_version`). |
| `verification_document` | verifications | uploaded proofs (`doc_type`, `file_url`, per-doc `status`). |
| `account_capability` | verifications | e.g. `rescuer` `pending\|approved\|rejected`; unique `(account, capability)`. |
| `shelter_profile` | shelter | 1:1 with account (B/C): `org_name`, `org_type`, **`tier`** (drives the doc set + badge), contact, vet/PRC. Verified is **derived**, not stored here. |
| `adoption_listing` | listings | minimal (seed/demo scope); the public feed reads this. |

**"Verified" is always derived**, never a stored boolean — e.g. a Verified Member = *exists*
`account_capability(capability='rescuer', status='approved')`.

---

## Security model (Journey A)

- **Email identity**, and **no JWT is issued before the email is verified** (only `/auth/email/verify`
  and `/auth/login` mint tokens).
- **Enumeration asymmetry (deliberate):** `signup` **may** reveal a taken email (`409`); `login`,
  `password/forgot`, and email-verify stay **generic**. Do not "fix" them to be symmetric.
- **OTP:** 6-digit, stored **hashed** (never raw, never logged in prod), 5-minute TTL, 5-attempt lock,
  resend throttled (1/min + 5/hour).
- **Session revocation** uses `account.sessions_revoked_at`: the JWT auth layer *and* the refresh
  endpoint reject any token whose `iat` predates it. (SimpleJWT's blacklist is bound to Django's user
  model, which our custom `Account` is not — so `logout-all`/reset use the timestamp instead.)
- **Privileged account types are not self-assignable** — public signup/social accept only
  `personal`/`shelter`; `admin` is server-only.
- **Consent is persisted** on verification submission, never inferred from a document existing.
- Passwords hashed with **argon2**.

---

## Configuration

Environment variables (loaded from `backend/.env` via `python-dotenv`). Create `.env` if it's missing:

| Var | Default | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | a dev-only placeholder | Django/JWT signing key. **Set a real one in any shared/prod env.** |
| `DJANGO_DEBUG` | `1` | `1` = debug on (and OTP codes printed to the log). Set `0` outside local dev. |
| `DATABASE_URL` | `postgres://localhost/kupkop_dev` | Only the **database name** is read from this; the host is always `localhost` in this slice (documented in `settings.py`). |
| `TEST_DATABASE_NAME` | `kupkop_test` | Test database name. |

Key tunables in `config/settings.py`: JWT lifetimes (`ACCESS` 15 min, `REFRESH` 30 days, rotated +
blacklisted), `OTP_TTL_MINUTES` (5), `OTP_MAX_ATTEMPTS` (5), and the OTP-resend throttle rates.

Example `.env`:

```
DJANGO_SECRET_KEY=dev-only-change-me-0123456789-0123456789
DJANGO_DEBUG=1
DATABASE_URL=postgres://localhost/kupkop_dev
TEST_DATABASE_NAME=kupkop_test
```

---

## Icon-cruft hook (opt-in, one line)

Google Drive's Mirror sync (`team.kupkopph@gmail.com`) periodically re-creates zero-byte
macOS Finder `Icon\r` files inside `.git/`, and git then fails `fetch`/`pull` with
`bad object refs/Icon?`. The repo carries `.githooks/purge-icon-cruft` and a `pre-commit`
that calls it — the `-size 0` guard makes it safe (no real ref, object or reflog is ever
zero-byte). Enable it in your clone with:

```bash
git config core.hooksPath .githooks
```

Silent on the happy path; prints one line when it actually purges something so the
recurrence rate stays observable. See `../dev/HANDOFF.md` for the underlying issue.

---

## Gotchas & conventions

- **Python 3.12, not 3.14.** The pins don't build cleanly on 3.14; always create the venv with 3.12.
- **Google-Drive sync corrupts `.git` (and can wipe the git-ignored `.venv`).** This repo lives in a
  Drive-synced folder; the sync recreates macOS `Icon\r` files inside `.git/` and has been observed to
  clobber history and remove the venv. Recover with the [Setup](#setup-from-scratch) steps; a lasting
  fix is to move the repo out of the synced folder. Clean stray icons with
  `find .git -name 'Icon*' -delete`.
- **Migrations are the DB source of truth** for this service; run `migrate` after pulling.
- **Test output must be pristine** — no warnings. `conftest.py` clears the cache between tests so
  throttle state doesn't leak.

---

## Scheduled tasks

Four management commands must run on a schedule in production. A ready-to-install crontab is at [`deploy/cron.d/kupkop`](./deploy/cron.d/kupkop).

| Command | Frequency | Purpose |
|---|---|---|
| `run_sweeps` | Every hour | Stray escalation, stalled claim expiry, offer expiry, shift reminders, badges (US-F0/E1/E2/N2/V7/B1) |
| `run_matching_sweep` | Nightly 18:20 UTC · 02:20 PHT | §11.4's lost↔found **safety net** — re-score still-open reports so a near miss gets another look (US-L2) |
| `purge_expired_documents` | Nightly 18:50 UTC · 02:50 PHT | RA 10173 data minimization — null `file_url` 90 days after a terminal verification decision (US-SEC4) |
| `purge_deleted_accounts` | Nightly 19:20 UTC · 03:20 PHT | RA 10173 erasure — anonymize soft-deleted accounts in place once the 30-day grace window closes (US-N2, §12.7) |

Quick-start (development):

```bash
.venv/bin/python manage.py run_sweeps
.venv/bin/python manage.py run_matching_sweep
.venv/bin/python manage.py purge_expired_documents
.venv/bin/python manage.py purge_deleted_accounts
```

The sweep framework uses plain cron (decision US-F0: no Celery-beat for MVP). Every command is idempotent — safe to run more often than scheduled during testing.

⚠️ **All four refuse to run twice at once** (US-Q2 follow-up). Each subclasses `SingletonCommand` (`common/management_base.py`) and takes a Postgres advisory lock named after itself; if the previous run is still going, the next logs a skip and exits 0. It is a database lock rather than `flock` because §16.1 runs 1–2 Fargate tasks, and a file lock guards one host while *looking* in the crontab exactly as though it guards both.

⚠️ `run_matching_sweep` is **§11.4's safety net, not the matcher.** A lost/found report is scanned synchronously when it is filed, so nobody's reunion waits on this job — which is why it can be nightly. It was inside `run_sweeps` (hourly, 24× what §11.4 asks) until US-Q2 measured it at **11.5 minutes over 50,000 reports**: 11.5 minutes of database load every hour, competing with the reads §13.1 budgets. A sweep belongs in `run_sweeps` only if a one-hour delay would hurt someone.

⚠️ **Nightly slots are stated in PHT as well as UTC, and asserted in local time.** The crontab is UTC and the userbase is UTC+8, and for three sprints both `purge_*` entries were commented *"low-traffic window"* while running at 10:00 and 10:30 PHT — mid-morning. The arithmetic in those comments was right and the conclusion was not, which is not something a comment can catch. `common/tests/test_crontab.py` converts each nightly hour to PHT and requires 01:00–04:59 (and validates the fields are legal cron, after an ordered string-replace once produced a minute of `350` — cron rejects the whole file for that, silently disabling every job in it).

⚠️ **Re-timing never shortens a retention promise.** Both purges select `<= now - N days`, so a later slot means data is held slightly *longer* than its 90-day or 30-day window, never a minute less. That property is what makes the schedule a performance decision rather than a privacy one — and it is why the grace window a user was promised (and whatever the privacy policy states) cannot be changed by moving a cron line.

⚠️ The two `purge_*` commands are **irreversible** and deliberately live outside `run_sweeps`: retention deletion should be schedulable, and auditable, independently of the routine hourly sweeps.

---

## Deferred & stubbed

These are intentional for the Sprint-1 slice — implemented as clean stubs or seams, not gaps:

- **Native social login + real token verification** — `accounts/social.verify_token` is a seam that
  raises `SocialNotConfigured` (→ `503`) until a real Google/Apple verifier is wired there. **Blocked
  on the developer-program credentials (sprint-0 S0-05/S0-06), not on code** — the mobile side has a
  matching seam. ⚠️ Apple sign-in is an App Store 4.8 requirement. (Facebook is Phase 2 — it can omit
  email, which the endpoint rejects with `400 email_required`.)
- **Real email delivery** — the code seam is done (`common.senders.SesEmailSender`, Amazon SES via
  boto3). Set `EMAIL_PROVIDER=ses`, `EMAIL_FROM=<verified identity>`, `AWS_SES_REGION=<region>` in
  the deployed env; unset in dev keeps the `ConsoleSender` and its `[DEV OTP]` stdout print. Owner
  actions still needed: open the AWS account, verify a sending identity (an address for a smoke
  test, then the sending domain with DKIM), and request production access — SES starts in a
  *sandbox* that only mails verified recipients, so every real signup fails until that ticket is
  approved. §16.6 gate 3.
- **Real SMS delivery** — still `ConsoleSender`; waits on the Semaphore/Movider account named in
  §16.6 gate 3. `SesEmailSender` deliberately delegates `channel="sms"` to the fallback so a mixed
  configuration doesn't ship SMS OTPs through email.
- **Object storage** — `POST /media/presign` returns a placeholder; wire access-restricted S3.
- **PostGIS** — `address.geom` is modeled as nullable text; reconcile to a real `geography(Point)` in
  Sprint 2, when `stray_report` (Sagip) needs real proximity queries.
- **`GET /me` city** — the profile doesn't yet return the user's city (the mobile app caches it
  locally); adding it to `me_repr` is a small follow-up that lets the client drop the cache.
- **Reviewer/admin approval flow** (approve/reject/needs-info) — **Sprint 2** (`../dev/sprint-2-stories.md`).
  Journeys B/C themselves are **built** (Sprint 1); only the decision side that flips them to `approved`
  is deferred.

---

## Related

- **Mobile client:** `../mobile_app/` (Expo / React Native), which consumes this API. Its plan is in
  `../docs/superpowers/plans/2026-08-03-sprint1-journey-a-mobile.md`.
- **Design spec & backend plan:** `../docs/superpowers/specs/` and `../docs/superpowers/plans/`.
- **Canonical project state:** `../dev/HANDOFF.md`.
