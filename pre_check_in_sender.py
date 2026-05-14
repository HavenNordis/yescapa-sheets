"""
pre_check_in_sender.py — Envio automático de email de pré-check-in a hóspedes Yescapa.

Lê a worksheet "Reservas" (read-only) e a worksheet "PreCheckIn" (state tracking).
Para cada reserva ainda não tratada (estado vazio na PreCheckIn), envia email
pré-check-in via Gmail API e atualiza estado.

Safeguards anti-duplicado:
  1. Folha PreCheckIn é única source-of-truth do estado por booking_id.
  2. Backfill obrigatório: todas as reservas existentes pré-marcadas "manual_anterior".
  3. Lock por linha: estado "enviando_<timestamp>" antes do envio.
  4. Modo DRY_RUN=true para testes sem envio nem escrita.

Env vars necessárias:
  GOOGLE_CREDENTIALS_JSON   (reutilizado do yescapa_sheets.py)
  GMAIL_CLIENT_ID
  GMAIL_CLIENT_SECRET
  GMAIL_REFRESH_TOKEN
  GOOGLE_SHEET_NAME         (default: "Reservas Yescapa")
  WORKSHEET_NAME            (default: "Reservas")
  PRE_CHECK_IN_WORKSHEET    (default: "PreCheckIn")
  TALLY_FORM_URL            (default: "https://tally.so/r/zx2ORZ")
  SENDER_EMAIL              (default: "ops@havennordis.com")
  SENDER_NAME               (default: "Haven Nordis")
  DRY_RUN                   (default: "false")
"""

import base64
import json
import os
import urllib.parse
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from string import Template

import gspread
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as SACredentials
from googleapiclient.discovery import build
from dotenv import load_dotenv

load_dotenv()

# --- Configurações ---

DRY_RUN = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes")

SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Reservas Yescapa")
RESERVAS_WORKSHEET = os.getenv("WORKSHEET_NAME", "Reservas")
PRE_CHECK_IN_WORKSHEET = os.getenv("PRE_CHECK_IN_WORKSHEET", "PreCheckIn")

TALLY_FORM_URL = os.getenv("TALLY_FORM_URL", "https://tally.so/r/zx2ORZ")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "ops@havennordis.com")
SENDER_NAME = os.getenv("SENDER_NAME", "Haven Nordis")

GMAIL_CLIENT_ID = os.getenv("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET")
GMAIL_REFRESH_TOKEN = os.getenv("GMAIL_REFRESH_TOKEN")

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Países que disparam template PT (case-insensitive). Resto → EN.
PT_COUNTRIES = {
    "portugal", "pt", "pt-pt",
    "brasil", "brazil", "br",
}

# Estados meta que são "enviáveis" (reservas confirmadas/ativas).
# Outros estados → marca "nao_enviar" para nunca tentar.
SENDABLE_STATES = {"confirmed", "confirmada", "confirmado", ""}

PRE_CHECK_IN_HEADERS = [
    "booking_id", "estado", "timestamp", "email_destinatario", "idioma", "erro",
]


# --- Logging ---

def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] [pre-check-in] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H:%M:%S")


# --- Google Sheets (service account, partilha do yescapa_sheets.py) ---

def get_sheets_client() -> gspread.Client:
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON env var not set")
    info = json.loads(creds_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    creds = SACredentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def open_spreadsheet():
    return get_sheets_client().open(SHEET_NAME)


def ensure_pre_check_in_worksheet(spreadsheet):
    """Cria worksheet PreCheckIn se não existir, com headers."""
    try:
        ws = spreadsheet.worksheet(PRE_CHECK_IN_WORKSHEET)
        # Garantir headers (no-op se já lá estão)
        first_row = ws.row_values(1)
        if first_row != PRE_CHECK_IN_HEADERS:
            log(f"Headers desalinhados em '{PRE_CHECK_IN_WORKSHEET}', a corrigir.")
            ws.update("A1:F1", [PRE_CHECK_IN_HEADERS])
        return ws
    except gspread.exceptions.WorksheetNotFound:
        log(f"Worksheet '{PRE_CHECK_IN_WORKSHEET}' não existe — criando.")
        ws = spreadsheet.add_worksheet(title=PRE_CHECK_IN_WORKSHEET, rows=2000, cols=6)
        ws.update("A1:F1", [PRE_CHECK_IN_HEADERS])
        return ws


def load_state(ws) -> dict:
    """Devolve {booking_id (str): {'estado':..., 'row_index':int, ...}}."""
    rows = ws.get_all_records()
    state = {}
    for idx, row in enumerate(rows):
        bid = str(row.get("booking_id", "")).strip()
        if not bid:
            continue
        state[bid] = {
            "estado": str(row.get("estado", "")).strip(),
            "timestamp": str(row.get("timestamp", "")),
            "email": str(row.get("email_destinatario", "")),
            "idioma": str(row.get("idioma", "")),
            "erro": str(row.get("erro", "")),
            "_row_index": idx + 2,  # +1 header, +1 1-indexed
        }
    return state


def upsert_state(ws, state_map: dict, booking_id: str, estado: str,
                 email: str = "", idioma: str = "", erro: str = ""):
    """Escreve estado na worksheet PreCheckIn (insert ou update) e atualiza state_map."""
    booking_id = str(booking_id)
    ts = now_iso()
    row_values = [booking_id, estado, ts, email, idioma, erro]

    if booking_id in state_map:
        idx = state_map[booking_id]["_row_index"]
        ws.update(f"A{idx}:F{idx}", [row_values])
    else:
        ws.append_row(row_values, value_input_option="USER_ENTERED")
        # Atualiza index local para futuras escritas
        state_map[booking_id] = {"_row_index": len(state_map) + 2}

    state_map[booking_id].update({
        "estado": estado, "timestamp": ts, "email": email,
        "idioma": idioma, "erro": erro,
    })


# --- Mapeamento Reservas → modelo interno ---
#
# Headers reais da worksheet "Reservas":
#   ID; Estado; Estado Meta; Data Início; Hora Início; Data Fim; Hora Fim;
#   Nº Dias; Hóspede Nome; Hóspede Apelido; Hóspede Email; Hóspede Telefone;
#   Hóspede Verificado; Hóspede Reservas; Viajantes; 2º Condutor;
#   Veículo; Matrícula; Cidade; Morada; País; KM Incluídos; Opção KM;
#   Seguro; Cobertura Seguro; Preço Hóspede; Ganhos Proprietário; Moeda;
#   Caução; Meio Caução; Reserva Instantânea; Profissional; Confirmado Em;
#   Países Permitidos; Motivo Cancelamento; Contrato; Fatura

def reservas_to_booking(row: dict) -> dict:
    veiculo = str(row.get("Veículo", "")).strip()
    matricula = str(row.get("Matrícula", "")).strip()
    viatura = f"{veiculo} ({matricula})".strip() if matricula else veiculo

    kms_inc = str(row.get("KM Incluídos", "")).strip()
    kms_opc = str(row.get("Opção KM", "")).strip()
    kms = f"{kms_inc} km ({kms_opc})".strip(" ()") if kms_inc else kms_opc

    seguro_nome = str(row.get("Seguro", "")).strip()
    seguro_cob = str(row.get("Cobertura Seguro", "")).strip()
    seguro = f"{seguro_nome} — {seguro_cob}".strip(" —") if seguro_cob else seguro_nome

    return {
        "ref": str(row.get("ID", "")).strip(),
        "estado_meta": str(row.get("Estado Meta", "")).strip(),
        "nome": str(row.get("Hóspede Nome", "")).strip(),
        "apelido": str(row.get("Hóspede Apelido", "")).strip(),
        "email": str(row.get("Hóspede Email", "")).strip(),
        "viatura": viatura,
        "data_in": str(row.get("Data Início", "")).strip(),
        "hora_in": str(row.get("Hora Início", "")).strip(),
        "data_out": str(row.get("Data Fim", "")).strip(),
        "hora_out": str(row.get("Hora Fim", "")).strip(),
        "num_viajantes": str(row.get("Viajantes", "")).strip(),
        "paises": str(row.get("Países Permitidos", "")).strip(),
        "kms": kms,
        "seguro": seguro,
        "pais_hospede": str(row.get("País", "")).strip(),
    }


def is_valid_email(email: str) -> bool:
    return bool(email) and "@" in email and "." in email.split("@")[-1]


def is_sendable(booking: dict) -> bool:
    return booking.get("estado_meta", "").lower() in SENDABLE_STATES


def detect_language(pais: str) -> str:
    return "pt" if (pais or "").strip().lower() in PT_COUNTRIES else "en"


# --- Templates ---

def load_template(language: str) -> tuple[str, str, str]:
    subject_path = TEMPLATES_DIR / f"pre_check_in_{language}.subject"
    body_path = TEMPLATES_DIR / f"pre_check_in_{language}.txt"
    html_path = TEMPLATES_DIR / f"pre_check_in_{language}.html"
    subject = subject_path.read_text(encoding="utf-8").strip()
    body = body_path.read_text(encoding="utf-8")
    html = html_path.read_text(encoding="utf-8") if html_path.exists() else ""
    return subject, body, html


def build_form_link(booking: dict) -> str:
    params = {
        "name": booking.get("nome", ""),
        "ref": booking.get("ref", ""),
        "vehicle": booking.get("viatura", ""),
        "date_in": booking.get("data_in", ""),
        "date_out": booking.get("data_out", ""),
    }
    params = {k: v for k, v in params.items() if v}
    if not params:
        return TALLY_FORM_URL
    return TALLY_FORM_URL + "?" + urllib.parse.urlencode(params)


def render_email(language: str, booking: dict) -> tuple[str, str, str]:
    subject_tpl, body_tpl, html_tpl = load_template(language)
    ctx = {
        "nome": booking.get("nome", ""),
        "ref": booking.get("ref", ""),
        "viatura": booking.get("viatura", ""),
        "data_in": booking.get("data_in", ""),
        "hora_in": booking.get("hora_in", ""),
        "data_out": booking.get("data_out", ""),
        "hora_out": booking.get("hora_out", ""),
        "num_viajantes": booking.get("num_viajantes", ""),
        "paises": booking.get("paises", ""),
        "kms": booking.get("kms", ""),
        "seguro": booking.get("seguro", ""),
        "link_formulario": build_form_link(booking),
    }
    subject = Template(subject_tpl).safe_substitute(ctx)
    body = Template(body_tpl).safe_substitute(ctx)
    html = Template(html_tpl).safe_substitute(ctx) if html_tpl else ""
    return subject, body, html


# --- Gmail (OAuth user credentials para ops@havennordis.com) ---

def get_gmail_service():
    if not all([GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN]):
        raise RuntimeError(
            "Faltam env vars: GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN"
        )
    creds = UserCredentials(
        token=None,
        refresh_token=GMAIL_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GMAIL_CLIENT_ID,
        client_secret=GMAIL_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )
    creds.refresh(Request())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send_email(service, to: str, subject: str, body: str, html: str = ""):
    if html:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
    else:
        msg = MIMEText(body, "plain", "utf-8")
    msg["To"] = to
    msg["From"] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
    msg["Reply-To"] = SENDER_EMAIL
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()


# --- Main ---

def send_test_email():
    """Modo de teste: envia 1 email com dados fictícios para validar template HTML.

    Ativa quando env var SEND_TEST_NOW=1. Não escreve em PreCheckIn.
    Destinatário definido por TEST_RECIPIENT (default: joanamateusjorge@gmail.com).
    Idioma por TEST_LANGUAGE (default: pt). Ignora todas as Reservas.
    """
    recipient = os.getenv("TEST_RECIPIENT", "joanamateusjorge@gmail.com")
    language = os.getenv("TEST_LANGUAGE", "pt")
    log(f"=== MODO TESTE: enviar 1 email para {recipient} (lang={language}) ===")

    test_booking = {
        "ref": "TEST-9999",
        "nome": "Joana",
        "viatura": "Bürstner Lyseo Privilège T 690 G (AA-00-AA)",
        "data_in": "20/05/2026",
        "hora_in": "15:00",
        "data_out": "25/05/2026",
        "hora_out": "11:00",
        "num_viajantes": "2",
        "paises": "Portugal, Espanha",
        "kms": "1000 km (Incluídos)",
        "seguro": "All Risks — Total",
    }

    gmail_service = get_gmail_service()
    subject, body, html = render_email(language, test_booking)
    send_email(gmail_service, recipient, subject, body, html)
    log(f"=== TESTE enviado: '{subject}' a {recipient} ===")
    return {"test_sent": True, "recipient": recipient, "language": language}


def run():
    if os.getenv("SEND_TEST_NOW", "").lower() in ("true", "1", "yes"):
        return send_test_email()

    log(f"=== pre_check_in_sender (DRY_RUN={DRY_RUN}) ===")

    spreadsheet = open_spreadsheet()
    reservas_ws = spreadsheet.worksheet(RESERVAS_WORKSHEET)
    state_ws = ensure_pre_check_in_worksheet(spreadsheet)

    log("A ler folha Reservas...")
    rows = reservas_ws.get_all_records()
    log(f"  → {len(rows)} reservas")

    log("A ler estado PreCheckIn...")
    state_map = load_state(state_ws)
    log(f"  → {len(state_map)} entradas de estado")

    gmail_service = None
    if not DRY_RUN:
        log("A inicializar Gmail service...")
        gmail_service = get_gmail_service()

    enviados = 0
    falhados = 0
    ignorados = 0
    sem_email = 0

    for row in rows:
        booking = reservas_to_booking(row)
        bid = booking["ref"]

        if not bid:
            ignorados += 1
            continue

        existing = state_map.get(str(bid))
        estado_atual = (existing or {}).get("estado", "").strip()

        # SAFEGUARD 1: só age em estado vazio
        if estado_atual:
            ignorados += 1
            continue

        # SAFEGUARD 2: só reservas confirmadas
        if not is_sendable(booking):
            log(f"  · #{bid}: estado_meta '{booking['estado_meta']}' não enviável → nao_enviar")
            if not DRY_RUN:
                upsert_state(
                    state_ws, state_map, bid, "nao_enviar",
                    erro=f"estado_meta={booking['estado_meta']}",
                )
            ignorados += 1
            continue

        # SAFEGUARD 3: email válido
        if not is_valid_email(booking["email"]):
            log(f"  · #{bid}: email inválido '{booking['email']}' → auto_falhou")
            if not DRY_RUN:
                upsert_state(
                    state_ws, state_map, bid,
                    f"auto_falhou_{now_iso()}",
                    email=booking["email"], erro="email_invalido",
                )
            falhados += 1
            sem_email += 1
            continue

        idioma = detect_language(booking["pais_hospede"])
        subject, body, html = render_email(idioma, booking)

        if DRY_RUN:
            log(f"  [DRY-RUN] #{bid} → {booking['email']} (lang={idioma}): {subject}")
            enviados += 1
            continue

        # SAFEGUARD 4: lock antes de enviar
        try:
            upsert_state(
                state_ws, state_map, bid,
                f"enviando_{now_iso()}",
                email=booking["email"], idioma=idioma,
            )
        except Exception as e:
            log(f"  ✗ #{bid}: falha ao escrever lock ({e}) — a saltar")
            falhados += 1
            continue

        try:
            send_email(gmail_service, booking["email"], subject, body, html)
            upsert_state(
                state_ws, state_map, bid,
                f"auto_enviado_{now_iso()}",
                email=booking["email"], idioma=idioma,
            )
            log(f"  ✓ #{bid}: enviado a {booking['email']} ({idioma})")
            enviados += 1
        except Exception as e:
            tipo = type(e).__name__
            upsert_state(
                state_ws, state_map, bid,
                f"auto_falhou_{now_iso()}",
                email=booking["email"], idioma=idioma,
                erro=f"{tipo}: {str(e)[:200]}",
            )
            log(f"  ✗ #{bid}: falhou ({tipo}: {e})")
            falhados += 1

    log(
        f"=== Fim: enviados={enviados} falhados={falhados} "
        f"ignorados={ignorados} (sem_email={sem_email}) ==="
    )
    return {
        "enviados": enviados,
        "falhados": falhados,
        "ignorados": ignorados,
        "sem_email": sem_email,
    }


def main():
    """Entry point para ser chamado do run.py."""
    return run()


if __name__ == "__main__":
    run()
