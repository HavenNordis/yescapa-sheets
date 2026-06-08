"""
whatsapp_notification.py — Envia notificação interna para ops@havennordis.com
com a mensagem WhatsApp pronta a encaminhar ao hóspede via botão wa.me.

NÃO usa Cloud API da Meta. NÃO envia diretamente ao hóspede.
O envio final é manual (1 clique no botão wa.me do email).

Trigger: cron yescapa-sheets a cada 15 min.

Lógica:
  1. Lê worksheet Reservas (read-only).
  2. Lê worksheet PreCheckIn para saber se/quando o email pré-check-in saiu.
  3. Lê worksheet WhatsApp (estado próprio).
  4. Para cada reserva confirmed onde:
        pre_check_in_estado começa com "auto_enviado_"
        e (now - timestamp_email) >= DELAY_APOS_EMAIL_MINUTES (default 120)
        e estado WhatsApp vazio
        e telefone válido
     → renderiza mensagem PT/EN
     → constrói URL wa.me
     → envia email interno HTML para OPS_NOTIFICATION_EMAIL
     → marca WhatsApp como auto_notificado_<ts>

Safeguards:
  - Worksheet WhatsApp como única source-of-truth do estado.
  - Backfill obrigatório: reservas pré-existentes → "manual_anterior".
  - Lock por linha antes do envio.
  - DRY_RUN=true desliga toda a escrita.

Env vars necessárias:
  GOOGLE_CREDENTIALS_JSON
  GMAIL_CLIENT_ID                    (reutilizado do pre_check_in_sender)
  GMAIL_CLIENT_SECRET
  GMAIL_REFRESH_TOKEN
  GOOGLE_SHEET_NAME                  (default: "Reservas Yescapa")
  WORKSHEET_NAME                     (default: "Reservas")
  PRE_CHECK_IN_WORKSHEET             (default: "PreCheckIn")
  WHATSAPP_WORKSHEET                 (default: "WhatsApp")
  OPS_NOTIFICATION_EMAIL             (default: "ops@havennordis.com")
  SENDER_EMAIL                       (default: "ops@havennordis.com")
  SENDER_NAME                        (default: "Haven Nordis")
  DELAY_APOS_EMAIL_MINUTES           (default: "120")
  DELAY_LASTMINUTE_MINUTES           (default: "30")  # reservas <24h ao check-in
  TALLY_FORM_URL_PT                  (default: "https://tally.so/r/zx2ORZ")
  TALLY_FORM_URL_EN                  (default: "https://tally.so/r/BzAOr5")
  DRY_RUN                            (default: "false")
"""

import base64
import json
import os
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
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
WHATSAPP_WORKSHEET = os.getenv("WHATSAPP_WORKSHEET", "WhatsApp")

OPS_NOTIFICATION_EMAIL = os.getenv("OPS_NOTIFICATION_EMAIL", "ops@havennordis.com")
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "ops@havennordis.com")
SENDER_NAME = os.getenv("SENDER_NAME", "Haven Nordis")

DELAY_APOS_EMAIL_MINUTES = int(os.getenv("DELAY_APOS_EMAIL_MINUTES", "120"))
DELAY_LASTMINUTE_MINUTES = int(os.getenv("DELAY_LASTMINUTE_MINUTES", "30"))
LASTMINUTE_WINDOW_HOURS = 24  # se check-in está a <24h, usa delay reduzido

TALLY_FORM_URL_PT = os.getenv("TALLY_FORM_URL_PT", "https://tally.so/r/zx2ORZ")
TALLY_FORM_URL_EN = os.getenv("TALLY_FORM_URL_EN", "https://tally.so/r/BzAOr5")

GMAIL_CLIENT_ID = os.getenv("GMAIL_CLIENT_ID")
GMAIL_CLIENT_SECRET = os.getenv("GMAIL_CLIENT_SECRET")
GMAIL_REFRESH_TOKEN = os.getenv("GMAIL_REFRESH_TOKEN")

TEMPLATES_DIR = Path(__file__).parent / "templates"

PT_COUNTRIES = {"portugal", "pt", "pt-pt", "brasil", "brazil", "br"}
SENDABLE_STATES = {"confirmed"}

# Links dos Guias de Funcionamento vêm do módulo partilhado links_config.py
# (single source of truth — partilhado com pre_check_in_sender.py).
# Agora suporta vídeos PT e EN separados (Krafie tem 2 vídeos distintos; Fjord/
# Runa/Celta também passaram a ter PT e EN separados desde 2026-05-30).
from links_config import (
    link_guia as _link_guia_para_matricula_idioma,
    GUIAS_VIDEO_ID,
)

WHATSAPP_HEADERS = [
    "booking_id", "estado", "timestamp", "telefone", "idioma", "erro",
]


# --- Logging ---

def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] [whatsapp] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H:%M:%S")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# --- Sheets ---

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


def ensure_whatsapp_worksheet(spreadsheet):
    try:
        ws = spreadsheet.worksheet(WHATSAPP_WORKSHEET)
        first_row = ws.row_values(1)
        if first_row != WHATSAPP_HEADERS:
            log(f"Headers desalinhados em '{WHATSAPP_WORKSHEET}', a corrigir.")
            ws.update("A1:F1", [WHATSAPP_HEADERS])
        return ws
    except gspread.exceptions.WorksheetNotFound:
        log(f"Worksheet '{WHATSAPP_WORKSHEET}' não existe — criando.")
        ws = spreadsheet.add_worksheet(title=WHATSAPP_WORKSHEET, rows=2000, cols=6)
        ws.update("A1:F1", [WHATSAPP_HEADERS])
        return ws


def load_state(ws, fields: list[str]) -> dict:
    rows = ws.get_all_records()
    state = {}
    for idx, row in enumerate(rows):
        bid = str(row.get("booking_id", "")).strip()
        if not bid:
            continue
        entry = {f: str(row.get(f, "")) for f in fields}
        entry["_row_index"] = idx + 2
        state[bid] = entry
    return state


def upsert_whatsapp_state(ws, state_map: dict, booking_id: str, estado: str,
                          telefone: str = "", idioma: str = "", erro: str = ""):
    booking_id = str(booking_id)
    ts = now_iso()
    row_values = [booking_id, estado, ts, telefone, idioma, erro]
    if booking_id in state_map:
        idx = state_map[booking_id]["_row_index"]
        ws.update(f"A{idx}:F{idx}", [row_values])
    else:
        ws.append_row(row_values, value_input_option="USER_ENTERED")
        state_map[booking_id] = {"_row_index": len(state_map) + 2}
    state_map[booking_id].update({
        "estado": estado, "timestamp": ts, "telefone": telefone,
        "idioma": idioma, "erro": erro,
    })


# --- Mapeamento Reservas ---

def reservas_to_booking(row: dict) -> dict:
    veiculo = str(row.get("Veículo", "")).strip()
    matricula = str(row.get("Matrícula", "")).strip()
    viatura = f"{veiculo} ({matricula})".strip() if matricula else veiculo
    return {
        "ref": str(row.get("ID", "")).strip(),
        "estado_meta": str(row.get("Estado Meta", "")).strip(),
        "nome": str(row.get("Hóspede Nome", "")).strip(),
        "apelido": str(row.get("Hóspede Apelido", "")).strip(),
        "email": str(row.get("Hóspede Email", "")).strip(),
        "telefone": str(row.get("Hóspede Telefone", "")).strip(),
        "viatura": viatura,
        "matricula": matricula,
        "data_in": str(row.get("Data Início", "")).strip(),
        "hora_in": str(row.get("Hora Início", "")).strip(),
        "data_out": str(row.get("Data Fim", "")).strip(),
        "hora_out": str(row.get("Hora Fim", "")).strip(),
        "pais_hospede": str(row.get("País", "")).strip(),
    }


def link_guia_para_viatura(matricula: str, idioma: str = "pt") -> str:
    """Devolve o URL YouTube do Guia de Funcionamento conforme (matrícula, idioma).
    Delega em links_config.link_guia (single source of truth).
    Matrícula desconhecida → fallback para vídeo padrão do idioma.
    Sem matrícula → string vazia (evita link enganador)."""
    key = (matricula or "").strip().upper()
    if not key:
        return ""
    return _link_guia_para_matricula_idioma(key, idioma)


def is_sendable(booking: dict) -> bool:
    return booking.get("estado_meta", "").lower() in SENDABLE_STATES


def detect_language(pais: str) -> str:
    return "pt" if (pais or "").strip().lower() in PT_COUNTRIES else "en"


# --- Telefone ---

def normalizar_telefone(tel: str) -> str:
    """Devolve apenas dígitos, com indicativo. '+351 96 12 34 567' → '351961234567'.
    wa.me não aceita '+' no URL."""
    if not tel:
        return ""
    tel = tel.strip()
    if tel.startswith("+"):
        return re.sub(r"\D", "", tel[1:])
    digits = re.sub(r"\D", "", tel)
    # Sem indicativo? Assume PT como fallback razoável.
    if len(digits) == 9:
        return "351" + digits
    return digits


def telefone_valido_para_wa(tel: str) -> bool:
    digits = normalizar_telefone(tel)
    return len(digits) >= 10  # indicativo + número, qualquer país


# --- Templates ---

def carregar(nome: str) -> str:
    return (TEMPLATES_DIR / nome).read_text(encoding="utf-8")


def build_form_link(booking: dict, language: str) -> str:
    base_url = TALLY_FORM_URL_EN if language == "en" else TALLY_FORM_URL_PT
    params = {
        "name": booking.get("nome", ""),
        "ref": booking.get("ref", ""),
        "vehicle": booking.get("viatura", ""),
        "date_in": booking.get("data_in", ""),
        "date_out": booking.get("data_out", ""),
    }
    params = {k: v for k, v in params.items() if v}
    if not params:
        return base_url
    return base_url + "?" + urllib.parse.urlencode(params)


def render_msg_whatsapp(booking: dict, language: str = None) -> str:
    """Renderiza a mensagem WhatsApp BILINGUE (PT + separador + EN) para o hóspede.

    A mensagem é sempre bilingue, em coerência com o email pré-check-in.
    O hóspede recebe ambas as versões; lê a que prefere e dispensa a outra.

    O parâmetro `language` é mantido por compatibilidade mas IGNORADO — fica
    aqui para não partir chamadores antigos. Pode ser removido depois de toda
    a base de código estar adaptada.
    """
    tpl = carregar("whatsapp_msg.txt")
    matricula = booking.get("matricula", "")
    ctx = {
        "nome": booking.get("nome", ""),
        "viatura": booking.get("viatura", ""),
        "data_in": booking.get("data_in", ""),
        "link_formulario_pt": build_form_link(booking, "pt"),
        "link_formulario_en": build_form_link(booking, "en"),
        "link_guia_pt": link_guia_para_viatura(matricula, "pt"),
        "link_guia_en": link_guia_para_viatura(matricula, "en"),
    }
    return Template(tpl).safe_substitute(ctx).strip()


def build_wa_link(telefone: str, mensagem: str) -> str:
    """https://wa.me/<digits>?text=<urlencoded>"""
    digits = normalizar_telefone(telefone)
    return f"https://wa.me/{digits}?text=" + urllib.parse.quote(mensagem, safe="")


def render_email_interno(booking: dict, idioma: str, msg_preview: str,
                         wa_link: str, email_enviado_em: str,
                         contacto_estado: str) -> tuple[str, str, str]:
    ctx = {
        "ref": booking.get("ref", ""),
        "nome": booking.get("nome", ""),
        "apelido": booking.get("apelido", ""),
        "viatura": booking.get("viatura", ""),
        "data_in": booking.get("data_in", ""),
        "hora_in": booking.get("hora_in", ""),
        "data_out": booking.get("data_out", ""),
        "hora_out": booking.get("hora_out", ""),
        "telefone": booking.get("telefone", ""),
        "email": booking.get("email", ""),
        "idioma": idioma.upper(),
        "mensagem_preview": msg_preview,
        "wa_link": wa_link,
        "email_enviado_em": email_enviado_em,
        "contacto_estado": contacto_estado,
    }
    subject = Template(carregar("email_interno_ops.subject")).safe_substitute(ctx).strip()
    body = Template(carregar("email_interno_ops.txt")).safe_substitute(ctx)
    html = Template(carregar("email_interno_ops.html")).safe_substitute(ctx)
    return subject, body, html


# --- Gmail ---

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


def send_email(service, to: str, subject: str, body: str, html: str):
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    msg["To"] = to
    msg["From"] = f"{SENDER_NAME} <{SENDER_EMAIL}>"
    msg["Reply-To"] = SENDER_EMAIL
    msg["Subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    return service.users().messages().send(
        userId="me", body={"raw": raw}
    ).execute()


# --- Lógica de timing ---

PRE_CHECK_IN_TIMESTAMP_RE = re.compile(r"^auto_enviado_(\d{4}-\d{2}-\d{2}_\d{2}:\d{2}:\d{2})$")


def parse_email_timestamp(estado_pre: str) -> datetime | None:
    m = PRE_CHECK_IN_TIMESTAMP_RE.match(estado_pre.strip())
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d_%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_data_checkin(data_in: str) -> datetime | None:
    for parser in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(data_in.strip(), parser).replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            continue
    return None


def passou_delay(ts_email: datetime, data_checkin: datetime | None) -> bool:
    delay_min = DELAY_APOS_EMAIL_MINUTES
    if data_checkin and (data_checkin - now_utc()) <= timedelta(hours=LASTMINUTE_WINDOW_HOURS):
        delay_min = DELAY_LASTMINUTE_MINUTES
    return (now_utc() - ts_email) >= timedelta(minutes=delay_min)


# --- Main ---

def run():
    log(f"=== whatsapp_notification (DRY_RUN={DRY_RUN}) ===")
    log(f"    Sheet: '{SHEET_NAME}', destinatário ops: {OPS_NOTIFICATION_EMAIL}")

    spreadsheet = open_spreadsheet()
    reservas_ws = spreadsheet.worksheet(RESERVAS_WORKSHEET)
    pre_ws = spreadsheet.worksheet(PRE_CHECK_IN_WORKSHEET)
    wa_ws = ensure_whatsapp_worksheet(spreadsheet)

    # Estado de contactos é opcional — só para enriquecer o email interno.
    try:
        contactos_ws = spreadsheet.worksheet(os.getenv("CONTACTS_WORKSHEET", "Contactos"))
        contactos_state = load_state(contactos_ws, ["estado", "nome_contacto"])
    except gspread.exceptions.WorksheetNotFound:
        contactos_state = {}

    rows = reservas_ws.get_all_records()
    pre_state = load_state(pre_ws, ["estado", "timestamp"])
    wa_state = load_state(wa_ws, ["estado", "timestamp", "telefone", "idioma", "erro"])

    log(f"  → {len(rows)} reservas, {len(pre_state)} estados PreCheckIn, {len(wa_state)} estados WhatsApp")

    gmail_service = None
    if not DRY_RUN:
        gmail_service = get_gmail_service()

    enviados = 0
    falhados = 0
    ignorados = 0
    a_aguardar = 0

    for row in rows:
        booking = reservas_to_booking(row)
        bid = booking["ref"]
        if not bid:
            ignorados += 1
            continue

        estado_wa = (wa_state.get(bid) or {}).get("estado", "").strip()
        if estado_wa:
            ignorados += 1
            continue

        if not is_sendable(booking):
            if not DRY_RUN:
                upsert_whatsapp_state(
                    wa_ws, wa_state, bid, "nao_notificar",
                    erro=f"estado_meta={booking['estado_meta']}",
                )
            ignorados += 1
            continue

        estado_pre = (pre_state.get(bid) or {}).get("estado", "").strip()
        ts_email = parse_email_timestamp(estado_pre)
        if not ts_email:
            # Email ainda não saiu (ou estado inválido) → esperar próxima volta
            a_aguardar += 1
            continue

        data_checkin = parse_data_checkin(booking["data_in"])
        if not passou_delay(ts_email, data_checkin):
            a_aguardar += 1
            continue

        if not telefone_valido_para_wa(booking["telefone"]):
            if not DRY_RUN:
                upsert_whatsapp_state(
                    wa_ws, wa_state, bid, f"auto_falhou_{now_iso()}",
                    telefone=booking["telefone"], erro="telefone_invalido",
                )
            falhados += 1
            continue

        # Mensagem WhatsApp é bilingue — mantemos detect_language() só para
        # informar a equipa interna no email de ops@ qual o idioma provável
        # do hóspede (útil para responderem no idioma certo).
        idioma_provavel = detect_language(booking["pais_hospede"])
        msg = render_msg_whatsapp(booking)
        wa_link = build_wa_link(booking["telefone"], msg)
        contacto = contactos_state.get(bid, {})
        contacto_estado_str = contacto.get("estado", "—") or "—"
        email_enviado_em = (pre_state.get(bid) or {}).get("timestamp", "")

        subject, body, html = render_email_interno(
            booking=booking, idioma=idioma_provavel,
            msg_preview=msg, wa_link=wa_link,
            email_enviado_em=email_enviado_em,
            contacto_estado=contacto_estado_str,
        )

        if DRY_RUN:
            log(f"  [DRY-RUN] #{bid} → ops@ (idioma provável {idioma_provavel}): {subject}")
            log(f"            wa.me → {wa_link[:80]}...")
            enviados += 1
            continue

        try:
            upsert_whatsapp_state(
                wa_ws, wa_state, bid, f"a_enviar_{now_iso()}",
                telefone=booking["telefone"], idioma=idioma_provavel,
            )
            send_email(gmail_service, OPS_NOTIFICATION_EMAIL, subject, body, html)
            upsert_whatsapp_state(
                wa_ws, wa_state, bid, f"auto_notificado_{now_iso()}",
                telefone=booking["telefone"], idioma=idioma_provavel,
            )

            log(f"  ✓ #{bid}: notificação enviada (idioma provável {idioma_provavel})")
            enviados += 1
        except Exception as e:
            tipo = type(e).__name__
            upsert_whatsapp_state(
                wa_ws, wa_state, bid, f"auto_falhou_{now_iso()}",
                telefone=booking["telefone"], idioma=idioma_provavel,
                erro=f"{tipo}: {str(e)[:200]}",
            )
            log(f"  ✗ #{bid}: falhou ({tipo}: {e})")
            falhados += 1

    log(
        f"=== Fim: notificados={enviados} falhados={falhados} "
        f"ignorados={ignorados} a_aguardar={a_aguardar} ==="
    )
    return {
        "notificados": enviados, "falhados": falhados,
        "ignorados": ignorados, "a_aguardar": a_aguardar,
    }


def main():
    return run()


if __name__ == "__main__":
    run()
