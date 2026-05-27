"""
drive_archiver.py — Arquiva no Google Drive os documentos de cada reserva Yescapa.

Para cada reserva confirmada ou já concluída (não cancelada) ainda sem documentos
arquivados, cria uma subpasta no Drive e faz upload de:
  - Fatura  — URL na folha "Reservas" (campo bill_url captado pelo sync)
  - Contrato de aluguer + Certificado de seguro (DEV1.1) — descarregados da
    página da reserva no Yescapa; a API NÃO os expõe (contract_url vem vazio),
    só existem como links/botões de download na página da reserva.

Tudo via uma sessão autenticada do Yescapa (Playwright — os documentos só são
acessíveis com os cookies do login).

Lê a worksheet "Reservas" (read-only) e a worksheet "Documentos" (state tracking).

Estrutura no Drive:
  <Drive Partilhado> / <ano> / "#3391737 - Pablo Espinillo" /
      Contrato.pdf, Seguro.pdf, Fatura.pdf

Safeguards anti-duplicado / anti-quota:
  1. Folha "Documentos" é a source-of-truth do estado por booking_id.
  2. Get-or-create de pastas e find-file antes de cada upload — idempotente,
     nunca duplica pastas nem ficheiros.
  3. DOCS_MAX_PER_RUN limita o nº de reservas tratadas por execução.
  4. Playwright só arranca se houver mesmo reservas pendentes.

NOTA sobre o destino no Drive: a service account não tem quota de
armazenamento própria. DRIVE_DOCS_ROOT_FOLDER_ID deve apontar para um
**Drive Partilhado** — numa pasta normal de "O meu Drive" o upload falha
com storageQuotaExceeded.

Env vars:
  GOOGLE_CREDENTIALS_JSON     (service account)
  DRIVE_DOCS_ROOT_FOLDER_ID   (ID da pasta-raiz / Drive Partilhado de destino)
  GOOGLE_SHEET_NAME           (default: "Reservas Yescapa")
  WORKSHEET_NAME              (default: "Reservas")
  DOCS_WORKSHEET              (default: "Documentos")
  DOCS_MAX_PER_RUN            (default: "15")
  DOCS_MAX_ATTEMPTS           (default: "3")
  DOCS_DEBUG                  (default: "false" — log detalhado do scraping)
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
DOCS_DEBUG = os.getenv("DOCS_DEBUG", "false").lower() in ("true", "1", "yes")

YESCAPA_EMAIL = os.getenv("YESCAPA_EMAIL", "")
YESCAPA_PASSWORD = os.getenv("YESCAPA_PASSWORD", "")
YESCAPA_BASE = "https://www.yescapa.pt"
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

# Colunas da folha "Reservas" com URLs de documentos → nome do ficheiro.
# A API só expõe a fatura (bill_url); o contrato/seguro vêm do scraping da
# página da reserva (ver scrape_booking_documents — DEV1.1).
DOC_COLUMNS = [
    ("Contrato", "Contrato.pdf"),
    ("Fatura", "Fatura.pdf"),
]

DOCS_HEADERS = [
    "booking_id", "estado", "timestamp", "pasta_drive",
    "contrato", "seguro", "fatura", "tentativas", "erro",
]

# Estados terminais — reservas nestes estados não voltam a ser processadas.
ESTADOS_TERMINAIS = {"arquivado", "falhou_permanente"}

# Classificação de um URL/link de documento por palavras-chave.
DOC_CLASSIFY = [
    ("Fatura.pdf", ("fatura", "factur", "facture", "invoice", "bill")),
    ("Seguro.pdf", ("seguro", "assur", "insur", "certificat", "attestation",
                    "apolice", "apólice", "garantie")),
    ("Contrato.pdf", ("contrato", "contract", "contrat", "agreement")),
]


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
    """Nome da subpasta da reserva: '#3391737 - Pablo Espinillo'.

    Hifen simples — alinhado com as pastas que o downloader anterior ja
    criou no Drive Partilhado, para nao duplicar.
    """
    return sanitize_folder_name(f"#{ref} - {nome}")


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


def classify_document(url: str, label: str = ""):
    """Classifica um URL/link como Contrato.pdf / Seguro.pdf / Fatura.pdf."""
    blob = (str(url) + " " + str(label)).lower()
    for filename, kws in DOC_CLASSIFY:
        if any(k in blob for k in kws):
            return filename
    return None


def _walk_urls(obj, out: set):
    """Recolhe recursivamente, de uma estrutura JSON, strings que parecem
    URLs de documentos (http + .pdf ou palavra-chave de documento)."""
    if isinstance(obj, dict):
        for v in obj.values():
            _walk_urls(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk_urls(v, out)
    elif isinstance(obj, str):
        s = obj.strip()
        low = s.lower()
        if s.lower().startswith("http") and (
            ".pdf" in low
            or any(k in low for _, kws in DOC_CLASSIFY for k in kws)
        ):
            out.add(s)


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


# --- Yescapa (login + download + scraping da página da reserva) -----------

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


def scrape_booking_documents(page, booking_id: str) -> dict:
    """DEV1.1 — Abre a página da reserva no Yescapa e devolve {nome_ficheiro: url}
    para o contrato de aluguer e o certificado de seguro (e fatura, se aparecer).

    A API do Yescapa não expõe o contrato nem o seguro (contract_url vem vazio),
    por isso vamos buscá-los à página da reserva. Estratégia dupla:
      (1) intercepta o JSON da API de detalhe que a SPA carrega e varre todos
          os campos à procura de URLs de documentos;
      (2) varre os links <a> do DOM.
    Classifica cada URL por palavras-chave. Com DOCS_DEBUG, regista tudo o que vê.
    """
    captured = {"json": None}

    def _on_response(resp):
        try:
            u = resp.url
            if re.search(r"/v\d+/booking[^?]*/" + re.escape(str(booking_id)), u):
                ct = (resp.headers or {}).get("content-type", "")
                if "json" in ct:
                    captured["json"] = resp.json()
        except Exception:
            pass

    page.on("response", _on_response)
    all_urls = set()
    dom_label = {}
    try:
        page.goto(
            f"{YESCAPA_BASE}/d/bookings/{booking_id}",
            wait_until="networkidle", timeout=30_000,
        )
        page.wait_for_timeout(3500)
    except Exception as e:
        log(f"    [scrape #{booking_id}] erro ao abrir a página: {e}")
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass

    # (1) URLs vindas do JSON da API de detalhe.
    if captured["json"]:
        _walk_urls(captured["json"], all_urls)

    # (2) Links <a> do DOM.
    try:
        dom_links = page.eval_on_selector_all(
            "a",
            "els => els.map(e => ({href: e.href || '', "
            "text: (e.textContent || '').trim().slice(0, 80)}))",
        )
    except Exception:
        dom_links = []
    for lk in dom_links:
        href = (lk.get("href") or "").strip()
        text = (lk.get("text") or "").strip()
        if not href.lower().startswith("http"):
            continue
        dom_label[href] = text
        low = (href + " " + text).lower()
        if ".pdf" in low or any(k in low for _, kws in DOC_CLASSIFY for k in kws):
            all_urls.add(href)

    if DOCS_DEBUG:
        log(f"    [scrape #{booking_id}] JSON detalhe={'sim' if captured['json'] else 'nao'} "
            f"| {len(dom_links)} links DOM | {len(all_urls)} URLs candidatas:")
        for u in sorted(all_urls):
            log(f"      · [{classify_document(u, dom_label.get(u, '')) or '?'}] {u[:150]}")

    # Classificar — primeira URL que cair em cada categoria.
    found = {}
    for u in sorted(all_urls):
        fn = classify_document(u, dom_label.get(u, ""))
        if fn and fn not in found:
            found[fn] = u
    if not found:
        log(f"    [scrape #{booking_id}] nenhum documento encontrado na página")
    return found


# --- Folha de estado "Documentos" -----------------------------------------

def ensure_docs_worksheet(spreadsheet):
    """Cria/garante a worksheet 'Documentos' com os headers corretos.

    Se os headers estiverem desalinhados (ex. esquema antigo sem a coluna
    'seguro'), limpa a folha e reescreve — o re-arquivo é idempotente.
    """
    rng = f"A1:{chr(64 + len(DOCS_HEADERS))}1"  # A1:I1
    try:
        ws = spreadsheet.worksheet(DOCS_WORKSHEET)
        if ws.row_values(1) != DOCS_HEADERS:
            log(f"Headers de '{DOCS_WORKSHEET}' desalinhados — a recriar a folha.")
            ws.clear()
            ws.update(rng, [DOCS_HEADERS])
        return ws
    except gspread.exceptions.WorksheetNotFound:
        log(f"Worksheet '{DOCS_WORKSHEET}' não existe — a criar.")
        ws = spreadsheet.add_worksheet(
            title=DOCS_WORKSHEET, rows=2000, cols=len(DOCS_HEADERS),
        )
        ws.update(rng, [DOCS_HEADERS])
        return ws


def load_state(ws) -> dict:
    """Devolve {booking_id (str): {estado, tentativas, _row_index, ...}}."""
    rows = ws.get_all_records()
    state = {}
    for idx, row in enumerate(rows):
        bid = str(row.get("booking_id", "")).strip()
        if not bid:
            continue
        try:
            tent = int(row.get("tentativas", 0) or 0)
        except (ValueError, TypeError):
            tent = 0
        state[bid] = {
            "estado": str(row.get("estado", "")).strip(),
            "tentativas": tent,
            "_row_index": idx + 2,  # +1 header, +1 base-1
        }
    return state


def upsert_state(ws, state_map: dict, booking_id: str, estado: str,
                 pasta: str = "", contrato: str = "", seguro: str = "",
                 fatura: str = "", tentativas: int = 0, erro: str = ""):
    """Escreve (insert ou update) o estado de uma reserva na folha Documentos."""
    booking_id = str(booking_id)
    row_values = [
        booking_id, estado, now_iso(), pasta,
        contrato, seguro, fatura, tentativas, erro,
    ]
    last_col = chr(64 + len(DOCS_HEADERS))  # 'I'
    if booking_id in state_map and "_row_index" in state_map[booking_id]:
        idx = state_map[booking_id]["_row_index"]
        ws.update(f"A{idx}:{last_col}{idx}", [row_values])
    else:
        ws.append_row(row_values, value_input_option="USER_ENTERED")
        state_map[booking_id] = {"_row_index": len(state_map) + 2}
    state_map[booking_id].update({"estado": estado, "tentativas": tentativas})


def _booking_year(row: dict) -> int:
    """Ano da reserva a partir da Data Inicio (dd/mm/yyyy). Fallback: ano atual."""
    m = re.search(r"(\d{4})", str(row.get("Data Início", "") or ""))
    return int(m.group(1)) if m else datetime.now(timezone.utc).year


def find_or_create_booking_folder(drive, ref, nome, parent_id):
    """Get-or-create da subpasta da reserva, identificada pelo prefixo '#ref'.

    Reaproveita uma pasta existente mesmo que o nome do hospede difira
    (capitalizacao, espacos) — a chave estavel e o ID da reserva.
    """
    ref = str(ref).strip()
    query = (
        f"name contains '#{ref}' "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and '{parent_id}' in parents and trashed = false"
    )
    result = drive.files().list(
        q=query, spaces="drive", fields="files(id, name)",
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    for f in result.get("files", []):
        name = f.get("name", "")
        if name == f"#{ref}" or name.startswith(f"#{ref} ") or name.startswith(f"#{ref}-"):
            return f["id"]
    metadata = {
        "name": booking_folder_name(ref, nome),
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = drive.files().create(
        body=metadata, fields="id", supportsAllDrives=True,
    ).execute()
    log(f"  pasta criada: '{metadata['name']}'")
    return folder["id"]


def resolve_booking_folder(drive, root_id, ref, nome, row):
    """Get-or-create <raiz>/<ano>/#ref - nome. Devolve o folder_id da reserva."""
    year_folder = find_or_create_folder(drive, str(_booking_year(row)), root_id)
    return find_or_create_booking_folder(drive, ref, nome, year_folder)


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

    # Pendentes: todas as reservas arquiváveis e ainda não terminais.
    # Já NÃO exigimos URL da fatura na folha — o contrato e o seguro vêm da
    # página da reserva (estão disponíveis logo após a confirmação), e a
    # fatura é acrescentada quando o Yescapa a gera (URL na folha).
    pendentes = []
    for row in rows:
        if not is_archivable(row):
            continue
        ref = str(row.get("ID", "")).strip()
        estado_atual = state_map.get(ref, {}).get("estado", "")
        if estado_atual in ESTADOS_TERMINAIS:
            continue
        pendentes.append((ref, row))

    if not pendentes:
        log("Nada a arquivar — todas as reservas elegíveis já estão tratadas.")
        return {"arquivados": 0, "aguarda_fatura": 0, "parciais": 0, "falhados": 0, "pendentes": 0}

    lote = pendentes[:DOCS_MAX_PER_RUN]
    log(f"{len(pendentes)} reservas pendentes — a processar {len(lote)} neste run.")

    arquivados = aguarda_fatura_count = parciais = falhados = 0

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
        logged_in = [False]  # login lazy — só quando precisamos mesmo de descarregar.

        def ensure_login():
            if not logged_in[0]:
                log("A fazer login no Yescapa...")
                yescapa_login(page)
                logged_in[0] = True

        for ref, row in lote:
            nome = full_guest_name(row)
            prev = state_map.get(ref, {})
            tentativas = int(prev.get("tentativas", 0) or 0)

            try:
                folder_id = resolve_booking_folder(
                    drive, DRIVE_DOCS_ROOT_FOLDER_ID, ref, nome, row,
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

            # O que já está no Drive?
            doc_links = {}
            for fn in ("Contrato.pdf", "Seguro.pdf", "Fatura.pdf"):
                fid = find_file(drive, fn, folder_id)
                if fid:
                    doc_links[fn] = file_url(fid)
            missing = [fn for fn in ("Contrato.pdf", "Seguro.pdf", "Fatura.pdf")
                       if fn not in doc_links]

            if not missing:
                estado = "arquivado"
                arquivados += 1
                log(f"  ✓ #{ref}: já estava tudo arquivado")
                upsert_state(
                    docs_ws, state_map, ref, estado,
                    pasta=folder_url(folder_id),
                    contrato=doc_links.get("Contrato.pdf", ""),
                    seguro=doc_links.get("Seguro.pdf", ""),
                    fatura=doc_links.get("Fatura.pdf", ""),
                    tentativas=tentativas,
                )
                time.sleep(0.4)
                continue

            # Recolher URLs dos documentos em falta:
            #   - Fatura: coluna 'Fatura' da folha (URL tokenizada do Yescapa)
            #   - Contrato/Seguro: raspagem da página da reserva (DEV1.1)
            doc_urls = {}
            sheet_urls = collect_doc_urls(row)
            if "Fatura.pdf" in missing and "Fatura.pdf" in sheet_urls:
                doc_urls["Fatura.pdf"] = sheet_urls["Fatura.pdf"]

            need_scrape = ("Contrato.pdf" in missing) or ("Seguro.pdf" in missing)
            if need_scrape:
                ensure_login()
                try:
                    scraped = scrape_booking_documents(page, ref)
                except Exception as e:
                    log(f"  · #{ref}: scraping falhou ({type(e).__name__}: {e})")
                    scraped = {}
                for fn in ("Contrato.pdf", "Seguro.pdf", "Fatura.pdf"):
                    if fn in missing and fn in scraped:
                        doc_urls.setdefault(fn, scraped[fn])

            falhas = []
            if doc_urls:
                ensure_login()
            for fn, url in doc_urls.items():
                log(f"  #{ref} '{nome}' → {fn}")
                pdf = download_pdf(page, url)
                if not pdf:
                    falhas.append(fn)
                    continue
                try:
                    fid = upload_pdf(drive, pdf, fn, folder_id)
                    doc_links[fn] = file_url(fid)
                except Exception as e:
                    log(f"    ✗ upload falhou ({type(e).__name__}: {e})")
                    falhas.append(fn)

            still_missing = [fn for fn in ("Contrato.pdf", "Seguro.pdf", "Fatura.pdf")
                             if fn not in doc_links]

            if not still_missing:
                estado = "arquivado"
                arquivados += 1
                log(f"  ✓ #{ref}: arquivado ({len(doc_links)} doc.)")
            elif still_missing == ["Fatura.pdf"] and not falhas:
                # Contrato+seguro arquivados; a fatura ainda não foi gerada pelo
                # Yescapa. Próximo run só verifica a coluna Fatura — sem voltar
                # a navegar a página da reserva.
                estado = "aguarda_fatura"
                aguarda_fatura_count += 1
                log(f"  · #{ref}: aguarda fatura (contrato+seguro arquivados)")
            else:
                tentativas += 1
                if tentativas >= DOCS_MAX_ATTEMPTS:
                    estado = "falhou_permanente"
                    falhados += 1
                    log(f"  ✗ #{ref}: falhou em definitivo após {tentativas} tentativas — em falta: {still_missing}")
                else:
                    estado = "parcial"
                    parciais += 1
                    log(f"  · #{ref}: parcial (tentativa {tentativas}) — em falta: {still_missing}, falhas: {falhas}")

            upsert_state(
                docs_ws, state_map, ref, estado,
                pasta=folder_url(folder_id),
                contrato=doc_links.get("Contrato.pdf", ""),
                seguro=doc_links.get("Seguro.pdf", ""),
                fatura=doc_links.get("Fatura.pdf", ""),
                tentativas=tentativas,
                erro="" if not still_missing else f"em falta: {', '.join(still_missing)}",
            )
            time.sleep(0.4)  # folga para a Sheets API (limite 60 escritas/min)

        browser.close()

    restantes = len(pendentes) - len(lote)
    log(
        f"=== Fim: arquivados={arquivados} aguarda_fatura={aguarda_fatura_count} "
        f"parciais={parciais} falhados={falhados} | {restantes} ainda na fila ==="
    )
    return {
        "arquivados": arquivados,
        "aguarda_fatura": aguarda_fatura_count,
        "parciais": parciais,
        "falhados": falhados,
        "pendentes": restantes,
    }


def main():
    """Entry point para ser chamado do run.py."""
    return run()


if __name__ == "__main__":
    main()
