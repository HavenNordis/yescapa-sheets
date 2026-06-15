"""
google_contacts_sync.py — Cria/atualiza contactos no Google Contacts a partir das
reservas confirmadas da folha Yescapa.

Segue o mesmo padrão de pre_check_in_sender.py:
  - Worksheet "Contactos" como única source-of-truth do estado por booking_id.
  - Backfill obrigatório: reservas pré-existentes marcadas "manual_anterior".
  - Lock por linha antes de chamar a People API.
  - DRY_RUN=true desliga toda a escrita (People API e worksheet).

Formato do nome do contacto:
    "{primeiro_e_ultimo_nome} · {nome_curto_viatura} · {dd/mm}–{dd/mm}"

Exemplos:
    "Walmyr Pena · Bharpur · 12/06–19/06"
    "Tiago Costa · CF-68-JJ · 03/08–10/08"

Env vars necessárias:
  GOOGLE_CREDENTIALS_JSON         (service account — Sheets, partilhado)
  CONTACTS_CLIENT_ID              (OAuth user creds — conta ops@)
  CONTACTS_CLIENT_SECRET
  CONTACTS_REFRESH_TOKEN
  CONTACTS_GROUP_RESOURCE_NAME    (ex.: "contactGroups/abc123" — grupo Hóspedes Yescapa)
  GOOGLE_SHEET_NAME               (default: "Reservas Yescapa")
  WORKSHEET_NAME                  (default: "Reservas")
  CONTACTS_WORKSHEET              (default: "Contactos")
  DRY_RUN                         (default: "false")
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

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
CONTACTS_WORKSHEET = os.getenv("CONTACTS_WORKSHEET", "Contactos")

CONTACTS_CLIENT_ID = os.getenv("CONTACTS_CLIENT_ID")
CONTACTS_CLIENT_SECRET = os.getenv("CONTACTS_CLIENT_SECRET")
CONTACTS_REFRESH_TOKEN = os.getenv("CONTACTS_REFRESH_TOKEN")
CONTACTS_GROUP = os.getenv("CONTACTS_GROUP_RESOURCE_NAME", "")

SENDABLE_STATES = {"confirmed"}

CONTACTS_HEADERS = [
    "booking_id", "estado", "timestamp", "nome_contacto", "resource_name", "erro",
]

# Mapa de nome próprio das autocaravanas por matrícula.
# Se não houver match, fallback para a matrícula no nome do contacto.
# Fonte: Yescapa › Meus anúncios (confirmado pela Joana em 2026-05-30).
NOMES_AUTOCARAVANAS = {
    "CH-61-GD": "Runa",
    "52-US-19": "Krafie",
    "CF-68-JJ": "Fjord",
    "CE-60-LH": "Celta",
}


# --- Logging ---

def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] [contacts] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H:%M:%S")


# --- Google Sheets (service account, partilhado) ---

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


def ensure_contacts_worksheet(spreadsheet):
    try:
        ws = spreadsheet.worksheet(CONTACTS_WORKSHEET)
        first_row = ws.row_values(1)
        if first_row != CONTACTS_HEADERS:
            log(f"Headers desalinhados em '{CONTACTS_WORKSHEET}', a corrigir.")
            ws.update("A1:F1", [CONTACTS_HEADERS])
        return ws
    except gspread.exceptions.WorksheetNotFound:
        log(f"Worksheet '{CONTACTS_WORKSHEET}' não existe — criando.")
        ws = spreadsheet.add_worksheet(title=CONTACTS_WORKSHEET, rows=2000, cols=6)
        ws.update("A1:F1", [CONTACTS_HEADERS])
        return ws


def load_state(ws) -> dict:
    rows = ws.get_all_records()
    state = {}
    for idx, row in enumerate(rows):
        bid = str(row.get("booking_id", "")).strip()
        if not bid:
            continue
        state[bid] = {
            "estado": str(row.get("estado", "")).strip(),
            "timestamp": str(row.get("timestamp", "")),
            "nome_contacto": str(row.get("nome_contacto", "")),
            "resource_name": str(row.get("resource_name", "")),
            "erro": str(row.get("erro", "")),
            "_row_index": idx + 2,
        }
    return state


def upsert_state(ws, state_map: dict, booking_id: str, estado: str,
                 nome_contacto: str = "", resource_name: str = "", erro: str = ""):
    booking_id = str(booking_id)
    ts = now_iso()
    row_values = [booking_id, estado, ts, nome_contacto, resource_name, erro]
    if booking_id in state_map:
        idx = state_map[booking_id]["_row_index"]
        ws.update(f"A{idx}:F{idx}", [row_values])
    else:
        ws.append_row(row_values, value_input_option="USER_ENTERED")
        state_map[booking_id] = {"_row_index": len(state_map) + 2}
    state_map[booking_id].update({
        "estado": estado, "timestamp": ts, "nome_contacto": nome_contacto,
        "resource_name": resource_name, "erro": erro,
    })


# --- Mapeamento Reservas → modelo interno ---

def reservas_to_booking(row: dict) -> dict:
    return {
        "ref": str(row.get("ID", "")).strip(),
        "estado_meta": str(row.get("Estado Meta", "")).strip(),
        "nome": str(row.get("Hóspede Nome", "")).strip(),
        "apelido": str(row.get("Hóspede Apelido", "")).strip(),
        "email": str(row.get("Hóspede Email", "")).strip(),
        "telefone": str(row.get("Hóspede Telefone", "")).strip(),
        "veiculo": str(row.get("Veículo", "")).strip(),
        "matricula": str(row.get("Matrícula", "")).strip(),
        "data_in": str(row.get("Data Início", "")).strip(),
        "data_out": str(row.get("Data Fim", "")).strip(),
    }


def is_sendable(booking: dict) -> bool:
    return booking.get("estado_meta", "").lower() in SENDABLE_STATES


# --- Formatação do nome do contacto ---

def nome_curto_viatura(booking: dict) -> str:
    """Devolve o nome próprio da autocaravana, ou a matrícula como fallback."""
    mat = booking.get("matricula", "").strip().upper()
    if mat and mat in NOMES_AUTOCARAVANAS:
        return NOMES_AUTOCARAVANAS[mat]
    if mat:
        return mat
    # Último recurso: extrair primeira palavra do nome do veículo
    veiculo = booking.get("veiculo", "").strip()
    return veiculo.split()[0] if veiculo else "—"


def primeiro_e_ultimo_nome(nome: str, apelido: str) -> str:
    """Devolve 'Primeiro Último' (corta nomes do meio para não ficar comprido)."""
    nome = (nome or "").strip()
    apelido = (apelido or "").strip()
    if not apelido and " " in nome:
        partes = nome.split()
        return f"{partes[0]} {partes[-1]}"
    if apelido:
        primeiro = nome.split()[0] if nome else ""
        ultimo = apelido.split()[-1] if apelido else ""
        return f"{primeiro} {ultimo}".strip()
    return nome


def formatar_data_curta(data_iso: str, mostrar_ano: bool = False) -> str:
    """Aceita 'YYYY-MM-DD' ou 'DD/MM/YYYY' e devolve 'dd/mm' (ou 'dd/mm/yy')."""
    if not data_iso:
        return ""
    fmt = "%d/%m/%y" if mostrar_ano else "%d/%m"
    for parser in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(data_iso.strip(), parser).strftime(fmt)
        except ValueError:
            continue
    # Não foi possível parsear — devolver o que recebemos para não falhar em silêncio
    return data_iso


def datas_a_cavalo_de_anos(data_in: str, data_out: str) -> bool:
    """True se as datas atravessam mudança de ano."""
    try:
        d_in = datetime.strptime(data_in.strip(), "%Y-%m-%d")
        d_out = datetime.strptime(data_out.strip(), "%Y-%m-%d")
        return d_in.year != d_out.year
    except ValueError:
        return False


def formatar_nome_contacto(booking: dict) -> str:
    """'Walmyr Pena · Bharpur · 12/06–19/06'"""
    nome = primeiro_e_ultimo_nome(booking.get("nome", ""), booking.get("apelido", ""))
    viatura = nome_curto_viatura(booking)
    mostrar_ano = datas_a_cavalo_de_anos(booking.get("data_in", ""), booking.get("data_out", ""))
    dt_in = formatar_data_curta(booking.get("data_in", ""), mostrar_ano)
    dt_out = formatar_data_curta(booking.get("data_out", ""), mostrar_ano)
    return f"{nome} · {viatura} · {dt_in}–{dt_out}"


# --- People API ---

def get_people_service():
    if not all([CONTACTS_CLIENT_ID, CONTACTS_CLIENT_SECRET, CONTACTS_REFRESH_TOKEN]):
        raise RuntimeError(
            "Faltam env vars: CONTACTS_CLIENT_ID, CONTACTS_CLIENT_SECRET, CONTACTS_REFRESH_TOKEN"
        )
    creds = UserCredentials(
        token=None,
        refresh_token=CONTACTS_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=CONTACTS_CLIENT_ID,
        client_secret=CONTACTS_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/contacts"],
    )
    creds.refresh(Request())
    return build("people", "v1", credentials=creds, cache_discovery=False)


def normalizar_telefone(tel: str) -> str:
    """Remove tudo exceto dígitos e '+' inicial. '+351 96 123 4567' → '+351961234567'."""
    if not tel:
        return ""
    tel = tel.strip()
    if tel.startswith("+"):
        return "+" + re.sub(r"\D", "", tel[1:])
    return re.sub(r"\D", "", tel)


def criar_contacto(service, booking: dict) -> str:
    """Cria contacto no Google Contacts. Devolve o resourceName (people/c...)."""
    nome_completo = formatar_nome_contacto(booking)
    payload = {
        "names": [{"givenName": nome_completo}],
        "biographies": [{
            "value": (
                f"Reserva Yescapa #{booking['ref']}\n"
                f"{booking.get('veiculo', '')} ({booking.get('matricula', '')})\n"
                f"Check-in: {booking.get('data_in', '')} · Check-out: {booking.get('data_out', '')}"
            ),
            "contentType": "TEXT_PLAIN",
        }],
    }
    tel = normalizar_telefone(booking.get("telefone", ""))
    if tel:
        payload["phoneNumbers"] = [{"value": tel}]
    if booking.get("email"):
        payload["emailAddresses"] = [{"value": booking["email"]}]
    if CONTACTS_GROUP:
        payload["memberships"] = [{
            "contactGroupMembership": {"contactGroupResourceName": CONTACTS_GROUP},
        }]

    result = service.people().createContact(body=payload).execute()
    return result.get("resourceName", "")


def atualizar_contacto(service, resource_name: str, booking: dict):
    """Atualiza um contacto existente (datas mudaram, p.ex.)."""
    # People API exige get → modificar → update com etag e updatePersonFields
    current = service.people().get(
        resourceName=resource_name,
        personFields="names,biographies,phoneNumbers,emailAddresses",
    ).execute()
    nome_completo = formatar_nome_contacto(booking)
    body = {
        "etag": current["etag"],
        "names": [{"givenName": nome_completo}],
        "biographies": [{
            "value": (
                f"Reserva Yescapa #{booking['ref']}\n"
                f"{booking.get('veiculo', '')} ({booking.get('matricula', '')})\n"
                f"Check-in: {booking.get('data_in', '')} · Check-out: {booking.get('data_out', '')}"
            ),
            "contentType": "TEXT_PLAIN",
        }],
    }
    service.people().updateContact(
        resourceName=resource_name,
        updatePersonFields="names,biographies",
        body=body,
    ).execute()


# --- Main ---

def run():
    log(f"=== google_contacts_sync (DRY_RUN={DRY_RUN}) ===")
    log(f"    Sheet: '{SHEET_NAME}' / worksheet '{RESERVAS_WORKSHEET}'")

    spreadsheet = open_spreadsheet()
    reservas_ws = spreadsheet.worksheet(RESERVAS_WORKSHEET)
    state_ws = ensure_contacts_worksheet(spreadsheet)

    log("A ler folha Reservas...")
    rows = reservas_ws.get_all_records()
    log(f"  → {len(rows)} reservas")

    state_map = load_state(state_ws)
    log(f"  → {len(state_map)} entradas de estado Contactos")

    people_service = None
    if not DRY_RUN:
        log("A inicializar People API...")
        people_service = get_people_service()

    criados = 0
    atualizados = 0
    falhados = 0
    ignorados = 0

    for row in rows:
        booking = reservas_to_booking(row)
        bid = booking["ref"]
        if not bid:
            ignorados += 1
            continue

        existing = state_map.get(str(bid))
        estado_atual = (existing or {}).get("estado", "").strip()

        # SAFEGUARD 1: só reservas confirmadas
        if not is_sendable(booking):
            if not estado_atual and not DRY_RUN:
                upsert_state(
                    state_ws, state_map, bid, "nao_criar",
                    erro=f"estado_meta={booking['estado_meta']}",
                )
            ignorados += 1
            continue

        nome_alvo = formatar_nome_contacto(booking)

        # SAFEGUARD 2: se já tem resource_name → atualizar
        if existing and existing.get("resource_name"):
            if existing.get("nome_contacto") == nome_alvo:
                ignorados += 1
                continue  # nada mudou
            if DRY_RUN:
                log(f"  [DRY-RUN] #{bid}: atualizaria '{nome_alvo}'")
                atualizados += 1
                continue
            try:
                atualizar_contacto(people_service, existing["resource_name"], booking)
                upsert_state(
                    state_ws, state_map, bid, f"auto_atualizado_{now_iso()}",
                    nome_contacto=nome_alvo, resource_name=existing["resource_name"],
                )
                log(f"  ✎ #{bid}: atualizado → '{nome_alvo}'")
                atualizados += 1
            except Exception as e:
                tipo = type(e).__name__
                upsert_state(
                    state_ws, state_map, bid,
                    f"auto_falhou_{now_iso()}",
                    nome_contacto=nome_alvo,
                    resource_name=existing.get("resource_name", ""),
                    erro=f"{tipo}: {str(e)[:200]}",
                )
                log(f"  ✗ #{bid}: falha update ({tipo}: {e})")
                falhados += 1
            continue

        # SAFEGUARD 3: só age em estado vazio (não cria duplicados)
        if estado_atual:
            ignorados += 1
            continue

        if DRY_RUN:
            log(f"  [DRY-RUN] #{bid}: criaria '{nome_alvo}'")
            criados += 1
            continue

        # SAFEGUARD 4: lock antes da chamada à API
        try:
            upsert_state(state_ws, state_map, bid, f"a_criar_{now_iso()}",
                         nome_contacto=nome_alvo)
        except Exception as e:
            log(f"  ✗ #{bid}: falha lock ({e})")
            falhados += 1
            continue

        try:
            resource_name = criar_contacto(people_service, booking)
            upsert_state(
                state_ws, state_map, bid, f"auto_criado_{now_iso()}",
                nome_contacto=nome_alvo, resource_name=resource_name,
            )
            log(f"  ✓ #{bid}: criado '{nome_alvo}' ({resource_name})")
            criados += 1
        except Exception as e:
            tipo = type(e).__name__
            upsert_state(
                state_ws, state_map, bid, f"auto_falhou_{now_iso()}",
                nome_contacto=nome_alvo, erro=f"{tipo}: {str(e)[:200]}",
            )
            log(f"  ✗ #{bid}: falha create ({tipo}: {e})")
            falhados += 1

    log(
        f"=== Fim: criados={criados} atualizados={atualizados} "
        f"falhados={falhados} ignorados={ignorados} ==="
    )
    return {
        "criados": criados, "atualizados": atualizados,
        "falhados": falhados, "ignorados": ignorados,
    }


def main():
    return run()


if __name__ == "__main__":
    run()
