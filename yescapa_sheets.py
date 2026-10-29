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
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LISBON_TZ = ZoneInfo("Europe/Lisbon")

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
LOGSHEET_NAME     = os.getenv("LOGSHEET_NAME", "Log")
# Aba MANUAL escrita pelo Painel Interno (reservas fora do Yescapa). O sync junta
# estas linhas à folha Reservas em cada run, para os automatismos as tratarem.
DIRECT_WORKSHEET  = os.getenv("DIRECT_WORKSHEET", "Reservas Diretas")

YESCAPA_BASE = "https://www.yescapa.pt"
API_BASE     = "https://api.jelouemoncampingcar.com"
HEADLESS     = os.getenv("HEADLESS", "false").lower() == "true"


# ---------------------------------------------------------------------------
# YESCAPA — tudo dentro do Playwright
# ---------------------------------------------------------------------------

class YescapaPlaywright:

    # Combinações (meta_state, state) a recolher.
    # state=None → sem filtro de sub-estado (devolve tudo do meta_state).
    # archived é dividido pelos sub-estados conhecidos para garantir paginação correcta.
    FETCH_STATES: list[tuple[str, str | None]] = [
        ("confirmed",   None),
        ("waiting",     None),
        ("todo",        None),
        ("archived",    "TO_COME"),
        ("archived",    "CANCELLED_GUEST"),
        ("archived",    "CANCELLED_OWNER"),
        ("archived",    "CANCELLED_BOTH"),
    ]

    def run(self) -> list[dict]:
        self._intercepted: dict[int, dict] = {}
        self._api_counts: dict[str, int] = {}  # chave: "meta_state" ou "meta_state/state"
        self._api_next: dict[str, str] = {}    # chave → URL base da página 1 para construir paginação
        self._api_headers: dict = {}            # headers capturados do primeiro pedido da SPA à API
        # Sinais só para a entrega independente ao Haven (não afetam a folha):
        self.archived_api_total = None          # count de meta_state=archived sem state
        self.detail_failures = 0                # detalhes que falharam nesta captura

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
            page.on("request", self._on_api_request)

            # 1. Login
            self._login(page)

            # 2. Percorrer todas as combinações com paginação
            for meta_state, state in self.FETCH_STATES:
                self._collect_state(page, meta_state, state)

            summaries = list(self._intercepted.values())
            print(f"\nTotal recolhido: {len(summaries)} reservas.")
            if self._api_counts:
                print(f"Totais reportados pela API: {self._api_counts}")

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

            # Diagnóstico só-de-leitura do total de arquivadas (envio Haven
            # DESLIGADO). Não funde resultados → a folha fica intacta. Isolado:
            # timeout próprio, nunca levanta, nunca impede o sync das planilhas.
            self._diagnostico_arquivadas(page)

            browser.close()

        return detailed

    def _collect_state(self, page, meta_state: str, state: str | None = None) -> None:
        """Carrega p1 via SPA (para apanhar count + headers) e pagina via API directa.

        Não depende de clicar em botões "next" da SPA — usa o número total de
        reservas reportado pela API e itera explicitamente page=2..N até captar tudo.
        """
        key = f"{meta_state}/{state}" if state else meta_state
        params = f"meta_state={meta_state}" + (f"&state={state}" if state else "")
        print(f"\nA recolher {key}...")

        # 1. p1 via SPA — apanha count, headers da API e os primeiros bookings
        url = f"{YESCAPA_BASE}/d/bookings?{params}"
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
            print(f"  [{key}] erro ao carregar p1: {e}")
            return

        api_total = self._api_counts.get(key, 0)
        captured = sum(
            1 for b in self._intercepted.values()
            if b.get("meta_state") == meta_state
            and (state is None or b.get("state") == state)
        )
        print(f"  [{key}] p1 via SPA: {captured}/{api_total or '?'} capturadas")

        if api_total == 0 or captured >= api_total:
            return

        # 2. Determinar page_size — preferir o da URL real da SPA; caso contrário 10
        base_url = self._api_next.get(key, "")
        ps_match = re.search(r"[?&]page_size=(\d+)", base_url)
        page_size = int(ps_match.group(1)) if ps_match else 10
        total_pages = (api_total + page_size - 1) // page_size
        print(f"  [{key}] api_total={api_total} page_size={page_size} → {total_pages} páginas no total")

        # 3. Paginar via API directa para p2..N
        for page_num in range(2, total_pages + 1):
            before = len(self._intercepted)

            if base_url and re.search(r"[?&]page=\d+", base_url):
                next_url = re.sub(r"([?&]page=)\d+", f"\\g<1>{page_num}", base_url)
            elif base_url:
                sep = "&" if "?" in base_url else "?"
                next_url = f"{base_url}{sep}page={page_num}"
            else:
                next_url = (
                    f"{API_BASE}/v4/bookings-owner/?{params}"
                    f"&page={page_num}&page_size={page_size}"
                )

            try:
                resp = page.request.get(next_url, headers=self._api_headers)
            except Exception as e:
                print(f"  [{key}] p{page_num} ERRO de rede: {e}")
                break

            if not resp.ok:
                body = ""
                try:
                    body = resp.text()[:200]
                except Exception:
                    pass
                print(f"  [{key}] p{page_num} HTTP {resp.status}: {body}")
                break

            try:
                api_data = resp.json()
            except Exception as e:
                print(f"  [{key}] p{page_num} JSON inválido: {e}")
                break

            results = api_data if isinstance(api_data, list) else api_data.get("results", [])
            for b in results:
                bid = b.get("id")
                if bid and bid not in self._intercepted:
                    self._intercepted[bid] = b

            new = len(self._intercepted) - before
            captured = sum(
                1 for b in self._intercepted.values()
                if b.get("meta_state") == meta_state
                and (state is None or b.get("state") == state)
            )
            print(f"  [{key}] p{page_num}: API devolveu {len(results)}, +{new} novos | {captured}/{api_total}")

            if captured >= api_total:
                break
            if len(results) == 0:
                # API ficou sem mais resultados antes de chegarmos ao count → parar.
                print(f"  [{key}] p{page_num} vazia — interrompo paginação (esperava mais {api_total - captured})")
                break

    def _on_api_request(self, request) -> None:
        """Captura os headers da SPA para reutilizar em fetches directos."""
        if self._api_headers:
            return
        if "jelouemoncampingcar.com/v4/bookings-owner" not in request.url:
            return
        # Ignora pseudo-headers e headers geridos pelo browser/rede
        skip = {"content-length", "connection", "host", ":method", ":path", ":scheme", ":authority"}
        self._api_headers = {k: v for k, v in request.headers.items() if k.lower() not in skip}

    def _on_api_response(self, response) -> None:
        """Intercepta respostas da API de listagem feitas pela SPA."""
        url = response.url
        if "jelouemoncampingcar.com" not in url:
            return
        # Captura qualquer endpoint de reservas (bookings-owner, bookings, etc.)
        if not re.search(r"/v4/booking", url):
            return
        # Exclui endpoints de detalhe individual (ex: /bookings-owner/123/)
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
                    # Guardar o URL real da p1 (com page= e page_size=) para derivar páginas seguintes
                    if key not in self._api_next and re.search(r"[?&]page=\d+", url):
                        self._api_next[key] = url
                    print(f"    API [{key}]: count={data['count']}")
            results = data if isinstance(data, list) else data.get("results", [])
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
        """Busca detalhes completos de uma reserva."""
        try:
            resp = page.request.get(
                f"{API_BASE}/v4/bookings-owner/{booking_id}/",
                headers=self._api_headers,
            )
            if not resp.ok:
                self.detail_failures = getattr(self, "detail_failures", 0) + 1
                return {}
            return resp.json()
        except Exception:
            self.detail_failures = getattr(self, "detail_failures", 0) + 1
            return {}

    def _diagnostico_arquivadas(self, page) -> None:
        """DIAGNÓSTICO só-de-leitura (envio Haven desligado): lê o `count` de
        meta_state=archived SEM filtro de state e compara com a soma dos 4
        subgrupos arquivados da MESMA captura (que já estão em self._api_counts).

        Garantias: não funde resultados (não toca em self._intercepted → a folha
        fica intacta); timeout próprio; nunca levanta exceção; nunca impede o
        sync das planilhas. Regista SÓ totais, instante e resultado — nunca
        headers, credenciais, respostas completas ou dados de clientes.
        """
        instante = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sub = {
            s: self._api_counts.get(f"archived/{s}", 0)
            for s in ("TO_COME", "CANCELLED_GUEST", "CANCELLED_OWNER", "CANCELLED_BOTH")
        }
        soma = sum(sub.values())
        detalhe_sub = (
            f"TO_COME={sub['TO_COME']} CANCELLED_GUEST={sub['CANCELLED_GUEST']} "
            f"CANCELLED_OWNER={sub['CANCELLED_OWNER']} CANCELLED_BOTH={sub['CANCELLED_BOTH']}"
        )
        try:
            resp = page.request.get(
                f"{API_BASE}/v4/bookings-owner/?meta_state=archived&page=1&page_size=1",
                headers=self._api_headers,
                timeout=15000,
            )
            if not resp.ok:
                print(f"[diagnostico-arquivadas] instante={instante} resultado=indisponivel "
                      f"http={resp.status} soma_subgrupos={soma} ({detalhe_sub})", flush=True)
                return
            data = resp.json()
            total = data.get("count") if isinstance(data, dict) else None
            if total is None:
                print(f"[diagnostico-arquivadas] instante={instante} resultado=sem_count "
                      f"soma_subgrupos={soma} ({detalhe_sub})", flush=True)
                return
            total = int(total)
            self.archived_api_total = total     # reutilizável pela entrega Haven
            print(f"[diagnostico-arquivadas] instante={instante} "
                  f"archived_api_total={total} soma_subgrupos={soma} "
                  f"coincide={total == soma} ({detalhe_sub})", flush=True)
        except Exception as ex:
            # Só o TIPO do erro — nunca a mensagem (pode conter URL/headers).
            print(f"[diagnostico-arquivadas] instante={instante} resultado=erro "
                  f"tipo={type(ex).__name__} soma_subgrupos={soma} ({detalhe_sub})", flush=True)


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


def direct_rows_to_bookings(records: list[dict], headers: list[str],
                            yescapa_ids: set) -> list[dict]:
    """Converte as linhas da aba 'Reservas Diretas' em reservas com os mesmos
    campos da folha Reservas.

    Saltam-se: linhas sem ID; linhas ligadas a uma reserva Yescapa (coluna
    'ID Yescapa' preenchida — aí manda a linha do Yescapa e o valor direto vai
    para 'Pagamentos Extra'); e IDs que colidam com um ID do Yescapa.
    """
    out = []
    for rec in records:
        bid = str(rec.get("ID", "") or "").strip()
        if not bid or bid in yescapa_ids:
            continue
        if str(rec.get("ID Yescapa", "") or "").strip():
            continue
        out.append({h: rec.get(h, "") for h in headers})
    return out


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

    def read_direct_bookings(self, sheet_name: str, headers: list[str],
                             yescapa_ids: set) -> list[dict]:
        """Lê a aba de reservas diretas (se existir). Falha → lista vazia, para
        nunca estragar o sync das reservas Yescapa."""
        try:
            ws = self.client.open(sheet_name).worksheet(DIRECT_WORKSHEET)
            records = ws.get_all_records(numericise_ignore=["all"])  # texto tal e qual
        except gspread.WorksheetNotFound:
            return []
        except Exception as e:
            print(f"Aviso: não consegui ler '{DIRECT_WORKSHEET}' ({e}) — sem reservas diretas.")
            return []
        return direct_rows_to_bookings(records, headers, yescapa_ids)

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

    def update_log(self, sheet_name: str, log_sheet_name: str, trigger: str, n_bookings: int):
        spreadsheet = self.client.open(sheet_name)
        headers = ["Data/Hora", "Motivo", "Nº Reservas"]
        try:
            ws = spreadsheet.worksheet(log_sheet_name)
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet(title=log_sheet_name, rows=1000, cols=3)
            ws.append_row(headers)
            ws.format("A1:C1", {"textFormat": {"bold": True}})
            print(f"Separador '{log_sheet_name}' criado.")

        now = datetime.now(LISBON_TZ).strftime("%d/%m/%Y %H:%M:%S")
        motivo = "Email (nova reserva)" if trigger == "email" else "Agendamento"

        # Procurar TODAS as linhas com este motivo (e não só a primeira),
        # para apagar duplicados que possam ter ficado de versões anteriores.
        col_b = ws.col_values(2)  # inclui o cabeçalho
        matching = [
            i + 1 for i, v in enumerate(col_b)
            if i > 0 and (v or "").strip() == motivo
        ]
        if matching:
            first = matching[0]
            ws.update(f"A{first}:C{first}", [[now, motivo, n_bookings]])
            for row_num in sorted(matching[1:], reverse=True):
                ws.delete_rows(row_num)
            if len(matching) > 1:
                print(f"Log: apagados {len(matching) - 1} duplicados de '{motivo}'.")
        else:
            ws.append_row([now, motivo, n_bookings])
        print(f"Log actualizado: {now} | {motivo} | {n_bookings} reservas")


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

def main(trigger: str = "scheduled"):
    print("=== Yescapa → Google Sheets ===")

    # 1. Login + recolha de dados (tudo dentro do browser). Instante único (ms).
    #    Apanha também SystemExit (sem reservas) para não impedir a recuperação
    #    da fila Haven; o erro é re-levantado no fim, como antes.
    yp = None
    detailed = None
    snapshot_at = None
    capture_err = None
    try:
        yp = YescapaPlaywright()
        detailed = yp.run()
        now = datetime.now(timezone.utc)
        snapshot_at = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        print(f"Total: {len(detailed)} reservas.")
    except (Exception, SystemExit) as e:
        capture_err = e

    # 2. Cópia SÓ-YESCAPA antes de qualquer acesso ao Sheets (e antes das diretas).
    yescapa_only = None
    parse_err = None
    if detailed is not None:
        try:
            yescapa_only = [parse_booking(b) for b in detailed]
        except Exception as e:
            parse_err = e
            yescapa_only = None

    # 3. Guardar no Google Sheets — escrita INALTERADA; erro preservado.
    sheets_err = None
    if yescapa_only is not None:
        try:
            print("\n=== Google Sheets ===")
            sheets = SheetsClient()
            ws = sheets.get_or_create_worksheet(GOOGLE_SHEET_NAME, WORKSHEET_NAME)
            bookings = list(yescapa_only)
            if bookings:
                diretas = sheets.read_direct_bookings(
                    GOOGLE_SHEET_NAME, list(bookings[0].keys()),
                    {str(b.get("ID", "")) for b in bookings},
                )
                if diretas:
                    print(f"+ {len(diretas)} reserva(s) direta(s) da aba '{DIRECT_WORKSHEET}'.")
                    bookings += diretas
            sheets.update_bookings(ws, bookings)
            sheets.update_log(GOOGLE_SHEET_NAME, LOGSHEET_NAME, trigger, len(bookings))
        except (Exception, SystemExit) as e:
            sheets_err = e

    # 4. Entrega independente ao Haven — DESLIGADA por defeito
    #    (só com HAVEN_INGEST_ENABLED=true). Corre sempre, mesmo que o Sheets
    #    ou a captura falhem, para recuperar pendentes. Nunca afeta a folha.
    try:
        _entregar_haven(yp if detailed is not None else None, yescapa_only, snapshot_at)
    except (Exception, SystemExit) as e:
        print(f"[haven] ignorado sem afetar as planilhas ({type(e).__name__})")

    # 5. Erros originais re-levantados DEPOIS do Haven (o coordenador vê-os).
    if sheets_err is not None:
        raise sheets_err
    if parse_err is not None:
        raise parse_err
    if capture_err is not None:
        raise capture_err
    print("\nConcluído!")


def _entregar_haven(yp, yescapa_only, snapshot_at):
    if os.getenv("HAVEN_INGEST_ENABLED", "").strip().lower() != "true":
        return  # desligado: não abre a fila, não enfileira, não envia
    import haven_sync as HS
    import haven_queue as HQ
    import haven_emitter as HE
    q = HQ.HavenQueue(HQ.queue_path())   # exige volume declarado; recusa efémero
    new_env = None
    if yp is not None:
        try:
            new_env, _erros = HE.build_capture_envelope(yp, yescapa_only, snapshot_at)
        except Exception:
            new_env = None               # isolado: nunca impede a drenagem
    HE.delivery_cycle(q, HS.send_envelope, new_env)


if __name__ == "__main__":
    main()
