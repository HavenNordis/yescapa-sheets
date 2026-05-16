"""
yescapa_docs_downloader.py — Descarrega PDFs Yescapa (Contrato, Seguro, Fatura)
para uma pasta Google Drive partilhada com a Service Account.

Fluxo:
  1. Lê folha "Reservas" (preenchida pelo yescapa_sheets.py)
  2. Filtra confirmed com pelo menos 1 URL de documento preenchido
  3. Para cada (reserva, documento), verifica se PDF já existe em Drive
  4. Se não existe, baixa via Playwright (sessão autenticada) e sobe para Drive

Estrutura no Drive:
  Documentos Yescapa/        ← partilhada com SA, ID em DRIVE_DOCS_FOLDER_ID
    └─ 2026/
        └─ #3256591 - Bharpur Singh/
            ├─ Contrato.pdf
            ├─ Seguro.pdf
            └─ Fatura.pdf

Idempotência: o Drive é a fonte de verdade. Se o ficheiro já lá está, salta.
Tracking explícito em sheet não é necessário (e seria reset pelo sync).

Env vars necessárias:
  GOOGLE_CREDENTIALS_JSON      (mesma da SA usada por yescapa_sheets)
  YESCAPA_EMAIL, YESCAPA_PASSWORD
  GOOGLE_SHEET_NAME            (default: "Reservas Yescapa")
  WORKSHEET_NAME               (default: "Reservas")
  DRIVE_DOCS_FOLDER_ID         (ID da pasta raiz partilhada com SA)
  HEADLESS                     (default: "true")
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

YESCAPA_EMAIL    = os.environ.get("YESCAPA_EMAIL", "")
YESCAPA_PASSWORD = os.environ.get("YESCAPA_PASSWORD", "")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Reservas Yescapa")
WORKSHEET_NAME    = os.getenv("WORKSHEET_NAME", "Reservas")
DRIVE_DOCS_FOLDER_ID = os.getenv("DRIVE_DOCS_FOLDER_ID", "")
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

YESCAPA_BASE = "https://www.yescapa.pt"

# (sheet_column_name, file_name_in_drive)
DOCS = [
    ("Contrato URL", "Contrato.pdf"),
    ("Seguro URL",   "Seguro.pdf"),
    ("Fatura URL",   "Fatura.pdf"),
]


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] [docs-downloader] {msg}", flush=True)


# --- Google clients --------------------------------------------------------

def get_google_clients():
    """Devolve (sheets_client, drive_service) com scopes Sheets+Drive."""
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON") or ""
    if not raw:
        raise SystemExit("GOOGLE_CREDENTIALS_JSON não definida.")
    info = json.loads(raw)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    sheets_client = gspread.authorize(creds)
    drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return sheets_client, drive_service


# --- Drive helpers ---------------------------------------------------------

def _escape(name: str) -> str:
    return name.replace("\\", "\\\\").replace("'", "\\'")


def find_or_create_folder(drive_service, name: str, parent_id: str) -> str:
    """Encontra ou cria pasta `name` dentro de `parent_id`. Devolve folder_id."""
    query = (
        f"name = '{_escape(name)}' "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents "
        f"and trashed = false"
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
    log(f"  pasta criada: '{name}' (id={folder['id']})")
    return folder["id"]


def find_file(drive_service, name: str, folder_id: str) -> Optional[str]:
    """Devolve file_id se já existir, None caso contrário."""
    query = (
        f"name = '{_escape(name)}' "
        f"and '{folder_id}' in parents "
        f"and trashed = false"
    )
    result = drive_service.files().list(
        q=query, spaces="drive", fields="files(id, name)",
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    files = result.get("files", [])
    return files[0]["id"] if files else None


def upload_pdf(drive_service, pdf_bytes: bytes, name: str, folder_id: str) -> str:
    """Upload PDF bytes para Drive folder. Devolve file_id."""
    metadata = {"name": name, "parents": [folder_id]}
    media = MediaIoBaseUpload(
        io.BytesIO(pdf_bytes), mimetype="application/pdf", resumable=False,
    )
    file = drive_service.files().create(
        body=metadata, media_body=media, fields="id", supportsAllDrives=True,
    ).execute()
    return file["id"]


# --- Naming ---------------------------------------------------------------

_SANITIZE_RE = re.compile(r"[^\w\s\-#.,]+", re.UNICODE)


def sanitize_folder_name(name: str) -> str:
    """Remove caracteres problemáticos mantendo unicode (acentos)."""
    cleaned = _SANITIZE_RE.sub("", name).strip()
    return cleaned[:120]  # Drive limita comprimento; corta a 120 chars


# --- Playwright Yescapa ---------------------------------------------------

def yescapa_login(page):
    """Login na conta do parceiro Yescapa. Igual a yescapa_sheets._login()."""
    # Yescapa mudou URL de login em 2026-05: era /conexao/, agora é /login/email
    page.goto(f"{YESCAPA_BASE}/login/email?next=/", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(1500)

    for selector in [
        "#axeptio_btn_acceptAll", "#onetrust-accept-btn-handler",
        "button:has-text('Aceitar tudo')", "button:has-text('Aceitar')",
        "button:has-text('Accept all')",
    ]:
        try:
            page.click(selector, timeout=3000)
            break
        except Exception:
            pass

    for selector in ["input[type='email']", "input[name='email']", "#id_email", "#email"]:
        try:
            page.fill(selector, YESCAPA_EMAIL, timeout=5000)
            break
        except Exception:
            pass

    for selector in ["input[type='password']", "input[name='password']", "#id_password", "#password"]:
        try:
            page.fill(selector, YESCAPA_PASSWORD, timeout=5000)
            break
        except Exception:
            pass

    for selector in [
        "button:has-text('Conectar-se')",
        "button[type='submit']", "input[type='submit']",
        "button:has-text('Entrar')", "button:has-text('Se connecter')",
    ]:
        try:
            page.click(selector, timeout=5000)
            break
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


def download_pdf(page, url: str) -> Optional[bytes]:
    """Faz GET autenticado e devolve bytes do PDF (ou None se falhou)."""
    try:
        resp = page.request.get(url, timeout=20_000)
        if not resp.ok:
            log(f"    HTTP {resp.status}")
            return None
        body = resp.body()
        if body[:4] != b"%PDF":
            # Se não é PDF, pode ter sido HTML (a Yescapa pode mostrar página intermédia)
            log(f"    resposta não é PDF (primeiros 4 bytes: {body[:4]!r})")
            return None
        return body
    except Exception as e:
        log(f"    erro a baixar: {e}")
        return None


# --- Main flow ------------------------------------------------------------

def process_bookings():
    if not DRIVE_DOCS_FOLDER_ID:
        log("DRIVE_DOCS_FOLDER_ID não definida — a saltar download de documentos.")
        return {"baixados": 0, "ja_existiam": 0, "falhados": 0, "skipped_no_url": 0}

    if not YESCAPA_EMAIL or not YESCAPA_PASSWORD:
        log("YESCAPA_EMAIL/PASSWORD não definidas — a saltar.")
        return {"baixados": 0, "ja_existiam": 0, "falhados": 0, "skipped_no_url": 0}

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
        has_url = any((row.get(col) or "").strip() for col, _ in DOCS)
        if not has_url:
            continue
        candidates.append(row)
    log(f"  → {len(candidates)} reservas confirmed com pelo menos 1 URL")

    if not candidates:
        return {"baixados": 0, "ja_existiam": 0, "falhados": 0, "skipped_no_url": 0}

    # Drive: pasta raiz já existe (configurada via env var); criamos só ano e booking
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
                pdf_bytes = download_pdf(page, url)
                if not pdf_bytes:
                    falhados += 1
                    continue
                upload_pdf(drive_service, pdf_bytes, file_name, booking_folder_id)
                baixados += 1

        browser.close()

    log(f"=== Fim: baixados={baixados} já_existiam={ja_existiam} falhados={falhados} ===")
    return {
        "baixados": baixados,
        "ja_existiam": ja_existiam,
        "falhados": falhados,
    }


def main():
    log("=== yescapa_docs_downloader ===")
    return process_bookings()


if __name__ == "__main__":
    main()
