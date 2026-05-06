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

# For --email mode
IMAP_USER=...
IMAP_PASS=...
IMAP_HOST=imap.gmail.com               # optional default
IMAP_PORT=993                          # optional default
YESCAPA_SENDERS=no-reply@yescapa.com,...  # optional, comma-separated
BOOKING_KEYWORDS=nova reserva,...         # optional, comma-separated
```

`GOOGLE_CREDENTIALS_JSON` must be the full contents of a Google Cloud service account JSON key. The Google Sheet must be shared with the service account email.

## Architecture

**`yescapa_sheets.py`** — core logic, two main classes:

- `YescapaPlaywright.run()`: launches Chromium, logs in, then iterates `FETCH_STATES` (all booking meta-states/sub-states). Registers a response interceptor (`_on_api_response`) that captures booking list payloads as the SPA fetches them. Paginates by clicking the "next page" button. After collecting all summaries, calls `_fetch_detail()` per booking using `page.evaluate(fetch(...))` — this executes inside the authenticated browser context so no token handling is needed externally.
- `SheetsClient`: wraps gspread, creates spreadsheet/worksheet if missing, clears and rewrites all rows on each sync.

**`email_checker.py`** — IMAP client that checks for unread Yescapa emails. `has_new_booking_email()` searches by sender then validates the subject against keyword lists. `mark_booking_emails_read()` marks matched emails as `\Seen` after a successful sync.

**`run.py`** — entry point that dispatches between `--email` and `--scheduled` modes. Used by Railway cron jobs.

The API interception approach (`_on_api_response`) is intentional: Yescapa's SPA fetches booking lists automatically when navigating to `/d/bookings?meta_state=...`, so intercepting those responses avoids needing to reverse-engineer auth tokens. Detail fetches (`/v4/bookings-owner/{id}/`) use `page.evaluate()` with `credentials: 'include'` for the same reason.

## Deployment

Railway builds via Dockerfile (uses `mcr.microsoft.com/playwright/python` base image which includes Chromium). The three cron services in `railway.toml` all use `restartPolicyType = "never"` — they run once and exit. Set all env vars as Railway environment variables (not committed to the repo).
