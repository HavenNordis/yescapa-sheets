"""
Yescapa → Google Sheets
Faz login via Playwright, captura o token Bearer, depois usa requests
para chamar a API jelouemoncampingcar.com e buscar detalhes completos
de cada reserva, guardando tudo num Google Sheet.

Pré-requisitos:
  pip install playwright requests gspread google-auth python-dotenv
  playwright install chromium

Google Cloud:
  1. Criar projeto em console.cloud.google.com
  2. Ativar "Google Sheets API" e "Google Drive API"
  3. Criar Service Account → descarregar JSON → guardar como credentials.json
  4. Partilhar o Google Sheet com o email da Service Account
"""

import os
import re
import requests as req_lib
from datetime import datetime

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright, Request as PWRequest

load_dotenv()

# ---------------------------------------------------------------------------
# CONFIGURAÇÃO
# ---------------------------------------------------------------------------

YESCAPA_EMAIL    = os.environ["YESCAPA_EMAIL"]
YESCAPA_PASSWORD = os.environ["YESCAPA_PASSWORD"]
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Reservas Yescapa")
WORKSHEET_NAME    = os.getenv("WORKSHEET_NAME", "Reservas")

YESCAPA_BASE = "https://www.yescapa.pt"
API_BASE     = "https://api.jelouemoncampingcar.com"
HEADLESS     = os.getenv("HEADLESS", "false").lower() == "true"

# Estados a recolher. None = sem filtro (todos os estados).
# Se a API não suportar sem filtro, tenta cada estado individualmente.
META_STATES  = [None, "confirmed", "pending", "cancelled", "completed", "past"]

# Páginas do Yescapa que carregam reservas e disparam chamadas à API
BOOKING_PAGES = [
    "/mes-locations/",
    "/mes-locations/reservations/",
    "/owner/bookings/",
    "/proprietario/reservas/",
    "/bookings/owner/",
]

# ---------------------------------------------------------------------------
# FASE 1 — Login via Playwright e captura do token
# ---------------------------------------------------------------------------

def get_auth_token() -> str:
    """
    Faz login, navega para a página de reservas (que dispara chamadas à API)
    e captura o token por 3 vias em cascata:
      1. Header Authorization dos pedidos intercetados
      2. localStorage / sessionStorage do browser
      3. Cookie de sessão usado como Bearer
    """
    token = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, slow_mo=80)
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

        # --- Via 1: interceta TODOS os pedidos para capturar Authorization ---
        def on_request(request: PWRequest):
            nonlocal token
            if token:
                return
            if "jelouemoncampingcar.com" not in request.url:
                return
            headers = request.all_headers()
            auth = headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth
                print(f"  [via request] Token capturado: {auth[:40]}...")

        page.on("request", on_request)

        # --- Login ---
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

        # Preencher formulário
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
            "button[type='submit']", "input[type='submit']",
            "button:has-text('Entrar')", "button:has-text('Se connecter')",
        ]:
            try:
                page.click(selector, timeout=5000)
                break
            except Exception:
                pass

        # Aguardar redirect pós-login
        print("A aguardar autenticação...")
        try:
            page.wait_for_function(
                "() => !window.location.pathname.includes('conexao') && "
                "!window.location.pathname.includes('login')",
                timeout=20_000,
            )
            print(f"Login bem-sucedido. URL: {page.url}")
        except Exception:
            print(f"Aviso: URL após login: {page.url}")

        page.wait_for_timeout(2000)

        # Navegar para as páginas de reservas para disparar as chamadas à API
        print("A navegar para reservas (para forçar chamadas à API)...")
        for path in BOOKING_PAGES:
            if token:
                break
            try:
                page.goto(f"{YESCAPA_BASE}{path}", wait_until="domcontentloaded", timeout=15_000)
                page.wait_for_timeout(3000)
                print(f"  Carregada: {page.url}")
            except Exception:
                pass

        # --- Via 2: procurar token no localStorage / sessionStorage ---
        if not token:
            print("  A procurar token no localStorage/sessionStorage...")
            token = page.evaluate("""() => {
                const keys = ['token', 'access_token', 'jwt', 'auth_token',
                              'userToken', 'authToken', 'Bearer'];
                for (const store of [localStorage, sessionStorage]) {
                    for (const key of keys) {
                        const v = store.getItem(key);
                        if (v && v.length > 20) return 'Bearer ' + v;
                    }
                    // Percorrer todas as chaves do storage
                    for (let i = 0; i < store.length; i++) {
                        const k = store.key(i);
                        const v = store.getItem(k);
                        if (v && (v.startsWith('eyJ') || v.length > 100)) {
                            return 'Bearer ' + v;
                        }
                    }
                }
                return null;
            }""")
            if token:
                print(f"  [via localStorage] Token encontrado: {token[:40]}...")

        # --- Via 3: cookies do contexto → tentar usá-los como auth ---
        if not token:
            print("  A extrair cookies para usar como autenticação...")
            all_cookies = context.cookies()
            # Guardar cookies para a sessão requests (usado em fallback na API)
            token = _cookies_to_header(all_cookies)
            if token:
                print(f"  [via cookies] Sessão capturada: {token[:40]}...")

        browser.close()

    if not token:
        raise SystemExit(
            "Não foi possível capturar autenticação.\n"
            "Confirma que o login funciona manualmente no browser."
        )

    return token


def _cookies_to_header(cookies: list[dict]) -> str | None:
    """Formata cookies de sessão como string para o header Cookie."""
    relevant = {
        c["name"]: c["value"]
        for c in cookies
        if any(k in c["name"].lower() for k in ["session", "token", "auth", "jwt", "user"])
    }
    if not relevant:
        # fallback: todos os cookies do domínio yescapa/jeloue
        relevant = {
            c["name"]: c["value"]
            for c in cookies
            if "yescapa" in c.get("domain", "") or "jeloue" in c.get("domain", "")
        }
    if relevant:
        return "cookie:" + "; ".join(f"{k}={v}" for k, v in relevant.items())
    return None


# ---------------------------------------------------------------------------
# FASE 2 — Chamadas diretas à API com requests
# ---------------------------------------------------------------------------

class YescapaAPI:
    def __init__(self, auth_token: str):
        self.session = req_lib.Session()
        self.session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": YESCAPA_BASE,
            "Referer": YESCAPA_BASE,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        })
        # Suporta "Bearer <jwt>", "Token <token>", ou "cookie:<name>=<val>;..."
        if auth_token.startswith("cookie:"):
            for pair in auth_token[7:].split(";"):
                pair = pair.strip()
                if "=" in pair:
                    name, _, value = pair.partition("=")
                    self.session.cookies.set(name.strip(), value.strip())
            print("  Autenticação via cookies configurada.")
        else:
            self.session.headers["Authorization"] = auth_token
            print(f"  Autenticação via header: {auth_token[:40]}...")

    def get_all_bookings(self) -> list[dict]:
        """Recolhe todos os IDs/dados sumários de todos os estados."""
        all_bookings: dict[int, dict] = {}  # id → dados, para deduplicar

        for state in META_STATES:
            params = {"page_size": 100}
            if state:
                params["meta_state"] = state

            base_url = f"{API_BASE}/v4/bookings-owner/"
            state_label = state or "todos"
            page_num = 1

            while True:
                # Construir params: página 1 usa params completos; seguintes só mudam "page"
                current_params = {**params, "page": page_num} if page_num > 1 else params
                resp = self.session.get(base_url, params=current_params, timeout=20)
                if resp.status_code == 401:
                    print(f"  Autenticação inválida (401). Estado: {state_label}")
                    break
                if resp.status_code not in (200, 201):
                    # Estado provavelmente não existe — ignorar silenciosamente
                    break

                data = resp.json()
                results = data if isinstance(data, list) else data.get("results", [])

                for b in results:
                    bid = b.get("id")
                    if bid and bid not in all_bookings:
                        all_bookings[bid] = b

                # Paginação: "next" pode ser URL completo, número de página, ou null
                if not isinstance(data, dict):
                    break
                next_val = data.get("next")
                if not next_val:
                    break
                if isinstance(next_val, str) and next_val.startswith("http"):
                    # URL completo — extrair número de página para manter consistência
                    import urllib.parse
                    qs = urllib.parse.urlparse(next_val).query
                    qs_params = urllib.parse.parse_qs(qs)
                    page_num = int(qs_params.get("page", [page_num + 1])[0])
                else:
                    # Número de página direto (int ou string)
                    page_num = int(next_val)
                print(f"    [{state_label}] página {page_num}...")

            if all_bookings and state is None:
                # Sem filtro já trouxe tudo — não precisamos de iterar estados
                break

        print(f"Total de reservas únicas (sumário): {len(all_bookings)}")
        return list(all_bookings.values())

    def get_booking_detail(self, booking_id: int) -> dict:
        """Busca os detalhes completos de uma reserva."""
        resp = self.session.get(
            f"{API_BASE}/v4/bookings-owner/{booking_id}/",
            timeout=20,
        )
        if resp.status_code == 200:
            return resp.json()
        return {}


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
    extensions = raw.get("extensions") or {}

    date_from = raw.get("date_from", "")
    date_to   = raw.get("date_to", "")
    hour_from = raw.get("hour_from")
    hour_to   = raw.get("hour_to")

    return {
        "ID":                    raw.get("id"),
        "Estado":                raw.get("state"),
        "Estado Meta":           raw.get("meta_state"),
        "Data Início":           _fmt_date(date_from),
        "Hora Início":           pickup.get("time") or (f"{hour_from}:00" if hour_from is not None else ""),
        "Data Fim":              _fmt_date(date_to),
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
        import json
        raw = os.environ.get("GOOGLE_CREDENTIALS_JSON") or ""
        if not raw:
            raise SystemExit("Variável GOOGLE_CREDENTIALS_JSON não definida.")
        info = json.loads(raw)
        creds = Credentials.from_service_account_info(info, scopes=self.SCOPES)
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


def _nested(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _col_letter(n: int) -> str:
    """Converte número de coluna (1-based) para letra (A, B, ..., Z, AA, ...)."""
    result = ""
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    # 1. Login e captura do token
    print("=== Login Yescapa ===")
    auth_token = get_auth_token()

    # 2. Recolher lista de reservas
    print("\n=== A recolher reservas ===")
    api = YescapaAPI(auth_token)
    summary_list = api.get_all_bookings()

    if not summary_list:
        raise SystemExit("Nenhuma reserva encontrada na API.")

    # 3. Buscar detalhes de cada reserva
    print(f"\nA recolher detalhes de {len(summary_list)} reservas...")
    detailed = []
    for i, booking in enumerate(summary_list, 1):
        bid = booking.get("id")
        detail = api.get_booking_detail(bid) if bid else {}
        merged = {**booking, **detail}
        detailed.append(merged)
        if i % 10 == 0 or i == len(summary_list):
            print(f"  {i}/{len(summary_list)} reservas processadas.")

    bookings = [parse_booking(b) for b in detailed]

    # 4. Guardar no Google Sheets
    print("\n=== Google Sheets ===")
    sheets = SheetsClient()
    ws = sheets.get_or_create_worksheet(GOOGLE_SHEET_NAME, WORKSHEET_NAME)
    sheets.update_bookings(ws, bookings)

    print("\nConcluído!")


if __name__ == "__main__":
    main()
