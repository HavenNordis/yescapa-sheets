"""
vv_runner.py — Via Verde movement scraper

Corre diariamente (chamado por run.py às 19h30 Lisboa).
Para cada reserva com checkout hoje, ontem ou anteontem:
  1. Acede ao portal viaverde.pt (empresas)
  2. Filtra movimentos por matrícula + período
  3. Carrega todos os resultados ("Ver mais")
  4. Soma as passagens e grava em Cauções:
       vv_real_confirmado, vv_obs, vv_data_consulta

Env vars:
  VV_EMAIL              (obrigatório)
  VV_PASSWORD           (obrigatório)
  GOOGLE_CREDENTIALS_JSON
  GOOGLE_SHEET_NAME     (default: "Reservas Yescapa")
  APP_SHEET_NAME        (default: "Haven Nordis · App (Tarefas e Manutenções)")

Pré-requisito único:
  Partilhar a sheet "Haven Nordis · App (Tarefas e Manutenções)" com o service account
  yescapa@yescapa-sheets-495311.iam.gserviceaccount.com (Editor).
"""

import json
import os
import re
import time
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo

import gspread
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
    """Reservas com checkout hoje, ontem ou anteontem."""
    ss = gc.open(GOOGLE_SHEET_NAME)
    ws = ss.worksheet(TAB_RESERVAS)
    records = ws.get_all_records()

    today = datetime.now(LISBON_TZ).date()
    target_dates = {today, today - timedelta(days=1), today - timedelta(days=2)}

    targets = []
    for row in records:
        checkout = parse_date(row.get("Data Fim", ""))
        if not checkout or checkout not in target_dates:
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
            "checkout": checkout,
            "guest": str(row.get("Hóspede Nome", "")).strip(),
        })
    return targets


def get_caucao_row(app_ss, bid):
    """Devolve (worksheet, row_dict ou None)."""
    try:
        ws = app_ss.worksheet(TAB_CAUCOES)
    except gspread.exceptions.WorksheetNotFound:
        return None, None
    for row in ws.get_all_records():
        if str(row.get("booking_id", "")).strip() == str(bid).strip():
            return ws, row
    return ws, None


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

    records = ws.get_all_records()
    for i, r in enumerate(records, start=2):
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


def _login(page):
    """Preenche o modal de login se estiver visível."""
    try:
        email_inp = page.locator("dialog input[type='email']").first
        if email_inp.is_visible(timeout=4000):
            email_inp.fill(VV_EMAIL)
            page.locator("dialog input[type='password']").first.fill(VV_PASSWORD)
            page.locator("dialog button[type='submit']").first.click()
            page.wait_for_load_state("networkidle")
            time.sleep(2)
            log("  ✓ Login efectuado")
            return
    except Exception:
        pass

    # Se o modal não abriu automaticamente, tenta clicar no trigger de login
    for trigger in [
        "a:has-text('Aceder')",
        "button:has-text('Aceder')",
        "a:has-text('Login')",
        "button:has-text('Login')",
    ]:
        try:
            page.click(trigger, timeout=2000)
            time.sleep(1)
            email_inp = page.locator("dialog input[type='email']").first
            if email_inp.is_visible(timeout=3000):
                email_inp.fill(VV_EMAIL)
                page.locator("dialog input[type='password']").first.fill(VV_PASSWORD)
                page.locator("dialog button[type='submit']").first.click()
                page.wait_for_load_state("networkidle")
                time.sleep(2)
                log("  ✓ Login efectuado (via trigger)")
                return
        except Exception:
            continue

    raise RuntimeError("Não foi possível encontrar o formulário de login")


def _is_logged_in(page):
    return "logout" in page.content().lower()


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
        time.sleep(1)
    except Exception:
        pass

    # Filtro: matrícula (input[placeholder=" "] dentro de .advance-filter-wrapper)
    try:
        _set_angular(page, '.advance-filter-wrapper input[placeholder=" "]', plate)
        time.sleep(0.3)
    except Exception:
        log("  ⚠ Não foi possível preencher o filtro de matrícula")

    # Filtro: datas (2 datepickers dentro de .advance-filter-wrapper)
    from_str = date_from.strftime("%d/%m/%Y")
    to_str = date_to.strftime("%d/%m/%Y")
    page.evaluate(
        """([from_s, to_s]) => {
            const w = document.querySelector('.advance-filter-wrapper');
            if (!w) return;
            const dps = w.querySelectorAll('input.datepicker');
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

    # Botão "Filtrar" visível (o da aba Movimentos)
    try:
        page.locator("button:has-text('Filtrar'):visible").last.click()
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
            checkout = t["checkout"]

            # Saltar se VV já confirmado
            _, cau = get_caucao_row(app_ss, bid)
            if cau and str(cau.get("vv_real_confirmado", "")).strip():
                log(f"  ⏭ {bid} ({plate}): VV já confirmado ({cau['vv_real_confirmado']}€)")
                continue
            if cau is None:
                log(f"  ⏭ {bid}: sem entrada em Cauções (cria primeiro na App)")
                continue

            log(f"  → {bid} | {plate} | checkout {checkout.strftime('%d/%m/%Y')}")
            date_from = checkout
            date_to = datetime.now(LISBON_TZ).date()

            try:
                passages = scrape_movements(page, plate, date_from, date_to)
            except Exception as e:
                log(f"  ✗ Erro ao fazer scraping: {e}")
                continue

            total = sum(px["amount"] for px in passages)
            today_str = datetime.now(LISBON_TZ).strftime("%d/%m/%Y")

            if not passages:
                vv_obs = (
                    f"Consulta {today_str} | {plate} | "
                    f"{date_from.strftime('%d/%m/%Y')}–{date_to.strftime('%d/%m/%Y')}: "
                    f"sem passagens registadas"
                )
                vv_total = 0.0
            else:
                lines = [
                    f"Consulta {today_str} | {plate} | "
                    f"{date_from.strftime('%d/%m/%Y')}–{date_to.strftime('%d/%m/%Y')}"
                ]
                for px in sorted(passages, key=lambda x: x["date"]):
                    lines.append(
                        f"  {px['date'].strftime('%d/%m/%Y')} — "
                        f"{px['location']} — {px['amount']:.2f}€"
                    )
                lines.append(f"  TOTAL: {total:.2f}€")
                vv_obs = "\n".join(lines)
                vv_total = total

            write_vv(app_ss, bid, vv_total, vv_obs)
            processed += 1

        browser.close()

    return f"ok: {processed} booking(s) processed"


if __name__ == "__main__":
    result = main()
    log(f"Resultado: {result}")
