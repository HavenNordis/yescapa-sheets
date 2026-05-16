"""
Yescapa → Google Sheets — versão com bypass via tokens da API.

Modos de autenticação:
  1) Login Playwright tradicional (YESCAPA_EMAIL/PASSWORD)
  2) Bypass: usa YESCAPA_AUTH_TOKEN + YESCAPA_X_API_KEY (extraídos do browser).
     Salta o login, chama a API directamente. Não scrapa URLs de documentos
     (esses precisam de sessão HTML — preservados de runs anteriores).
"""

import json
import os
import re
from datetime import datetime, timezone

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

load_dotenv()

YESCAPA_EMAIL     = os.environ.get("YESCAPA_EMAIL", "")
YESCAPA_PASSWORD  = os.environ.get("YESCAPA_PASSWORD", "")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Reservas Yescapa")
WORKSHEET_NAME    = os.getenv("WORKSHEET_NAME", "Reservas")
LOGSHEET_NAME     = os.getenv("LOGSHEET_NAME", "Log")

YESCAPA_AUTH_TOKEN = os.environ.get("YESCAPA_AUTH_TOKEN", "")
YESCAPA_X_API_KEY  = os.environ.get("YESCAPA_X_API_KEY", "")

YESCAPA_BASE = "https://www.yescapa.pt"
API_BASE     = "https://api.jelouemoncampingcar.com"
HEADLESS     = os.getenv("HEADLESS", "false").lower() == "true"


class YescapaPlaywright:

    FETCH_STATES = [
        ("confirmed",   None),
        ("waiting",     None),
        ("todo",        None),
        ("archived",    "TO_COME"),
        ("archived",    "CANCELLED_GUEST"),
        ("archived",    "CANCELLED_OWNER"),
        ("archived",    "CANCELLED_BOTH"),
    ]

    def run(self):
        self._intercepted = {}
        self._api_counts = {}
        self._api_next = {}
        self._api_headers = {}
        self._bypass_login = bool(YESCAPA_AUTH_TOKEN and YESCAPA_X_API_KEY)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=HEADLESS, slow_mo=50)
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                locale="pt-PT",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/148.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.on("response", self._on_api_response)
            page.on("request", self._on_api_request)

            if self._bypass_login:
                print("Bypass login: a usar YESCAPA_AUTH_TOKEN + YESCAPA_X_API_KEY.")
                # Headers que imitam fielmente o Chrome 148 a partir de yescapa.pt
                # (replica o pedido real que vimos nos devtools — incluindo Sec-Fetch-*
                #  e Client Hints, importantes para passar verificacoes anti-bot)
                self._api_headers = {
                    "Authorization": YESCAPA_AUTH_TOKEN,
                    "X-Api-Key": YESCAPA_X_API_KEY,
                    "Accept": "*/*",
                    "Accept-Encoding": "gzip, deflate, br, zstd",
                    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
                    "Origin": YESCAPA_BASE,
                    "Priority": "u=1, i",
                    "Referer": f"{YESCAPA_BASE}/",
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
            else:
                self._login(page)

            for meta_state, state in self.FETCH_STATES:
                if self._bypass_login:
                    self._collect_state_api(page, meta_state, state)
                else:
                    self._collect_state(page, meta_state, state)

            summaries = list(self._intercepted.values())
            print(f"\nTotal recolhido: {len(summaries)} reservas.")
            if self._api_counts:
                print(f"Totais reportados pela API: {self._api_counts}")

            if not summaries:
                browser.close()
                raise SystemExit("Nenhuma reserva encontrada. Verifica autenticacao.")

            print(f"A recolher detalhes de {len(summaries)} reservas...")
            detailed = []
            for i, summary in enumerate(summaries, 1):
                bid = summary.get("id")
                detail = self._fetch_detail(page, bid) if bid else {}
                docs = {}
                meta = (summary.get("meta_state") or detail.get("meta_state") or "").lower()
                if bid and meta in ("confirmed",) and not self._bypass_login:
                    docs = self._fetch_documents_urls(page, bid)
                detailed.append({**summary, **detail, **docs})
                if i % 10 == 0 or i == len(summaries):
                    print(f"  {i}/{len(summaries)} processadas.")

            browser.close()

        return detailed

    def _collect_state_api(self, page, meta_state, state=None):
        key = f"{meta_state}/{state}" if state else meta_state
        params = f"meta_state={meta_state}" + (f"&state={state}" if state else "")
        print(f"\nA recolher (API) {params}...")

        page_num = 1
        total = 0
        api_total = None

        while page_num <= 50:
            url = f"{API_BASE}/v4/bookings-owner/?{params}&page={page_num}&page_size=20"
            try:
                resp = page.request.get(url, headers=self._api_headers, timeout=20_000)
            except Exception as e:
                print(f"  [{key}] erro de rede p{page_num}: {e}")
                break
            if not resp.ok:
                print(f"  [{key}] HTTP {resp.status} em p{page_num}")
                if resp.status in (401, 403):
                    raise SystemExit(
                        f"Token Yescapa rejeitado (HTTP {resp.status}). "
                        "Recapture YESCAPA_AUTH_TOKEN do browser."
                    )
                break
            try:
                data = resp.json()
            except Exception:
                print(f"  [{key}] resposta nao-JSON em p{page_num}")
                break

            if api_total is None:
                api_total = data.get("count") if isinstance(data, dict) else None
                self._api_counts[key] = api_total or 0
                print(f"    API [{key}]: count={api_total}")

            results = data if isinstance(data, list) else data.get("results", [])
            new_count = 0
            for b in results:
                bid = b.get("id")
                if bid and bid not in self._intercepted:
                    self._intercepted[bid] = b
                    new_count += 1
            total += new_count
            print(f"  [{key}] p{page_num}: +{new_count} | total={total}/{api_total or '?'}")

            if not results or new_count == 0:
                break
            if isinstance(data, dict) and not data.get("next"):
                break
            if api_total and total >= api_total:
                break
            page_num += 1

    def _collect_state(self, page, meta_state, state=None):
        key = f"{meta_state}/{state}" if state else meta_state
        params = f"meta_state={meta_state}" + (f"&state={state}" if state else "")
        url = f"{YESCAPA_BASE}/d/bookings?{params}"
        print(f"\nA recolher {params}...")
        try:
            page.goto(url, wait_until="networkidle", timeout=30_000)
            try:
                page.wait_for_response(
                    lambda r: "jelouemoncampingcar.com/v4/bookings-owner" in r.url,
                    timeout=12_000,
                )
            except Exception:
                pass
            page.wait_for_timeout(2000)
        except Exception as e:
            print(f"  [{key}] erro ao carregar: {e}")
            return

        api_total = self._api_counts.get(key, 0)
        captured = sum(
            1 for b in self._intercepted.values()
            if b.get("meta_state") == meta_state
            and (state is None or b.get("state") == state)
        )
        print(f"  [{key}] p1: {captured}/{api_total or '?'} capturadas")

        page_num = 2
        while api_total > 0 and captured < api_total and page_num <= 50:
            before = len(self._intercepted)
            clicked = page.evaluate("""() => {
                const isNext = el => {
                    const t = (el.textContent || '').trim();
                    const a = (el.getAttribute('aria-label') || '').toLowerCase();
                    const c = (el.className || '').toLowerCase();
                    return !el.disabled && el.offsetParent !== null && (
                        a.includes('next') || a.includes('suivant') ||
                        a.includes('proxim') || a.includes('prochaine') ||
                        c.includes('next') || c.includes('forward') ||
                        t === 'NEXT_ARROW'
                    );
                };
                const btn = [...document.querySelectorAll('button,a')].find(isNext);
                if (btn) { btn.click(); return true; }
                return false;
            }""")

            if not clicked:
                base = self._api_next.get(key, "")
                if base:
                    next_url = re.sub(r"([?&]page=)\d+", f"\\g<1>{page_num}", base)
                else:
                    next_url = f"{API_BASE}/v4/bookings-owner/?{params}&page={page_num}"
                resp = page.request.get(next_url, headers=self._api_headers)
                if not resp.ok:
                    break
                api_data = resp.json()
                results = api_data if isinstance(api_data, list) else api_data.get("results", [])
                for b in results:
                    bid = b.get("id")
                    if bid and bid not in self._intercepted:
                        self._intercepted[bid] = b
                new = len(self._intercepted) - before
                if new == 0:
                    break
                page_num += 1
                continue

            try:
                page.wait_for_response(
                    lambda r: "jelouemoncampingcar.com/v4/bookings-owner" in r.url,
                    timeout=10_000,
                )
            except Exception:
                pass
            page.wait_for_timeout(2000)

            new = len(self._intercepted) - before
            captured = sum(
                1 for b in self._intercepted.values()
                if b.get("meta_state") == meta_state
                and (state is None or b.get("state") == state)
            )
            if new == 0:
                break
            page_num += 1

    def _on_api_request(self, request):
        if self._api_headers:
            return
        if "jelouemoncampingcar.com/v4/bookings-owner" not in request.url:
            return
        skip = {"content-length", "connection", "host", ":method", ":path", ":scheme", ":authority"}
        self._api_headers = {k: v for k, v in request.headers.items() if k.lower() not in skip}

    def _on_api_response(self, response):
        url = response.url
        if "jelouemoncampingcar.com" not in url:
            return
        if not re.search(r"/v4/booking", url):
            return
        if re.search(r"/v\d+/booking[^?]*/\d+/", url):
            return
        try:
            data = response.json()
            if isinstance(data, dict) and "count" in data:
                ms_m = re.search(r"meta_state=([^&]+)", url)
                st_m = re.search(r"[?&]state=([^&]+)", url)
                if ms_m:
                    key = ms_m.group(1) + (f"/{st_m.group(1)}" if st_m else "")
                    self._api_counts[key] = data["count"]
                    if key not in self._api_next and re.search(r"[?&]page=\d+", url):
                        self._api_next[key] = url
            results = data if isinstance(data, list) else data.get("results", [])
            for b in results:
                bid = b.get("id")
                if bid and bid not in self._intercepted:
                    self._intercepted[bid] = b
        except Exception:
            pass

    def _login(self, page):
        print("A abrir pagina de login...")
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
            print(f"  Login OK. URL: {page.url}")
        except Exception:
            print(f"  Aviso: URL apos login: {page.url}")

        page.wait_for_timeout(2000)

    def _fetch_detail(self, page, booking_id):
        try:
            resp = page.request.get(
                f"{API_BASE}/v4/bookings-owner/{booking_id}/",
                headers=self._api_headers,
            )
            return resp.json() if resp.ok else {}
        except Exception:
            return {}

    def _fetch_documents_urls(self, page, booking_id):
        urls = {"contrato_url": "", "seguro_url": "", "fatura_url": ""}
        try:
            page.goto(
                f"{YESCAPA_BASE}/d/bookings/{booking_id}",
                wait_until="networkidle",
                timeout=30_000,
            )
            try:
                page.wait_for_selector(
                    'a[href*="/minhas-reservas/"][href*="/documentos"], '
                    'a[href*="/minhas-reservas/factura-aluguer"]',
                    timeout=8_000,
                )
            except Exception:
                pass
            page.wait_for_timeout(1500)

            extracted = page.evaluate("""() => {
                const result = { contrato: '', seguro: '', fatura: '' };
                const links = document.querySelectorAll('a[href*="/minhas-reservas/"]');
                for (const a of links) {
                    const href = a.href || a.getAttribute('href') || '';
                    if (href.includes('factura-aluguer')) {
                        result.fatura = href;
                    } else if (href.includes('/documentos/atencao')) {
                        result.seguro = href;
                    } else if (href.includes('/documentos/')) {
                        result.contrato = href;
                    }
                }
                return result;
            }""")
            urls["contrato_url"] = extracted.get("contrato", "") or ""
            urls["seguro_url"]   = extracted.get("seguro", "") or ""
            urls["fatura_url"]   = extracted.get("fatura", "") or ""
            found = sum(1 for v in urls.values() if v)
            print(f"  [{booking_id}] URLs docs: {found}/3 capturados")
        except Exception as e:
            print(f"  [{booking_id}] erro: {e}")
        return urls


def parse_booking(raw):
    guest     = raw.get("guest") or {}
    camper    = raw.get("camper") or {}
    location  = camper.get("location") or {}
    insurance = raw.get("insurance") or {}
    pickup    = raw.get("pickup") or {}
    dropoff   = raw.get("dropoff") or {}
    hour_from = raw.get("hour_from")
    hour_to   = raw.get("hour_to")

    return {
        "ID":                    raw.get("id"),
        "Estado":                raw.get("state"),
        "Estado Meta":           raw.get("meta_state"),
        "Data Inicio":           _fmt_date(raw.get("date_from")),
        "Hora Inicio":           pickup.get("time") or (f"{hour_from}:00" if hour_from is not None else ""),
        "Data Fim":              _fmt_date(raw.get("date_to")),
        "Hora Fim":              dropoff.get("time") or (f"{hour_to}:00" if hour_to is not None else ""),
        "No Dias":               raw.get("nb_days"),
        "Hospede Nome":          guest.get("first_name"),
        "Hospede Apelido":       guest.get("last_name"),
        "Hospede Email":         guest.get("email"),
        "Hospede Telefone":      guest.get("phone"),
        "Hospede Verificado":    guest.get("profile_certified"),
        "Hospede Reservas":      guest.get("bookings_as_guest"),
        "Viajantes":             raw.get("travelers"),
        "2o Condutor":           raw.get("second_driver"),
        "Veiculo":               camper.get("title"),
        "Matricula":             camper.get("registration"),
        "Cidade":                location.get("city"),
        "Morada":                location.get("street"),
        "Pais":                  location.get("country"),
        "KM Incluidos":          raw.get("total_km"),
        "Opcao KM":              raw.get("km_option"),
        "Seguro":                insurance.get("name"),
        "Cobertura Seguro":      raw.get("insurance_coverage"),
        "Preco Hospede":         raw.get("price_guest"),
        "Ganhos Proprietario":   raw.get("total_earnings"),
        "Moeda":                 raw.get("total_earnings_currency") or "EUR",
        "Caucao":                raw.get("deposit"),
        "Meio Caucao":           ", ".join(raw.get("deposit_means") or []),
        "Reserva Instantanea":   raw.get("is_instant"),
        "Profissional":          raw.get("is_professional"),
        "Confirmado Em":         _fmt_date(raw.get("confirmed_on")),
        "Paises Permitidos":     ", ".join(raw.get("countries") or []),
        "Motivo Cancelamento":   raw.get("cancel_reason"),
        "Contrato URL":          raw.get("contrato_url") or "",
        "Seguro URL":            raw.get("seguro_url") or "",
        "Fatura URL":            raw.get("fatura_url") or raw.get("bill_url") or "",
        "Contrato":              raw.get("contract_url"),
        "Fatura":                raw.get("bill_url"),
    }


class SheetsClient:
    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    def __init__(self):
        raw = os.environ.get("GOOGLE_CREDENTIALS_JSON") or ""
        if not raw:
            raise SystemExit("Variavel GOOGLE_CREDENTIALS_JSON nao definida.")
        creds = Credentials.from_service_account_info(json.loads(raw), scopes=self.SCOPES)
        self.client = gspread.authorize(creds)

    def get_or_create_worksheet(self, sheet_name, worksheet_name):
        try:
            spreadsheet = self.client.open(sheet_name)
        except gspread.SpreadsheetNotFound:
            spreadsheet = self.client.create(sheet_name)

        try:
            worksheet = spreadsheet.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=2000, cols=25)

        return worksheet

    def update_bookings(self, worksheet, bookings):
        """MERGE: preserva reservas directas (ID nao-numerico) e URLs existentes.

        - Linhas com ID numerico = Yescapa -> sobrescritas pelo novo sync
        - Linhas com ID vazio ou alfanumerico = reservas directas/manuais -> PRESERVADAS
        - URLs de documentos vazias no novo run sao preenchidas a partir do anterior
        """
        if not bookings:
            print("Nenhuma reserva da API para guardar.")
            return

        preserve_cols = ("Contrato URL", "Seguro URL", "Fatura URL")
        existing_rows = []
        try:
            existing_rows = worksheet.get_all_records()
        except Exception as e:
            print(f"  Aviso: nao foi possivel ler sheet existente ({e}).")

        existing_by_id = {}
        for row in existing_rows:
            rid = str(row.get("ID") or "").strip()
            if rid:
                existing_by_id[rid] = row

        # Reservas directas/manuais (ID nao-numerico ou vazio) -> preservar tal qual
        def _is_yescapa_id(value):
            s = str(value or "").strip()
            return bool(s) and s.isdigit()

        manual_rows = [r for r in existing_rows if not _is_yescapa_id(r.get("ID"))]
        print(f"  {len(manual_rows)} reservas directas/manuais preservadas.")

        # Merge URLs de runs anteriores nas novas bookings (so para IDs Yescapa)
        for b in bookings:
            rid = str(b.get("ID") or "").strip()
            prev = existing_by_id.get(rid)
            if not prev:
                continue
            for col in preserve_cols:
                if not (b.get(col) or "").strip() and (prev.get(col) or "").strip():
                    b[col] = prev[col]

        # Cabecalho: campos do parse_booking + qualquer coluna extra das manuais
        headers = list(bookings[0].keys())
        manual_extra = set()
        for row in manual_rows:
            for k in row.keys():
                if k not in headers:
                    manual_extra.add(k)
        headers.extend(sorted(manual_extra))

        # Linhas finais: manuais primeiro, depois Yescapa
        rows = [headers]
        for row in manual_rows:
            rows.append([str(row.get(h, "") or "") for h in headers])
        for b in bookings:
            rows.append([str(b.get(h, "") or "") for h in headers])

        worksheet.clear()
        worksheet.update(rows, "A1")
        worksheet.format(f"A1:{_col_letter(len(headers))}1", {"textFormat": {"bold": True}})
        print(f"{len(manual_rows) + len(bookings)} linhas escritas "
              f"({len(manual_rows)} manuais + {len(bookings)} Yescapa).")

    def update_log(self, sheet_name, log_sheet_name, trigger, n_bookings):
        spreadsheet = self.client.open(sheet_name)
        headers = ["Data/Hora", "Motivo", "No Reservas"]
        try:
            ws = spreadsheet.worksheet(log_sheet_name)
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=log_sheet_name, rows=1000, cols=3)
            ws.append_row(headers)
            ws.format("A1:C1", {"textFormat": {"bold": True}})

        now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M:%S UTC")
        motivo = "Email (nova reserva)" if trigger == "email" else "Agendamento"
        cell = ws.find(motivo, in_column=2)
        if cell:
            ws.update(f"A{cell.row}:C{cell.row}", [[now, motivo, n_bookings]])
        else:
            ws.append_row([now, motivo, n_bookings])
        print(f"Log: {now} | {motivo} | {n_bookings}")


def _fmt_date(value):
    if not value:
        return ""
    if isinstance(value, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(value[:19], fmt).strftime("%d/%m/%Y")
            except ValueError:
                continue
        return value
    return str(value)


def _col_letter(n):
    result = ""
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def main(trigger="scheduled"):
    print("=== Yescapa -> Google Sheets ===")
    detailed = YescapaPlaywright().run()
    print(f"Total: {len(detailed)} reservas.")

    sheets = SheetsClient()
    ws = sheets.get_or_create_worksheet(GOOGLE_SHEET_NAME, WORKSHEET_NAME)
    bookings = [parse_booking(b) for b in detailed]
    sheets.update_bookings(ws, bookings)
    sheets.update_log(GOOGLE_SHEET_NAME, LOGSHEET_NAME, trigger, len(bookings))
    print("Concluido!")


if __name__ == "__main__":
    main()
