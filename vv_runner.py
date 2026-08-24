"""
vv_runner.py — Via Verde movement scraper

Corre diariamente (chamado por run.py às 19h30 Lisboa).
Para cada reserva activa (checkin-1 a checkout+3 dias):
  1. Acede ao portal viaverde.pt (empresas)
  2. Filtra movimentos por matrícula + período da reserva
  3. Carrega todos os resultados ("Ver mais")
  4. Faz upsert de cada passagem em VV_Passagens (uma linha por passagem)
  5. Recalcula vv_real_confirmado em Cauções com base nas passagens com incluir=TRUE

O staff pode excluir passagens individualmente na App (manutenções, passagens pós-checkout).

Env vars:
  VV_EMAIL              (obrigatório)
  VV_PASSWORD           (obrigatório)
  GOOGLE_CREDENTIALS_JSON
  GOOGLE_SHEET_NAME     (default: "Reservas Yescapa")
  APP_SHEET_NAME        (default: "Haven Nordis · App (Tarefas e Manutenções)")
"""

import base64
import json
import os
import re
import time
from datetime import datetime, timedelta, date, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from html import escape as _html_escape
from zoneinfo import ZoneInfo

import gspread
import requests
from html.parser import HTMLParser
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from playwright.sync_api import sync_playwright

LISBON_TZ = ZoneInfo("Europe/Lisbon")

# --- Alerta de portagens tardias (email interno ops@) ---
# Reutiliza as MESMAS env vars do whatsapp_notification.py (Gmail API já
# configurada no Railway). Se faltarem, o alerta é ignorado sem partir o scraper.
OPS_NOTIFICATION_EMAIL = os.environ.get("OPS_NOTIFICATION_EMAIL", "ops@havennordis.com")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "ops@havennordis.com")
SENDER_NAME = os.environ.get("SENDER_NAME", "Haven Nordis")
GMAIL_CLIENT_ID = os.environ.get("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET = os.environ.get("GMAIL_CLIENT_SECRET")
GMAIL_REFRESH_TOKEN = os.environ.get("GMAIL_REFRESH_TOKEN")

VV_EMAIL = os.environ.get("VV_EMAIL", "")
VV_PASSWORD = os.environ.get("VV_PASSWORD", "")
VV_URL = "https://www.viaverde.pt/empresas/minha-via-verde/extratos-movimentos"

GOOGLE_SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "Reservas Yescapa")
APP_SHEET_NAME = os.environ.get("APP_SHEET_NAME", "Haven Nordis · App (Tarefas e Manutenções)")
TAB_RESERVAS = "Reservas"
TAB_CAUCOES = "Caucoes"
TAB_VV_PASS = "VV_Passagens"

VEHICLES = {
    "Celta": "CE-60-LH",
    "Runa": "CH-61-GD",
    "Fjord": "CF-68-JJ",
    # Multivans (aluguer turismo, Yescapa conta João): tenta recolher Via Verde delas.
    # Se não tiverem transponder nesta conta Via Verde, simplesmente não devolve passagens.
    "Freya": "CE-07-JV",   # azul 8 lugares
    "Gaia":  "BC-06-NP",   # prata 7 lugares
}


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] [vv-runner] {msg}", flush=True)


# ── Google Sheets ──────────────────────────────────────────────────────────────

def get_gc():
    raw = os.environ.get("GOOGLE_CREDENTIALS_JSON") or ""
    if not raw:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON não definida.")
    info = json.loads(raw)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def parse_date(s):
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(str(s).strip(), fmt).date()
        except ValueError:
            continue
    return None


def normalize_plate(plate):
    return re.sub(r"[-\s]", "", str(plate)).upper()


# Janela de varrimento diário: últimos N dias.
# 60 (e não 30) porque as portagens ESTRANGEIRAS (Espanha/roaming) entram na
# Via Verde com atraso de semanas — filtramos pela DATA DA PASSAGEM, por isso
# uma janela curta perdia-as para sempre quando só apareciam >30 dias depois.
# O upsert é idempotente e nunca sobrescreve incluir/notas, logo reprocessar
# dias antigos é seguro.
# NB (ago/2026): BACKFILL março–agosto em curso — valor temporariamente 200.
# REVERTER para 60 depois de um run_vv completar (repõe a janela normal).
# Overridável por env VV_SCRAPE_DAYS.
SCRAPE_DAYS = int(os.environ.get("VV_SCRAPE_DAYS", "200"))

# Acima deste nº de dias considera-se "backfill": desliga o alerta de portagens
# tardias (senão o 1º varrimento largo mandava um email gigante a ops@).
BACKFILL_THRESHOLD_DAYS = 90

def get_scrape_window():
    """Devolve (date_from, date_to) para o varrimento diário."""
    today = datetime.now(LISBON_TZ).date()
    return today - timedelta(days=SCRAPE_DAYS), today


def _ws_records(ws):
    """get_all_records tolerante a colunas vazias/duplicadas no cabeçalho."""
    try:
        return ws.get_all_records()
    except gspread.exceptions.GSpreadException:
        values = ws.get_all_values()
        if not values:
            return []
        headers = values[0]
        records = []
        for row_vals in values[1:]:
            row_dict = {}
            for i, h in enumerate(headers):
                if h and i < len(row_vals):
                    row_dict[h] = row_vals[i]
            records.append(row_dict)
        return records


def get_caucao_row(app_ss, bid):
    """Devolve (worksheet, row_dict ou None)."""
    try:
        ws = app_ss.worksheet(TAB_CAUCOES)
    except gspread.exceptions.WorksheetNotFound:
        return None, None
    for row in _ws_records(ws):
        if str(row.get("booking_id", "")).strip() == str(bid).strip():
            return ws, row
    return ws, None


def get_vv_pass_ws(app_ss):
    try:
        return app_ss.worksheet(TAB_VV_PASS)
    except gspread.exceptions.WorksheetNotFound:
        return None


def upsert_passages(app_ss, passages, plate):
    """Insere passagens em VV_Passagens sem associação a reservas.
    Chave de upsert: (data, local, matricula).
    Nunca sobrescreve incluir/notas — preserva decisões do staff.

    Devolve a lista das passagens REALMENTE novas (inseridas neste run), cada uma
    como dict {date, hora, location, amount, plate} — usada pelo alerta de
    portagens tardias. (Antes devolvia só a contagem de inseridas.)
    """
    ws = get_vv_pass_ws(app_ss)
    if ws is None:
        log("  ⚠ Tab VV_Passagens não encontrada")
        return []

    now_str = datetime.now(LISBON_TZ).strftime("%d/%m/%Y %H:%M")
    all_vals = ws.get_all_values()
    if not all_vals:
        return []

    headers = all_vals[0]
    def hcol(name):
        return headers.index(name) if name in headers else -1

    # Índice de chaves existentes: (data, local, matricula) → row index (2-based)
    existing = {}
    for i, row in enumerate(all_vals[1:], start=2):
        key = (
            row[hcol("data")]      if hcol("data")      >= 0 else "",
            row[hcol("local")]     if hcol("local")     >= 0 else "",
            row[hcol("matricula")] if hcol("matricula") >= 0 else "",
        )
        existing[key] = i

    new_passages = []
    for px in passages:
        date_str = px["date"].strftime("%d/%m/%Y")
        key = (date_str, px["location"], plate)

        if key not in existing:
            new_row = [""] * len(headers)
            for field, val in [
                ("data",          date_str),
                ("hora",          px.get("hora", "")),
                ("local",         px["location"]),
                ("matricula",     plate),
                ("valor",         px["amount"]),
                ("incluir",       "TRUE"),
                ("notas",         ""),
                ("criado_em",     now_str),
                ("atualizado_em", now_str),
            ]:
                c = hcol(field)
                if c >= 0:
                    new_row[c] = val
            ws.append_row(new_row, value_input_option="USER_ENTERED")
            new_passages.append({
                "date": px["date"], "hora": px.get("hora", ""),
                "location": px["location"], "amount": px["amount"], "plate": plate,
            })
        else:
            # Actualiza valor, hora e timestamp — nunca toca em incluir/notas
            row_idx = existing[key]
            updates = {}
            if hcol("valor") >= 0:
                updates[hcol("valor")] = px["amount"]
            if hcol("hora") >= 0:
                updates[hcol("hora")] = px.get("hora", "")
            if hcol("atualizado_em") >= 0:
                updates[hcol("atualizado_em")] = now_str
            for col_idx, val in updates.items():
                ws.update_cell(row_idx, col_idx + 1, val)

    return new_passages


def recalc_vv_total(app_ss, bid):
    """Soma passagens com incluir=TRUE e grava em Cauções."""
    ws = get_vv_pass_ws(app_ss)
    if ws is None:
        return 0.0

    all_vals = ws.get_all_values()
    if not all_vals:
        return 0.0

    headers = all_vals[0]
    def hcol(name):
        return headers.index(name) if name in headers else -1

    total = 0.0
    for row in all_vals[1:]:
        if len(row) <= max(hcol("booking_id"), hcol("valor"), hcol("incluir")):
            continue
        if row[hcol("booking_id")].strip() != str(bid).strip():
            continue
        if row[hcol("incluir")].strip().upper() != "TRUE":
            continue
        try:
            total += float(str(row[hcol("valor")]).replace(",", "."))
        except (ValueError, IndexError):
            pass

    total = round(total, 2)

    # Actualizar Cauções
    try:
        ws_cau = app_ss.worksheet(TAB_CAUCOES)
        headers_cau = ws_cau.row_values(1)
        now_str = datetime.now(LISBON_TZ).strftime("%d/%m/%Y %H:%M")
        today_str = datetime.now(LISBON_TZ).strftime("%d/%m/%Y")
        for i, r in enumerate(_ws_records(ws_cau), start=2):
            if str(r.get("booking_id", "")).strip() == str(bid).strip():
                def col(n):
                    return headers_cau.index(n) + 1
                ws_cau.update_cell(i, col("vv_real_confirmado"), total if total > 0 else "")
                ws_cau.update_cell(i, col("vv_data_consulta"), today_str)
                ws_cau.update_cell(i, col("atualizado_em"), now_str)
                log(f"  ✓ VV total: booking {bid} → {total:.2f}€")
                return total
    except Exception as e:
        log(f"  ⚠ Erro ao actualizar Cauções: {e}")
    return total


def write_vv(app_ss, bid, vv_total, vv_obs):
    ws, row = get_caucao_row(app_ss, bid)
    if ws is None:
        log(f"  ⚠ Tab Caucoes não encontrada em '{APP_SHEET_NAME}'")
        return
    if row is None:
        log(f"  ⚠ Booking {bid} não tem entrada em Cauções — cria primeiro na App")
        return

    headers = ws.row_values(1)

    def col(name):
        return headers.index(name) + 1

    today_str = datetime.now(LISBON_TZ).strftime("%d/%m/%Y")
    now_str = datetime.now(LISBON_TZ).strftime("%d/%m/%Y %H:%M")

    for i, r in enumerate(_ws_records(ws), start=2):
        if str(r.get("booking_id", "")).strip() == str(bid).strip():
            ws.update_cell(i, col("vv_real_confirmado"), vv_total)
            ws.update_cell(i, col("vv_obs"), vv_obs)
            ws.update_cell(i, col("vv_data_consulta"), today_str)
            ws.update_cell(i, col("atualizado_em"), now_str)
            log(f"  ✓ Gravado: booking {bid} → VV {vv_total:.2f}€")
            return


# ── Via Verde scraping ─────────────────────────────────────────────────────────

def _set_angular(page, selector, value):
    """Escreve num input Angular sem que o framework ignore a mudança."""
    page.evaluate(
        """([sel, val]) => {
            const el = document.querySelector(sel);
            if (!el) return;
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, val);
            ['input', 'change', 'keyup'].forEach(ev =>
                el.dispatchEvent(new Event(ev, {bubbles: true})));
        }""",
        [selector, value],
    )


class _HiddenFieldParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        if tag == "input":
            d = dict(attrs)
            if d.get("type") == "hidden" and d.get("name"):
                self.fields[d["name"]] = d.get("value", "")


def _login(page):
    """Faz login via AJAX API do Via Verde e transfere cookies para Playwright."""
    api_url = (
        "https://www.viaverde.pt/DesktopModules/UserLogin/Api.ashx"
        "?returnurl=%2fempresas%2fminha-via-verde%2fextratos-movimentos"
    )
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    sess = requests.Session()
    sess.headers.update({
        "User-Agent": ua,
        "Referer": "https://www.viaverde.pt/empresas/",
        "Origin": "https://www.viaverde.pt",
    })

    # Primeiro: GET à página principal para obter o ASP.NET_SessionId
    sess.get("https://www.viaverde.pt/empresas/", timeout=30)

    # Login via API AJAX
    resp = sess.post(api_url, data={
        "action": "login",
        "username": VV_EMAIL,
        "password": VV_PASSWORD,
        "rememberMe": "true",
        "lang": "pt-PT",
        "forceVVS": "false",
        "savedUrl": "%2fempresas%2fminha-via-verde%2fextratos-movimentos",
    }, timeout=30)

    if resp.status_code != 200:
        raise RuntimeError(f"Login API falhou com status {resp.status_code}")

    try:
        result = resp.json()
        if not result.get("success") and not result.get("redirectUrl"):
            raise RuntimeError(f"Login recusado: {result}")
    except Exception:
        pass  # Alguns respostas não são JSON

    # Transferir cookies para o contexto Playwright
    playwright_cookies = []
    for c in sess.cookies:
        playwright_cookies.append({
            "name": c.name,
            "value": c.value,
            "domain": "www.viaverde.pt" if not c.domain.startswith(".") else c.domain,
            "path": c.path or "/",
        })
    page.context.add_cookies(playwright_cookies)
    log(f"  ✓ Login efectuado (AJAX API, {len(playwright_cookies)} cookies)")


def _is_logged_in(page):
    """Verifica login via URL — mais fiável que procurar 'logout' no HTML."""
    url = page.url
    if "returnurl" in url.lower():
        return False
    # Na página de empresas autenticada, o URL não tem returnurl e tem minha-via-verde ou menu de conta
    return "minha-via-verde" in url or "logout" in page.content().lower()


def scrape_movements(page, plate, date_from, date_to):
    """
    Devolve lista de {date, location, amount} para a matrícula no período.
    Estrutura confirmada: table.theme-default-table (2.ª tabela)
      célula[1] = matrícula, célula[2] = local + datetime, célula[5] = valor
    """
    plate_norm = normalize_plate(plate)

    # Navegar e fazer login se necessário
    page.goto(VV_URL, wait_until="networkidle")
    time.sleep(2)
    if not _is_logged_in(page):
        _login(page)
        page.goto(VV_URL, wait_until="networkidle")
        time.sleep(2)

    # Activar aba "Movimentos"
    try:
        page.locator("a:has-text('Movimentos')").first.click()
        page.wait_for_load_state("networkidle")
        time.sleep(2)
    except Exception:
        pass

    # Expandir painel de filtros (em headless pode estar fechado)
    page.evaluate("""
        const expBtns = Array.from(document.querySelectorAll('button.expand-button.js-toggle'));
        // Abrir o último (pertence à aba Movimentos)
        if (expBtns.length > 0) expBtns[expBtns.length - 1].click();
    """)
    time.sleep(0.5)

    # Filtro: matrícula — input.input com placeholder=" " dentro de div.tags
    try:
        _set_angular(page, 'input.input[placeholder=" "]', plate)
        time.sleep(0.3)
    except Exception:
        log("  ⚠ Não foi possível preencher o filtro de matrícula")

    # Filtro: datas — 2 datepickers dentro de div.filter-expanded-wrapper
    from_str = date_from.strftime("%d/%m/%Y")
    to_str = date_to.strftime("%d/%m/%Y")
    page.evaluate(
        """([from_s, to_s]) => {
            const dps = document.querySelectorAll(
                'div.filter-expanded-wrapper input.datepicker');
            if (dps.length < 2) return;
            function set(inp, val) {
                const s = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                s.call(inp, val);
                ['input','change','keyup'].forEach(ev =>
                    inp.dispatchEvent(new Event(ev, {bubbles: true})));
            }
            set(dps[0], from_s);
            set(dps[1], to_s);
        }""",
        [from_str, to_str],
    )
    time.sleep(0.3)

    # Botão "Filtrar" — JS click no último (evita conflito com duplicado escondido do Extratos)
    try:
        clicked = page.evaluate("""(() => {
            const btns = Array.from(document.querySelectorAll('button.button'));
            const filtrar = btns.filter(b => b.textContent.trim() === 'Filtrar');
            if (filtrar.length === 0) return false;
            filtrar[filtrar.length - 1].click();
            return true;
        })()""")
        if not clicked:
            log("  ⚠ Botão Filtrar não encontrado na página")
        page.wait_for_load_state("networkidle")
        time.sleep(2)
    except Exception as e:
        log(f"  ⚠ Erro ao clicar Filtrar: {e}")

    # Carregar todos os resultados (cap alto p/ suportar backfill de meses)
    for _ in range(120):
        try:
            btn = page.locator("button.button-border:has-text('Ver mais')")
            if not btn.is_visible(timeout=1500):
                break
            btn.click()
            page.wait_for_load_state("networkidle")
            time.sleep(1)
        except Exception:
            break

    # Extrair linhas da tabela de movimentos (2.ª tabela com class theme-default-table)
    rows_data = page.evaluate(
        """() => {
            const tables = document.querySelectorAll('table.theme-default-table');
            const t = tables[tables.length - 1];
            if (!t) return [];
            const out = [];
            t.querySelectorAll('tbody tr').forEach(tr => {
                const cells = tr.querySelectorAll('td');
                if (cells.length < 6) return;
                out.push({
                    plate:       cells[1]?.textContent.trim(),
                    description: cells[2]?.textContent.trim(),
                    value:       cells[5]?.textContent.trim(),
                });
            });
            return out;
        }"""
    )

    passages = []
    for row in rows_data:
        if normalize_plate(row.get("plate", "")) != plate_norm:
            continue

        desc = row.get("description", "")
        # Data no formato YYYY-MM-DD dentro da descrição
        m = re.search(r"(\d{4}-\d{2}-\d{2})", desc)
        if not m:
            continue
        row_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        if not (date_from <= row_date <= date_to):
            continue

        # Hora HH:MM
        hm = re.search(r"(\d{2}:\d{2})", desc)
        hora = hm.group(1) if hm else ""

        # Valor: "3,50 €" → 3.50
        vm = re.search(r"([\d]+[,.][\d]{2})", row.get("value", "0"))
        amount = float(vm.group(1).replace(",", ".")) if vm else 0.0

        # Local (texto antes do timestamp)
        location = re.sub(r"\s+\d{4}-\d{2}-\d{2}.*", "", desc).strip()

        passages.append({"date": row_date, "hora": hora, "location": location, "amount": amount})

    return passages


# ── Alerta de portagens tardias ─────────────────────────────────────────────────

def load_reservas_records(gc):
    try:
        ss = gc.open(GOOGLE_SHEET_NAME)
        return _ws_records(ss.worksheet(TAB_RESERVAS))
    except Exception as e:
        log(f"  ⚠ alerta: não consegui ler Reservas ({type(e).__name__}: {e})")
        return []


def _resolve_booking(reservas, plate, pdate):
    """Reserva a que pertence uma passagem: mesma matrícula e início ≤ data ≤ fim."""
    pn = normalize_plate(plate)
    for r in reservas:
        if normalize_plate(str(r.get("Matrícula", ""))) != pn:
            continue
        ci = parse_date(r.get("Data Início"))
        co = parse_date(r.get("Data Fim"))
        if ci and co and ci <= pdate <= co:
            return r
    return None


def _caucao_estado(app_ss, bid):
    try:
        _, row = get_caucao_row(app_ss, bid)
        return str((row or {}).get("estado", "")).strip()
    except Exception:
        return ""


def _fmt_eur(v):
    try:
        return f"{float(str(v).replace(',', '.')):.2f} €".replace(".", ",")
    except Exception:
        return f"{v} €"


def _send_ops_email(subject, html, body_text):
    if not all([GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN]):
        log("  ⚠ alerta: faltam env vars GMAIL_* — email não enviado")
        return False
    try:
        creds = UserCredentials(
            token=None, refresh_token=GMAIL_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GMAIL_CLIENT_ID, client_secret=GMAIL_CLIENT_SECRET,
            scopes=["https://www.googleapis.com/auth/gmail.send"],
        )
        creds.refresh(Request())
        svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
        msg["To"] = OPS_NOTIFICATION_EMAIL
        msg["From"] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
        msg["Subject"] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        return True
    except Exception as e:
        log(f"  ✗ alerta: falha ao enviar email ({type(e).__name__}: {e})")
        return False


def alert_late_passages(gc, app_ss, new_passages):
    """Avisa a equipa (email ops@) quando entram portagens de reservas JÁ
    TERMINADAS (checkout < hoje) — o caso em que é preciso cobrar ao cliente à
    parte, porque a caução pode já ter sido devolvida.

    Só considera passagens inseridas NESTE varrimento (cada uma alerta 1x).
    Passagens de reservas a decorrer/futuras não alertam (a equipa vê-as no
    fecho da caução); passagens sem reserva atribuível também não.
    """
    if not new_passages:
        return
    hoje = datetime.now(LISBON_TZ).date()
    reservas = load_reservas_records(gc)
    if not reservas:
        return

    items = []
    for px in new_passages:
        b = _resolve_booking(reservas, px["plate"], px["date"])
        if not b:
            continue
        co = parse_date(b.get("Data Fim"))
        if not co or co >= hoje:
            continue  # reserva a decorrer ou futura — não alerta
        bid = str(b.get("ID", "")).strip()
        items.append({
            "bid": bid,
            "hospede": f"{b.get('Hóspede Nome', '')} {b.get('Hóspede Apelido', '')}".strip(),
            "veiculo": f"{b.get('Veículo', '')} ({px['plate']})".strip(),
            "data_in": str(b.get("Data Início", "")),
            "data_out": str(b.get("Data Fim", "")),
            "px_data": px["date"].strftime("%d/%m/%Y"),
            "px_hora": px.get("hora", ""),
            "px_local": px["location"],
            "px_valor": px["amount"],
            "caucao_estado": _caucao_estado(app_ss, bid),
        })
    if not items:
        return

    by_bid = {}
    for it in items:
        by_bid.setdefault(it["bid"], []).append(it)

    n_px, n_res = len(items), len(by_bid)
    subject = (f"⚠ Via Verde: {n_px} portagem(ns) tardia(s) em {n_res} "
               f"reserva(s) já terminada(s) — verificar cobrança")

    rows_html, lines_txt, total_geral = [], [], 0.0
    for bid, its in by_bid.items():
        head = its[0]
        est = head["caucao_estado"]
        # "devolvida" (definitiva/parcial) = dinheiro já devolvido → urgente.
        # NB: "a_devolver" NÃO conta (ainda por devolver) — daí "devolvida", não "devolv".
        devolvida = "devolvida" in est.lower()
        flag = "🔴 CAUÇÃO JÁ DEVOLVIDA" if devolvida else ("🟡 caução: " + (est or "—"))
        subtot = 0.0
        for it in its:
            try:
                subtot += float(str(it["px_valor"]).replace(",", "."))
            except Exception:
                pass
        total_geral += subtot
        rows_html.append(
            "<tr><td colspan='3' style='padding:12px 8px 4px;border-top:2px solid #ddd'>"
            f"<b>#{_html_escape(bid)} · {_html_escape(head['hospede'])}</b> — "
            f"{_html_escape(head['veiculo'])}<br>"
            f"<span style='color:#666;font-size:13px'>estadia {_html_escape(head['data_in'])} → "
            f"{_html_escape(head['data_out'])} · subtotal {_fmt_eur(subtot)} · {flag}</span></td></tr>"
        )
        lines_txt.append(
            f"#{bid} · {head['hospede']} — {head['veiculo']} "
            f"(estadia {head['data_in']}->{head['data_out']}, {flag}, subtotal {_fmt_eur(subtot)})"
        )
        for it in its:
            rows_html.append(
                "<tr>"
                f"<td style='padding:4px 8px'>{_html_escape(it['px_data'])} {_html_escape(it['px_hora'])}</td>"
                f"<td style='padding:4px 8px'>{_html_escape(it['px_local'])}</td>"
                f"<td style='padding:4px 8px;text-align:right;white-space:nowrap'>{_fmt_eur(it['px_valor'])}</td>"
                "</tr>"
            )
            lines_txt.append(f"    {it['px_data']} {it['px_hora']} · {it['px_local']} · {_fmt_eur(it['px_valor'])}")

    html = (
        "<div style='font-family:Arial,sans-serif;max-width:640px;color:#222'>"
        "<h2 style='color:#b00;margin-bottom:4px'>Portagens Via Verde tardias</h2>"
        "<p>Entraram no último varrimento portagens de reservas <b>já terminadas</b>. "
        "Confirmem se é preciso <b>cobrar ao cliente</b> à parte — a caução pode já ter sido devolvida.</p>"
        f"<table style='border-collapse:collapse;width:100%;font-size:14px'><tbody>{''.join(rows_html)}</tbody></table>"
        f"<p style='margin-top:12px;font-size:15px'><b>Total das portagens tardias: {_fmt_eur(total_geral)}</b></p>"
        "<p style='color:#888;font-size:12px'>Alerta automático do robô Via Verde. "
        "Na App › Cauções, abre a reserva e carrega «Atualizar passagens Via Verde» para ver o detalhe.</p>"
        "</div>"
    )
    body_text = (
        "Portagens Via Verde tardias (reservas já terminadas):\n\n"
        + "\n".join(lines_txt)
        + f"\n\nTotal: {_fmt_eur(total_geral)}\n"
    )

    if _send_ops_email(subject, html, body_text):
        log(f"  ✉ alerta enviado a {OPS_NOTIFICATION_EMAIL}: "
            f"{n_px} portagem(ns) tardia(s) em {n_res} reserva(s)")


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    if not VV_EMAIL or not VV_PASSWORD:
        log("VV_EMAIL ou VV_PASSWORD não definidos — a saltar")
        return "skipped: no credentials"

    try:
        gc = get_gc()
        app_ss = gc.open(APP_SHEET_NAME)
    except gspread.exceptions.SpreadsheetNotFound:
        log(f"Sheet '{APP_SHEET_NAME}' não encontrada")
        return "error: app sheet not found"

    date_from, date_to = get_scrape_window()
    log(f"Varrimento: {date_from.strftime('%d/%m/%Y')} → {date_to.strftime('%d/%m/%Y')}")

    processed = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ))
        page = ctx.new_page()

        page.goto(VV_URL, wait_until="networkidle")
        time.sleep(2)
        if not _is_logged_in(page):
            try:
                _login(page)
            except Exception as e:
                log(f"✗ Login falhou: {e}")
                browser.close()
                return "error: login failed"

        all_new = []
        for plate in VEHICLES.values():
            log(f"  → {plate} | {date_from.strftime('%d/%m/%Y')}–{date_to.strftime('%d/%m/%Y')}")
            try:
                passages = scrape_movements(page, plate, date_from, date_to)
            except Exception as e:
                log(f"  ✗ Erro ao fazer scraping de {plate}: {e}")
                continue

            new_px = upsert_passages(app_ss, passages, plate)
            log(f"    {len(passages)} passagem(ns), {len(new_px)} nova(s)")
            all_new.extend(new_px)
            processed += 1

        browser.close()

    # Alerta: portagens que entraram agora mas pertencem a reservas já terminadas.
    # Desligado em modo backfill (janela larga) para não spammar ops@.
    if SCRAPE_DAYS > BACKFILL_THRESHOLD_DAYS:
        log(f"  ⓘ backfill ({SCRAPE_DAYS}d): alerta de portagens tardias DESLIGADO")
    else:
        try:
            alert_late_passages(gc, app_ss, all_new)
        except Exception as e:
            log(f"  ⚠ alerta: erro inesperado ({type(e).__name__}: {e})")

    return f"ok: {processed} matrícula(s) processada(s)"


if __name__ == "__main__":
    result = main()
    log(f"Resultado: {result}")
