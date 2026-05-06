# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Automates syncing Yescapa (campervan rental platform) booking data into Google Sheets. It uses Playwright to log into the Yescapa web app, intercepts the API responses from the SPA (`api.jelouemoncampingcar.com`), fetches full booking details per booking, and writes everything to a Google Sheet via the gspread library.

Deployed on Railway as three cron jobs (see `railway.toml`): email-triggered sync every 15 minutes, plus daily scheduled syncs at 08:00 and 20:00 UTC.

## Running locally

```bash
pip install -r requirements.txt
playwright install chromium

# Run full sync unconditionally
python run.py --scheduled

# Check email inbox and only sync if new Yescapa booking email found
python run.py --email

# Run the sync directly
python yescapa_sheets.py
```

Set `HEADLESS=false` (the default for local runs) to watch the browser. Set `HEADLESS=true` for headless operation (default in Docker/Railway).

## Required environment variables

Create a `.env` file locally (loaded via `python-dotenv`):

```
YESCAPA_EMAIL=...
YESCAPA_PASSWORD=...
GOOGLE_CREDENTIALS_JSON={"type":"service_account",...}   # full JSON, one line
GOOGLE_SHEET_NAME=Reservas Yescapa     # optional, this is the default
WORKSHEET_NAME=Reservas                # optional, this is the default
LOGSHEET_NAME=Log                      # optional, this is the default

# For --email mode
IMAP_USER=...
IMAP_PASS=...
IMAP_HOST=imap.gmail.com               # optional default
IMAP_PORT=993                          # optional default
YESCAPA_SENDERS=no-reply@yescapa.com,...  # optional, comma-separated
BOOKING_KEYWORDS=nova reserva,...         # optional, comma-separated
```

`GOOGLE_CREDENTIALS_JSON` must be the full contents of a Google Cloud service account JSON key. The Google Sheet must be shared with the service account email.

**Note on `IMAP_PASS`**: Gmail requires an App Password (16-char), not the regular account password. Generate one at Google Account → Security → 2-Step Verification → App passwords.

## Architecture

**`yescapa_sheets.py`** — core logic, two main classes:

- `YescapaPlaywright.run()`: launches Chromium, logs in, then iterates `FETCH_STATES` (valid meta-states: `confirmed`, `waiting`, `todo`, and `archived` with sub-states `TO_COME`, `CANCELLED_GUEST`, `CANCELLED_OWNER`, `CANCELLED_BOTH`). Two interceptors are registered on the page: `_on_api_response` captures booking list payloads; `_on_api_request` captures the SPA's request headers (including `Authorization` and `x-api-key`) on the first API call. Pagination tries a "next page" button click first; if not found, falls back to `page.request.get()` using the stored headers and a URL derived from the first-page URL (replacing `page=1` with `page=N`). After collecting all summaries, calls `_fetch_detail()` per booking via `page.request.get()` with the captured headers.
- `SheetsClient`: wraps gspread. `update_bookings()` clears and rewrites the bookings worksheet. `update_log()` maintains a "Log" worksheet with one row per trigger type (Agendamento / Email), updated in-place on each run.

**`email_checker.py`** — IMAP client that checks for unread Yescapa emails. `has_new_booking_email()` searches by sender then validates the subject against keyword lists. `mark_booking_emails_read()` marks matched emails as `\Seen` after a successful sync.

**`run.py`** — entry point that dispatches between `--email` and `--scheduled` modes, passing the trigger string to `main(trigger)`. Used by Railway cron jobs.

**Why `page.request.get()` instead of `page.evaluate(fetch(...))`**: the API at `api.jelouemoncampingcar.com` returns 403 for browser-context fetch calls due to CORS preflight restrictions on custom headers. `page.request.get()` bypasses CORS entirely while still sharing the browser session's cookies.

## Deployment

Railway builds via Dockerfile (uses `mcr.microsoft.com/playwright/python` base image which includes Chromium). The three cron services in `railway.toml` all use `restartPolicyType = "never"` — they run once and exit. Set all env vars as Railway environment variables (not committed to the repo).
