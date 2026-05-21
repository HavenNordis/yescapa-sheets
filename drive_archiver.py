"""
drive_archiver.py — Arquiva no Google Drive os documentos de cada reserva Yescapa.

Para cada reserva confirmada ou já concluída (não cancelada) ainda sem documentos
arquivados, cria uma subpasta no Drive e faz upload do Contrato e da Fatura
descarregados do Yescapa (sessão autenticada via Playwright — os documentos só
são acessíveis com os cookies do login).

Lê a worksheet "Reservas" (read-only) e a worksheet "Documentos" (state tracking).

Estrutura no Drive:
  <pasta-raiz partilhada> / "#3391737 — Pablo Espinillo" / Contrato.pdf, Fatura.pdf

Safeguards anti-duplicado / anti-quota:
  1. Folha "Documentos" é a source-of-truth do estado por booking_id.
  2. Get-or-create de pastas e find-file antes de cada upload — idempotente,
     nunca duplica pastas nem ficheiros.
  3. DOCS_MAX_PER_RUN limita o nº de reservas tratadas por execução, para
     espalhar um backfill grande por vários crons (evita o rate-limit da
     Sheets API, 60 escritas/min).
  4. Playwright só arranca se houver mesmo reservas pendentes com documentos.

NOTA sobre o destino no Drive: a service account não tem quota de
armazenamento própria. Para o upload de PDFs funcionar, DRIVE_DOCS_ROOT_FOLDER_ID
deve apontar para um **Drive Partilhado** (Shared Drive) onde a service account
seja membro — ou uma pasta dentro dele. Numa pasta normal de "O meu Drive" o
upload de ficheiros binários pela service account falha com storageQuotaExceeded.

Env vars:
  GOOGLE_CREDENTIALS_JSON     (service account — reutilizado do yescapa_sheets.py)
  DRIVE_DOCS_ROOT_FOLDER_ID   (ID da pasta-raiz / Drive Partilhado de destino)
  GOOGLE_SHEET_NAME           (default: "Reservas Yescapa")
  WORKSHEET_NAME              (default: "Reservas")
  DOCS_WORKSHEET              (default: "Documentos")
  DOCS_MAX_PER_RUN            (default: "15")
  DOCS_MAX_ATTEMPTS           (default: "3")
  YESCAPA_EMAIL / YESCAPA_PASSWORD
  HEADLESS                    (default: "true")
"""

import io
import json
import os
import re
import time
from datetime import datetime, timezone

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from playwright.sync_api import sync_playwright

load_dotenv()

# --- Configuração ---------------------------------------------------------

GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Reservas Yescapa")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Reservas")
DOCS_WORKSHEET = os.getenv("DOCS_WORKSHEET", "Documentos")
DRIVE_DOCS_ROOT_FOLDER_ID = os.getenv("DRIVE_DOCS_ROOT_FOLDER_ID", "").strip()

DOCS_MAX_PER_RUN = int(os.getenv("DOCS_MAX_PER_RUN", "15"))
DOCS_MAX_ATTEMPTS = int(os.getenv("DOCS_MAX_ATTEMPTS", "3"))

YESCAPA_EMAIL = os.getenv("YESCAPA_EMAIL", "")
YESCAPA_PASSWORD = os.getenv("YESCAPA_PASSWORD", "")
YESCAPA_BASE = "https://www.yescapa.pt"
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

# Colunas da folha "Reservas" com URLs de documentos → nome do ficheiro no Drive.
# NOTA: o certificado de seguro ainda não é captado pelo sync (parse_booking só
# expõe contract_url e bill_url). Acrescentar aqui assim que o campo da API for
# identificado — ver roadmap D1.4.
DOC_COLUMNS = [
    ("Contrato", "Contrato.pdf"),
    ("Fatura", "Fatura.pdf"),
]

DOCS_HEADERS = [
    "booking_id", "estado", "timestamp", "pasta_drive",
    "contrato", "fatura", "tentativas", "erro",
]

# Estados terminais — reservas nestes estados não voltam a ser processadas.
ESTADOS_TERMINAIS = {"arquivado", "falhou_permanente"}


# --- Logging --------------------------------------------------------------

def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] [drive-archiver] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H:%M:%S")


# --- Funções puras (testáveis sem rede) -----------------------------------

_SANITIZE_RE = re.compile(r"[^\w\s\-#.,—]+", re.UNICODE)


def sanitize_folder_name(name: str) -> str:
    """Remove caracteres problemáticos de um nome de pasta/ficheiro do Drive."""
    cleaned = _SANITIZE_RE.sub("", name or "").strip()
    return cleaned[:120] or "Sem Nome"


def full_guest_name(row: dict) -> str:
    """Junta 'Hóspede Nome' + 'Hóspede Apelido' da folha Reservas."""
    nome = str(row.get("Hóspede Nome", "") or "").strip()
    apelido = str(row.get("Hóspede Apelido", "") or "").strip()
    return f"{nome} {apelido}".strip() or "Sem Nome"


def booking_folder_name(ref: str, nome: str) -> str:
    """Nome da subpasta da reserva: '#3391737 — Pablo Espinillo' (roadmap D1.2)."""
    return sanitize_folder_name(f"#{ref} — {nome}")


def is_archivable(row: dict) -> bool:
    """Decide se uma reserva deve ter os documentos arquivados.

    Regra (roadmap D1.3): reservas 'confirmed' ou 'archived' (passadas), sem
    canceladas. Reservas diretas (ID não numérico) não têm PDFs no Yescapa.
    """
    ref = str(row.get("ID", "") or "").strip()
    if not ref or not ref.isdigit():
        return False
    meta = str(row.get("Estado Meta", "") or "").strip().lower()
    if meta not in ("confirmed", "archived"):
        return False
    estado = str(row.get("Estado", "") or "").strip().upper()
    if "CANCELLED" in estado or "CANCELED" in estado:
        return False
    return True


def collect_doc_urls(row: dict) -> dict:
    """Devolve {nome_ficheiro: url} para as colunas de documentos preenchidas."""
    urls = {}
    for col, filename in DOC_COLUMNS:
        url = str(row.get(col, "") or "").strip()
        if url:
            if not url.lower().startswith("http"):
                url = YESCAPA_BASE.rstrip("/") + "/" + url.lstrip("/")
            urls[filename] = url
    return urls


# --- Google (service account — partilhado com o yescapa_sheets.py) --------

def get_google_clients():
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON") or ""
    if not raw:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON não definida.")
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


def _escape(name: str) -> str:
    return name.replace("\\", "\\\\").replace("'", "\\'")


def find_or_create_folder(drive, name: str, parent_id: str) -> str:
    """Devolve o ID de uma subpasta com este nome dentro de parent_id,
    criando-a se não existir. Idempotente."""
    query = (
        f"name = '{_escape(name)}' "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents and trashed = false"
    )
    result = drive.files().list(
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
    folder = drive.files().create(
        body=metadata, fields="id", supportsAllDrives=True,
    ).execute()
    log(f"  pasta criada: '{name}'")
    return folder["id"]


def find_file(drive, name: str, folder_id: str):
    """Devolve o ID de um ficheiro com este nome na pasta, ou None."""
    query = (
        f"name = '{_escape(name)}' "
        f"and '{folder_id}' in parents and trashed = false"
    )
    result = drive.files().list(
        q=query, spaces="drive", fields="files(id, name)",
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    files = result.get("files", [])
    return files[0]["id"] if files else None


def upload_pdf(drive, pdf_bytes: bytes, name: str, folder_id: str) -> str:
    """Faz upload de um PDF para a pasta e devolve o ID do ficheiro."""
    metadata = {"name": name, "parents": [folder_id]}
    media = MediaIoBaseUpload(
        io.BytesIO(pdf_bytes), mimetype="application/pdf", resumable=False,
    )
    file = drive.files().create(
        body=metadata, media_body=media, fields="id", supportsAllDrives=True,
    ).execute()
    return file["id"]


def folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def file_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


# --- Yescapa (login + download autenticado) -------------------------------

def yescapa_login(page):
    """Faz login no Yescapa. Os documentos só são acessíveis com estes cookies."""
    page.goto(
        f"{YESCAPA_BASE}/conexao/",
        wait_until="domcontentloaded", timeout=30_000,
    )
    page.wait_for_timeout(1500)

    for sel in [
        "#axeptio_btn_acceptAll", "#onetrust-accept-btn-handler",
        "button:has-text('Aceitar tudo')", "button:has-text('Aceitar')",
        "button:has-text('Accept all')",
    ]:
        try:
            page.click(sel, timeout=3000)
            break
        except Exception:
            pass

    for sel in ["input[type='email']", "input[name='email']", "#id_email", "#email"]:
        try:
            page.fill(sel, YESCAPA_EMAIL, timeout=5000)
            break
        except Exception:
            pass

    for sel in ["input[type='password']", "input[name='password']", "#id_password", "#password"]:
        try:
            page.fill(sel, YESCAPA_PASSWORD, timeout=5000)
            break
        except Exception:
            pass

    for sel in [
        "button[type='submit']", "input[type='submit']",
        "button:has-text('Entrar')", "button:has-text('Conectar-se')",
        "button:has-text('Se connecter')",
    ]:
        try:
            page.click(sel, timeout=5000)
            break
        except Exception:
            pass

    try:
        page.wait_for_function(
            "() => !window.location.pathname.includes('conexao') && "
            "!window.location.pathname.includes('login')",
            timeout=20_000,
        )
        log(f"  login Yescapa OK ({page.url})")
    except Exception:
        log(f"  aviso: URL após login: {page.url}")
    page.wait_for_timeout(2000)


def download_pdf(page, url: str):
    """Descarrega um PDF via a sessão autenticada. Devolve bytes ou None."""
    try:
        resp = page.request.get(url, timeout=25_000)
    except Exception as e:
        log(f"    erro de rede: {e}")
        return None
    if not resp.ok:
        log(f"    HTTP {resp.status}")
        return None
    try:
        body = resp.body()
    except Exception as e:
        log(f"    erro a ler corpo: {e}")
        return None
    if body[:4] != b"%PDF":
        log(f"    resposta não é PDF (primeiros bytes: {body[:8]!r})")
        return None
    return body


# --- Folha de estado "Documentos" -----------------------------------------

def ensure_docs_worksheet(spreadsheet):
    """Cria a worksheet 'Documentos' se não existir, com headers."""
    try:
        ws = spreadsheet.worksheet(DOCS_WORKSHEET)
        if ws.row_values(1) != DOCS_HEADERS:
            log(f"Headers desalinhados em '{DOCS_WORKSHEET}', a corrigir.")
            ws.update("A1:H1", [DOCS_HEADERS])
        return ws
    except gspread.exceptions.WorksheetNotFound:
        log(f"Worksheet '{DOCS_WORKSHEET}' não existe — a criar.")
        ws = spreadsheet.add_worksheet(title=DOCS_WORKSHEET, rows=2000, cols=8)
        ws.update("A1:H1", [DOCS_HEADERS])
        return ws


def load_state(ws) -> dict:
    """Devolve {booking_id (str): {estado, tentativas, _row_index, ...}}."""
    rows = ws.get_all_records()
    state = {}
    for idx, row in enumerate(rows):
        bid = str(row.get("booking_id", "")).strip()
        if not bid:
            continue
        state[bid] = {
            "estado": str(row.get("estado", "")).strip(),
            "tentativas": int(row.get("tentativas", 0) or 0),
            "_row_index": idx + 2,  # +1 header, +1 base-1
        }
    return state


def upsert_state(ws, state_map: dict, booking_id: str, estado: str,
                 pasta: str = "", contrato: str = "", fatura: str = "",
                 tentativas: int = 0, erro: str = ""):
    """Escreve (insert ou update) o estado de uma reserva na folha Documentos."""
    booking_id = str(booking_id)
    row_values = [
        booking_id, estado, now_iso(), pasta,
        contrato, fatura, tentativas, erro,
    ]
    if booking_id in state_map and "_row_index" in state_map[booking_id]:
        idx = state_map[booking_id]["_row_index"]
        ws.update(f"A{idx}:H{idx}", [row_values])
    else:
        ws.append_row(row_values, value_input_option="USER_ENTERED")
        state_map[booking_id] = {"_row_index": len(state_map) + 2}
    state_map[booking_id].update({"estado": estado, "tentativas": tentativas})


# --- Orquestração ---------------------------------------------------------

def run() -> dict:
    log("=== drive_archiver ===")

    if not DRIVE_DOCS_ROOT_FOLDER_ID:
        log("DRIVE_DOCS_ROOT_FOLDER_ID não definida — a saltar arquivo de documentos.")
        return {"arquivados": 0, "parciais": 0, "falhados": 0, "saltado": True}

    if not (YESCAPA_EMAIL and YESCAPA_PASSWORD):
        log("Sem credenciais Yescapa — a saltar.")
        return {"arquivados": 0, "parciais": 0, "falhados": 0, "saltado": True}

    sheets, drive = get_google_clients()
    spreadsheet = sheets.open(GOOGLE_SHEET_NAME)
    reservas_ws = spreadsheet.worksheet(WORKSHEET_NAME)
    docs_ws = ensure_docs_worksheet(spreadsheet)

    log(f"A ler folha '{WORKSHEET_NAME}'...")
    rows = reservas_ws.get_all_records()
    log(f"  → {len(rows)} reservas")

    log(f"A ler estado '{DOCS_WORKSHEET}'...")
    state_map = load_state(docs_ws)
    log(f"  → {len(state_map)} entradas de estado")

    # Selecionar reservas pendentes: arquiváveis, com ≥1 URL, não terminais.
    pendentes = []
    for row in rows:
        if not is_archivable(row):
            continue
        urls = collect_doc_urls(row)
        if not urls:
            continue  # sem documentos disponíveis ainda — re-verifica num run futuro
        ref = str(row.get("ID", "")).strip()
        estado_atual = state_map.get(ref, {}).get("estado", "")
        if estado_atual in ESTADOS_TERMINAIS:
            continue
        pendentes.append((ref, row, urls))

    if not pendentes:
        log("Nada a arquivar — todas as reservas elegíveis já estão tratadas.")
        return {"arquivados": 0, "parciais": 0, "falhados": 0, "pendentes": 0}

    lote = pendentes[:DOCS_MAX_PER_RUN]
    log(f"{len(pendentes)} reservas pendentes — a processar {len(lote)} neste run.")

    arquivados = parciais = falhados = 0

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

        for ref, row, urls in lote:
            nome = full_guest_name(row)
            prev = state_map.get(ref, {})
            tentativas = int(prev.get("tentativas", 0) or 0)

            try:
                folder_id = find_or_create_folder(
                    drive, booking_folder_name(ref, nome), DRIVE_DOCS_ROOT_FOLDER_ID,
                )
            except Exception as e:
                log(f"  ✗ #{ref}: falha a criar pasta ({type(e).__name__}: {e})")
                tentativas += 1
                estado = "falhou_permanente" if tentativas >= DOCS_MAX_ATTEMPTS else "parcial"
                upsert_state(docs_ws, state_map, ref, estado,
                             tentativas=tentativas, erro=f"pasta: {str(e)[:150]}")
                if estado == "falhou_permanente":
                    falhados += 1
                else:
                    parciais += 1
                time.sleep(0.4)
                continue

            doc_links = {"Contrato.pdf": "", "Fatura.pdf": ""}
            falhas = []
            for filename, url in urls.items():
                existing = find_file(drive, filename, folder_id)
                if existing:
                    doc_links[filename] = file_url(existing)
                    continue
                log(f"  #{ref} '{nome}' → {filename}")
                pdf = download_pdf(page, url)
                if not pdf:
                    falhas.append(filename)
                    continue
                try:
                    fid = upload_pdf(drive, pdf, filename, folder_id)
                    doc_links[filename] = file_url(fid)
                except Exception as e:
                    log(f"    ✗ upload falhou ({type(e).__name__}: {e})")
                    falhas.append(filename)

            if not falhas:
                estado = "arquivado"
                arquivados += 1
                log(f"  ✓ #{ref}: arquivado ({len([v for v in doc_links.values() if v])} doc.)")
            else:
                tentativas += 1
                if tentativas >= DOCS_MAX_ATTEMPTS:
                    estado = "falhou_permanente"
                    falhados += 1
                    log(f"  ✗ #{ref}: falhou em definitivo após {tentativas} tentativas")
                else:
                    estado = "parcial"
                    parciais += 1
                    log(f"  · #{ref}: parcial (tentativa {tentativas}) — falhou: {falhas}")

            upsert_state(
                docs_ws, state_map, ref, estado,
                pasta=folder_url(folder_id),
                contrato=doc_links.get("Contrato.pdf", ""),
                fatura=doc_links.get("Fatura.pdf", ""),
                tentativas=tentativas,
                erro="" if not falhas else f"sem documento: {', '.join(falhas)}",
            )
            time.sleep(0.4)  # folga para a Sheets API (limite 60 escritas/min)

        browser.close()

    restantes = len(pendentes) - len(lote)
    log(
        f"=== Fim: arquivados={arquivados} parciais={parciais} "
        f"falhados={falhados} | {restantes} ainda na fila ==="
    )
    return {
        "arquivados": arquivados,
        "parciais": parciais,
        "falhados": falhados,
        "pendentes": restantes,
    }


def main():
    """Entry point para ser chamado do run.py."""
    return run()


if __name__ == "__main__":
    main()
