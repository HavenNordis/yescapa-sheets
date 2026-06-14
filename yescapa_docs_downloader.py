"""
yescapa_docs_downloader.py — Descarrega PDFs Yescapa (Contrato, Seguro, Fatura)
para pasta Drive partilhada com a Service Account.

Autenticação Yescapa:
  - Tradicional: YESCAPA_EMAIL + YESCAPA_PASSWORD (login Playwright)
  - Bypass: YESCAPA_AUTH_TOKEN + YESCAPA_X_API_KEY (skip login, headers injectados)
"""
import io
import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from playwright.sync_api import sync_playwright

load_dotenv()

YESCAPA_EMAIL      = os.environ.get("YESCAPA_EMAIL", "")
YESCAPA_PASSWORD   = os.environ.get("YESCAPA_PASSWORD", "")
YESCAPA_AUTH_TOKEN = os.environ.get("YESCAPA_AUTH_TOKEN", "")
YESCAPA_X_API_KEY  = os.environ.get("YESCAPA_X_API_KEY", "")
GOOGLE_SHEET_NAME  = os.getenv("GOOGLE_SHEET_NAME", "Reservas Yescapa")
WORKSHEET_NAME     = os.getenv("WORKSHEET_NAME", "Reservas")
DRIVE_DOCS_FOLDER_ID = os.getenv("DRIVE_DOCS_FOLDER_ID", "")
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

YESCAPA_BASE = "https://www.yescapa.pt"

DOCS = [
    ("Contrato URL", "Contrato.pdf"),
    ("Seguro URL",   "Seguro.pdf"),
    ("Fatura URL",   "Fatura.pdf"),
]


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] [docs-downloader] {msg}", flush=True)


def get_google_clients():
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON") or ""
    if not raw:
        raise SystemExit("GOOGLE_CREDENTIALS_JSON não definida.")
    info = json.loads(raw)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return (
        gspread.authorize(creds),
        build("drive", "v3", credentials=creds, cache_discovery=False),
    )


def _escape(name):
    return name.replace("\\", "\\\\").replace("'", "\\'")


def find_or_create_folder(drive_service, name, parent_id):
    query = (
        f"name = '{_escape(name)}' "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents and trashed = false"
    )
    result = drive_service.files().list(
        q=query, spaces="drive", fields="files(id, name)",
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    files = result.get("files", [])
    if files:
        return files[0]["id"]
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = drive_service.files().create(
        body=metadata, fields="id", supportsAllDrives=True,
    ).execute()
    log(f"  pasta criada: '{name}'")
    return folder["id"]


def find_file(drive_service, name, folder_id):
    query = (
        f"name = '{_escape(name)}' "
        f"and '{folder_id}' in parents and trashed = false"
    )
    result = drive_service.files().list(
        q=query, spaces="drive", fields="files(id, name)",
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    files = result.get("files", [])
    return files[0]["id"] if files else None


def upload_pdf(drive_service, pdf_bytes, name, folder_id):
    metadata = {"name": name, "parents": [folder_id]}
    media = MediaIoBaseUpload(
        io.BytesIO(pdf_bytes), mimetype="application/pdf", resumable=False,
    )
    file = drive_service.files().create(
        body=metadata, media_body=media, fields="id", supportsAllDrives=True,
    ).execute()
    return file["id"]


_SANITIZE_RE = re.compile(r"[^\w\s\-#.,]+", re.UNICODE)


def sanitize_folder_name(name):
    cleaned = _SANITIZE_RE.sub("", name).strip()
    return cleaned[:120]


def yescapa_login(page):
    page.goto(
        f"{YESCAPA_BASE}/login/email?next=/",
        wait_until="domcontentloaded", timeout=30_000,
    )
    page.wait_for_timeout(1500)
    for sel in [
        "#axeptio_btn_acceptAll", "#onetrust-accept-btn-handler",
        "button:has-text('Aceitar tudo')", "button:has-text('Aceitar')",
        "button:has-text('Accept all')",
    ]:
        try:
            page.click(sel, timeout=3000); break
        except Exception:
            pass
    for sel in ["input[type='email']", "input[name='email']", "#id_email", "#email"]:
        try:
            page.fill(sel, YESCAPA_EMAIL, timeout=5000); break
        except Exception:
            pass
    for sel in ["input[type='password']", "input[name='password']", "#id_password", "#password"]:
        try:
            page.fill(sel, YESCAPA_PASSWORD, timeout=5000); break
        except Exception:
            pass
    for sel in [
        "button:has-text('Conectar-se')",
        "button[type='submit']", "input[type='submit']",
        "button:has-text('Entrar')", "button:has-text('Se connecter')",
    ]:
        try:
            page.click(sel, timeout=5000); break
        except Exception:
            pass
    try:
        page.wait_for_function(
            "() => !window.location.pathname.includes('conexao') && "
            "!window.location.pathname.includes('login')",
            timeout=20_000,
        )
    except Exception:
        pass
    page.wait_for_timeout(2000)


def download_pdf(page, url, extra_headers=None):
    try:
        kwargs = {"timeout": 20_000}
        if extra_headers:
            kwargs["headers"] = extra_headers
        resp = page.request.get(url, **kwargs)
        if not resp.ok:
            log(f"    HTTP {resp.status}")
            return None
        body = resp.body()
        if body[:4] != b"%PDF":
            log(f"    não é PDF (primeiros 4 bytes: {body[:4]!r})")
            return None
        return body
    except Exception as e:
        log(f"    erro: {e}")
        return None


def process_bookings():
    if not DRIVE_DOCS_FOLDER_ID:
        log("DRIVE_DOCS_FOLDER_ID não definida — a saltar.")
        return {"baixados": 0, "ja_existiam": 0, "falhados": 0}

    bypass_login = bool(YESCAPA_AUTH_TOKEN and YESCAPA_X_API_KEY)
    if not bypass_login and (not YESCAPA_EMAIL or not YESCAPA_PASSWORD):
        log("Sem credenciais nem tokens — a saltar.")
        return {"baixados": 0, "ja_existiam": 0, "falhados": 0}

    sheets_client, drive_service = get_google_clients()

    log("A ler folha Reservas...")
    spreadsheet = sheets_client.open(GOOGLE_SHEET_NAME)
    ws = spreadsheet.worksheet(WORKSHEET_NAME)
    rows = ws.get_all_records()
    log(f"  → {len(rows)} reservas total")

    candidates = []
    for row in rows:
        if (row.get("Estado Meta") or "").strip().lower() != "confirmed":
            continue
        # Saltar reservas directas (ID não-numérico) — não têm PDFs no Yescapa
        rid = str(row.get("ID") or "").strip()
        if not rid or not rid.isdigit():
            continue
        has_url = any((row.get(col) or "").strip() for col, _ in DOCS)
        if not has_url:
            continue
        candidates.append(row)
    log(f"  → {len(candidates)} confirmed Yescapa com ≥1 URL")

    if not candidates:
        return {"baixados": 0, "ja_existiam": 0, "falhados": 0}

    year = str(datetime.now(timezone.utc).year)
    year_folder_id = find_or_create_folder(drive_service, year, DRIVE_DOCS_FOLDER_ID)

    baixados = 0
    ja_existiam = 0
    falhados = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, slow_mo=50)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="pt-PT",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        if bypass_login:
            log("Bypass login: YESCAPA_AUTH_TOKEN + YESCAPA_X_API_KEY.")
        else:
            log("A fazer login no Yescapa...")
            yescapa_login(page)

        for row in candidates:
            ref  = str(row.get("ID") or "").strip()
            nome = (
                (row.get("Hóspede Nome") or "").strip()
                + " "
                + (row.get("Hóspede Apelido") or "").strip()
            ).strip() or "Sem Nome"

            folder_name = sanitize_folder_name(f"#{ref} - {nome}")
            booking_folder_id = find_or_create_folder(
                drive_service, folder_name, year_folder_id,
            )

            for sheet_col, file_name in DOCS:
                url = (row.get(sheet_col) or "").strip()
                if not url:
                    continue
                existing = find_file(drive_service, file_name, booking_folder_id)
                if existing:
                    ja_existiam += 1
                    continue

                log(f"  #{ref} '{nome}' → {file_name}")
                extra_headers = None
                if bypass_login:
                    # Headers que imitam fielmente o Chrome 148 a partir de yescapa.pt
                    extra_headers = {
                        "Authorization": YESCAPA_AUTH_TOKEN,
                        "X-Api-Key": YESCAPA_X_API_KEY,
                        "Accept": "*/*",
                        "Accept-Encoding": "gzip, deflate, br, zstd",
                        "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
                        "Origin": YESCAPA_BASE,
                        "Referer": f"{YESCAPA_BASE}/d/bookings/{ref}",
                        "Sec-Ch-Ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
                        "Sec-Ch-Ua-Mobile": "?0",
                        "Sec-Ch-Ua-Platform": '"Windows"',
                        "Sec-Fetch-Dest": "empty",
                        "Sec-Fetch-Mode": "cors",
                        "Sec-Fetch-Site": "cross-site",
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/148.0.0.0 Safari/537.36"
                        ),
                    }
                pdf_bytes = download_pdf(page, url, extra_headers=extra_headers)
                if not pdf_bytes:
                    falhados += 1
                    continue
                upload_pdf(drive_service, pdf_bytes, file_name, booking_folder_id)
                baixados += 1

        browser.close()

    log(f"=== Fim: baixados={baixados} ja_existiam={ja_existiam} falhados={falhados} ===")
    return {"baixados": baixados, "ja_existiam": ja_existiam, "falhados": falhados}


def main():
    log("=== yescapa_docs_downloader ===")
    return process_bookings()


if __name__ == "__main__":
    main()
