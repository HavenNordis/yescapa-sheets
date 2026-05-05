"""
Yescapa → Google Sheets
Faz login via Playwright e usa page.evaluate(fetch()) para chamar a API
diretamente dentro do browser — sem replicar autenticação externamente.

Pré-requisitos:
  pip install playwright gspread google-auth python-dotenv
  playwright install chromium

Google Cloud:
  1. Criar projeto em console.cloud.google.com
  2. Ativar "Google Sheets API" e "Google Drive API"
  3. Criar Service Account → conteúdo JSON em GOOGLE_CREDENTIALS_JSON
  4. Partilhar o Google Sheet com o email da Service Account
"""

import json
import os
from datetime import datetime

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

load_dotenv()

# ---------------------------------------------------------------------------
# CONFIGURAÇÃO
# ---------------------------------------------------------------------------

YESCAPA_EMAIL     = os.environ["YESCAPA_EMAIL"]
YESCAPA_PASSWORD  = os.environ["YESCAPA_PASSWORD"]
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Reservas Yescapa")
WORKSHEET_NAME    = os.getenv("WORKSHEET_NAME", "Reservas")

YESCAPA_BASE = "https://www.yescapa.pt"
API_BASE     = "https://api.jelouemoncampingcar.com"
HEADLESS     = os.getenv("HEADLESS", "false").lower() == "true"


# ---------------------------------------------------------------------------
# YESCAPA — tudo dentro do Playwright
# ---------------------------------------------------------------------------

class YescapaPlaywright:

    # Estados reais da API Yescapa
    META_STATES = ["confirmed", "waiting", "todo", "cancelled", "archived"]

    def run(self) -> list[dict]:
        self._intercepted: dict[int, dict] = {}

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
            page.on("response", self._on_api_response)

            # 1. Login
            self._login(page)

            # 2. Navegar para cada estado separadamente e intercetar respostas
            for state in self.META_STATES:
                self._load_state(page, state)

            summaries = list(self._intercepted.values())
            print(f"\nTotal recolhido: {len(summaries)} reservas.")

            if not summaries:
                browser.close()
                raise SystemExit("Nenhuma reserva encontrada. Verifica se o login funcionou.")

            # 3. Buscar detalhes de cada reserva
            print(f"A recolher detalhes de {len(summaries)} reservas...")
            detailed = []
            for i, summary in enumerate(summaries, 1):
                bid = summary.get("id")
                detail = self._fetch_detail(page, bid) if bid else {}
                detailed.append({**summary, **detail})
                if i % 10 == 0 or i == len(summaries):
                    print(f"  {i}/{len(summaries)} processadas.")

            browser.close()

        return detailed

    def _load_state(self, page, state: str) -> None:
        """Navega para a página do estado, aguarda a resposta da API e captura todas as páginas."""
        url = f"{YESCAPA_BASE}/d/bookings?meta_state={state}"
        print(f"\nA carregar estado: {state}")

        before = len(self._intercepted)

        page.goto(url, wait_until="networkidle", timeout=30_000)

        # Aguardar explicitamente a resposta da API para este estado
        try:
            page.wait_for_response(
                lambda r: (
                    "jelouemoncampingcar.com/v4/bookings-owner" in r.url
                    and f"meta_state={state}" in r.url
                ),
                timeout=15_000,
            )
        except Exception:
            pass  # Pode não haver reservas neste estado

        page.wait_for_timeout(2000)

        after = len(self._intercepted)
        found = after - before
        print(f"  [{state}] {found} reservas intercetadas.")

        # Se a página carregou mais resultados que os intercetados (paginação),
        # buscar páginas adicionais navegando pelo URL da API directamente
        page_num = 2
        while True:
            api_url = (
                f"{API_BASE}/v4/bookings-owner/"
                f"?meta_state={state}&page={page_num}&page_size=100"
            )
            before_page = len(self._intercepted)
            page.goto(api_url, wait_until="domcontentloaded", timeout=15_000)
            page.wait_for_timeout(1000)
            after_page = len(self._intercepted)
            new = after_page - before_page
            if new == 0:
                break
            print(f"  [{state}] página {page_num}: {new} reservas.")
            page_num += 1

        # Voltar à página do Yescapa para manter a sessão activa
        page.goto(f"{YESCAPA_BASE}/d/bookings", wait_until="domcontentloaded", timeout=15_000)
        page.wait_for_timeout(1000)

    def _on_api_response(self, response) -> None:
        """Captura respostas da API de listagem durante a navegação."""
        import re
        url = response.url
        if "jelouemoncampingcar.com/v4/bookings-owner" not in url:
            return
        if re.search(r"/bookings-owner/\d+/", url):
            return
        try:
            data = response.json()
            results = (
                data if isinstance(data, list)
                else data.get("results", [])
            )
            for b in results:
                bid = b.get("id")
                if bid and bid not in self._intercepted:
                    self._intercepted[bid] = b
        except Exception:
            pass

    def _login(self, page):
        print("A abrir página de login...")
        page.goto(f"{YESCAPA_BASE}/conexao/", wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1500)

        # Aceitar cookies
        for selector in [
            "#axeptio_btn_acceptAll", "#onetrust-accept-btn-handler",
            "button:has-text('Aceitar tudo')", "button:has-text('Aceitar')",
            "button:has-text('Accept all')",
        ]:
            try:
                page.click(selector, timeout=3000)
                print("  Cookies aceites.")
                break
            except Exception:
                pass

        # Email
        for selector in ["input[type='email']", "input[name='email']", "#id_email", "#email"]:
            try:
                page.fill(selector, YESCAPA_EMAIL, timeout=5000)
                break
            except Exception:
                pass

        # Password
        for selector in ["input[type='password']", "input[name='password']", "#id_password", "#password"]:
            try:
                page.fill(selector, YESCAPA_PASSWORD, timeout=5000)
                break
            except Exception:
                pass

        # Submeter
        for selector in [
            "button[type='submit']", "input[type='submit']",
            "button:has-text('Entrar')", "button:has-text('Se connecter')",
        ]:
            try:
                page.click(selector, timeout=5000)
                break
            except Exception:
                pass

        # Aguardar redirect
        print("A aguardar autenticação...")
        try:
            page.wait_for_function(
                "() => !window.location.pathname.includes('conexao') && "
                "!window.location.pathname.includes('login')",
                timeout=20_000,
            )
            print(f"  Login bem-sucedido. URL: {page.url}")
        except Exception:
            print(f"  Aviso: URL após login: {page.url}")

        page.wait_for_timeout(2000)

    def _fetch_detail(self, page, booking_id: int) -> dict:
        """Busca detalhes completos de uma reserva dentro do contexto autenticado."""
        result = page.evaluate(
            """([apiBase, bid]) => fetch(
                `${apiBase}/v4/bookings-owner/${bid}/`,
                { credentials: 'include' }
            ).then(r => r.ok ? r.json() : {})
            """,
            [API_BASE, booking_id],
        )
        return result or {}


# ---------------------------------------------------------------------------
# MAPEAMENTO DE CAMPOS
# ---------------------------------------------------------------------------

def parse_booking(raw: dict) -> dict:
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
        "Data Início":           _fmt_date(raw.get("date_from")),
        "Hora Início":           pickup.get("time") or (f"{hour_from}:00" if hour_from is not None else ""),
        "Data Fim":              _fmt_date(raw.get("date_to")),
        "Hora Fim":              dropoff.get("time") or (f"{hour_to}:00" if hour_to is not None else ""),
        "Nº Dias":               raw.get("nb_days"),
        "Hóspede Nome":          guest.get("first_name"),
        "Hóspede Apelido":       guest.get("last_name"),
        "Hóspede Email":         guest.get("email"),
        "Hóspede Telefone":      guest.get("phone"),
        "Hóspede Verificado":    guest.get("profile_certified"),
        "Hóspede Reservas":      guest.get("bookings_as_guest"),
        "Viajantes":             raw.get("travelers"),
        "2º Condutor":           raw.get("second_driver"),
        "Veículo":               camper.get("title"),
        "Matrícula":             camper.get("registration"),
        "Cidade":                location.get("city"),
        "Morada":                location.get("street"),
        "País":                  location.get("country"),
        "KM Incluídos":          raw.get("total_km"),
        "Opção KM":              raw.get("km_option"),
        "Seguro":                insurance.get("name"),
        "Cobertura Seguro":      raw.get("insurance_coverage"),
        "Preço Hóspede":         raw.get("price_guest"),
        "Ganhos Proprietário":   raw.get("total_earnings"),
        "Moeda":                 raw.get("total_earnings_currency") or "EUR",
        "Caução":                raw.get("deposit"),
        "Meio Caução":           ", ".join(raw.get("deposit_means") or []),
        "Reserva Instantânea":   raw.get("is_instant"),
        "Profissional":          raw.get("is_professional"),
        "Confirmado Em":         _fmt_date(raw.get("confirmed_on")),
        "Países Permitidos":     ", ".join(raw.get("countries") or []),
        "Motivo Cancelamento":   raw.get("cancel_reason"),
        "Contrato":              raw.get("contract_url"),
        "Fatura":                raw.get("bill_url"),
    }


# ---------------------------------------------------------------------------
# GOOGLE SHEETS
# ---------------------------------------------------------------------------

class SheetsClient:
    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    def __init__(self):
        raw = os.environ.get("GOOGLE_CREDENTIALS_JSON") or ""
        if not raw:
            raise SystemExit("Variável GOOGLE_CREDENTIALS_JSON não definida.")
        creds = Credentials.from_service_account_info(json.loads(raw), scopes=self.SCOPES)
        self.client = gspread.authorize(creds)

    def get_or_create_worksheet(self, sheet_name: str, worksheet_name: str):
        try:
            spreadsheet = self.client.open(sheet_name)
        except gspread.SpreadsheetNotFound:
            spreadsheet = self.client.create(sheet_name)
            print(f"Spreadsheet '{sheet_name}' criada.")

        try:
            worksheet = spreadsheet.worksheet(worksheet_name)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title=worksheet_name, rows=2000, cols=25)
            print(f"Separador '{worksheet_name}' criado.")

        return worksheet

    def update_bookings(self, worksheet, bookings: list[dict]):
        if not bookings:
            print("Nenhuma reserva para guardar.")
            return

        headers = list(bookings[0].keys())
        rows = [headers] + [[str(b.get(h, "") or "") for h in headers] for b in bookings]

        worksheet.clear()
        worksheet.update(rows, "A1")
        worksheet.format(f"A1:{_col_letter(len(headers))}1", {"textFormat": {"bold": True}})
        print(f"{len(bookings)} reservas guardadas no Google Sheets.")


# ---------------------------------------------------------------------------
# UTILITÁRIOS
# ---------------------------------------------------------------------------

def _fmt_date(value) -> str:
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


def _col_letter(n: int) -> str:
    result = ""
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print("=== Yescapa → Google Sheets ===")

    # 1. Login + recolha de dados (tudo dentro do browser)
    detailed = YescapaPlaywright().run()
    print(f"Total: {len(detailed)} reservas.")

    # 2. Guardar no Google Sheets
    print("\n=== Google Sheets ===")
    sheets = SheetsClient()
    ws = sheets.get_or_create_worksheet(GOOGLE_SHEET_NAME, WORKSHEET_NAME)
    sheets.update_bookings(ws, [parse_booking(b) for b in detailed])

    print("\nConcluído!")


if __name__ == "__main__":
    main()
