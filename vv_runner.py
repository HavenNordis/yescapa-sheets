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

import json
import os
import re
import time
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo

import gspread
import requests
from html.parser import HTMLParser
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

LISBON_TZ = ZoneInfo("Europe/Lisbon")

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


def get_target_bookings(gc):
    """Reservas activas: janela de scraping = checkin-1 até checkout+3 dias."""
    ss = gc.open(GOOGLE_SHEET_NAME)
    ws = ss.worksheet(TAB_RESERVAS)
    records = ws.get_all_records()

    today = datetime.now(LISBON_TZ).date()

    targets = []
    for row in records:
        checkin  = parse_date(row.get("Data Início", ""))
        checkout = parse_date(row.get("Data Fim", ""))
        if not checkin or not checkout:
            continue
        # Janela de scraping: dia anterior ao checkin até 3 dias após checkout
        scrape_start = checkin - timedelta(days=1)
        scrape_end   = checkout + timedelta(days=3)
        if not (scrape_start <= today <= scrape_end):
            continue
        bid = str(row.get("ID", "")).strip()
        if not bid:
            continue
        veh_name = str(row.get("Veículo", "")).strip()
        plate = next((v for k, v in VEHICLES.items() if k in veh_name), None)
        if not plate:
            continue
        targets.append({
            "booking_id": bid,
            "plate": plate,
            "checkin":  checkin,
            "checkout": checkout,
            "date_from": scrape_start,
            "date_to":   min(today, scrape_end),
            "guest": str(row.get("Hóspede Nome", "")).strip(),
        })
    return targets


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


def upsert_passages(app_ss, bid, passages, plate):
    """Insere passagens novas em VV_Passagens; não duplica existentes."""
    ws = get_vv_pass_ws(app_ss)
    if ws is None:
        log(f"  ⚠ Tab VV_Passagens não encontrada")
        return 0

    now_str = datetime.now(LISBON_TZ).strftime("%d/%m/%Y %H:%M")
    all_vals = ws.get_all_values()
    if not all_vals:
        return 0

    headers = all_vals[0]
    def hcol(name):
        return headers.index(name) if name in headers else -1

    # Índice de chaves existentes: (bid, data, local, matricula) → row index (1-based, 2+ = dados)
    existing = {}
    for i, row in enumerate(all_vals[1:], start=2):
        key = (
            row[hcol("booking_id")] if hcol("booking_id") >= 0 else "",
            row[hcol("data")] if hcol("data") >= 0 else "",
            row[hcol("local")] if hcol("local") >= 0 else "",
            row[hcol("matricula")] if hcol("matricula") >= 0 else "",
        )
        existing[key] = i

    inserted = 0
    for px in passages:
        date_str = px["date"].strftime("%d/%m/%Y")
        key = (str(bid), date_str, px["location"], plate)

        if key not in existing:
            new_row = [""] * len(headers)
            for field, val in [
                ("booking_id", str(bid)),
                ("data", date_str),
                ("local", px["location"]),
                ("matricula", plate),
                ("valor", px["amount"]),
                ("incluir", "TRUE"),
                ("notas", ""),
                ("criado_em", now_str),
                ("atualizado_em", now_str),
            ]:
                c = hcol(field)
                if c >= 0:
                    new_row[c] = val
            ws.append_row(new_row, value_input_option="USER_ENTERED")
            inserted += 1
        else:
            # Actualizar valor (pode ter mudado) e atualizado_em
            row_idx = existing[key]
            c_val = hcol("valor")
            c_upd = hcol("atualizado_em")
            if c_val >= 0:
                ws.update_cell(row_idx, c_val + 1, px["amount"])
            if c_upd >= 0:
                ws.update_cell(row_idx, c_upd + 1, now_str)

    return inserted


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

    # Carregar todos os resultados
    for _ in range(40):
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

        # Valor: "3,50 €" → 3.50
        vm = re.search(r"([\d]+[,.][\d]{2})", row.get("value", "0"))
        amount = float(vm.group(1).replace(",", ".")) if vm else 0.0

        # Local (texto antes do timestamp)
        location = re.sub(r"\s+\d{4}-\d{2}-\d{2}.*", "", desc).strip()

        passages.append({"date": row_date, "location": location, "amount": amount})

    return passages


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    if not VV_EMAIL or not VV_PASSWORD:
        log("VV_EMAIL ou VV_PASSWORD não definidos — a saltar")
        return "skipped: no credentials"

    gc = get_gc()
    targets = get_target_bookings(gc)

    if not targets:
        log("Sem reservas com checkout nos últimos 3 dias")
        return "ok: nothing to do"

    log(f"{len(targets)} reserva(s) a verificar")

    try:
        app_ss = gc.open(APP_SHEET_NAME)
    except gspread.exceptions.SpreadsheetNotFound:
        log(f"Sheet '{APP_SHEET_NAME}' não encontrada — partilha-a com o service account")
        return "error: app sheet not found"

    processed = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = ctx.new_page()

        # Login inicial
        page.goto(VV_URL, wait_until="networkidle")
        time.sleep(2)
        if not _is_logged_in(page):
            try:
                _login(page)
            except Exception as e:
                log(f"✗ Login falhou: {e}")
                browser.close()
                return "error: login failed"

        for t in targets:
            bid = t["booking_id"]
            plate = t["plate"]
            date_from = t["date_from"]
            date_to = t["date_to"]

            # Saltar se não tem entrada em Cauções (scraper não cria entradas)
            _, cau = get_caucao_row(app_ss, bid)
            if cau is None:
                log(f"  ⏭ {bid}: sem entrada em Cauções (cria primeiro na App)")
                continue

            log(f"  → {bid} | {plate} | {date_from.strftime('%d/%m/%Y')}–{date_to.strftime('%d/%m/%Y')}")

            try:
                passages = scrape_movements(page, plate, date_from, date_to)
            except Exception as e:
                log(f"  ✗ Erro ao fazer scraping: {e}")
                continue

            inserted = upsert_passages(app_ss, bid, passages, plate)
            log(f"    {len(passages)} passagem(ns) encontrada(s), {inserted} nova(s)")
            recalc_vv_total(app_ss, bid)
            processed += 1

        browser.close()

    return f"ok: {processed} booking(s) processed"


if __name__ == "__main__":
    result = main()
    log(f"Resultado: {result}")
