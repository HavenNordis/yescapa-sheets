"""
checklist_runner.py — Gera automaticamente a checklist de preparação quando um
hóspede responde ao formulário Tally.

Fluxo (corre no cron, a seguir ao sync):
  1. Vai buscar as respostas novas ao Tally (API).
  2. Para cada resposta ainda sem checklist, cruza-a com a reserva na folha
     "Reservas" pelo campo `ref` (roadmap D2.2).
  3. Constrói os dados da checklist (resposta Tally + dados da reserva).
  4. Gera o PDF de 2 páginas com o checklist_generator.
  5. Faz upload para a subpasta da reserva no Drive (a mesma do drive_archiver).
  6. Regista o estado na folha "Checklists" (anti-duplicado).

Não envia email — só guarda no Drive (roadmap D2.4).

⚠️  CALIBRAÇÃO PENDENTE: o mapeamento resposta-Tally → dados-da-checklist
(função tally_to_checklist_data) usa correspondência por palavras-chave no
título das perguntas. Os títulos exatos do formulário publicado têm de ser
confirmados contra uma submissão real — ver as constantes KW_* abaixo.

Env vars:
  TALLY_API_KEY              (obrigatória — gerar em tally.so → Settings → API)
  TALLY_FORM_ID_PT           (default: "zx2ORZ")
  TALLY_FORM_ID_EN           (default: "BzAOr5")
  CHECKLISTS_WORKSHEET       (default: "Checklists")
  CHECKLISTS_MAX_PER_RUN     (default: "15")
  GOOGLE_CREDENTIALS_JSON / GOOGLE_SHEET_NAME / WORKSHEET_NAME (do sync)
  DRIVE_DOCS_ROOT_FOLDER_ID  (a mesma raiz do drive_archiver)
"""

import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

import gspread
from dotenv import load_dotenv

from checklist_generator import generate_checklist_pdf
from drive_archiver import (
    get_google_clients, find_or_create_folder, find_file, upload_pdf,
    booking_folder_name, full_guest_name, folder_url, file_url,
    DRIVE_DOCS_ROOT_FOLDER_ID,
)

load_dotenv()

# --- Configuração ---------------------------------------------------------

TALLY_API_KEY = os.getenv("TALLY_API_KEY", "").strip()
TALLY_FORM_IDS = [
    os.getenv("TALLY_FORM_ID_PT", "zx2ORZ").strip(),
    os.getenv("TALLY_FORM_ID_EN", "BzAOr5").strip(),
]
TALLY_API_BASE = "https://api.tally.so"

GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Reservas Yescapa")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Reservas")
CHECKLISTS_WORKSHEET = os.getenv("CHECKLISTS_WORKSHEET", "Checklists")
CHECKLISTS_MAX_PER_RUN = int(os.getenv("CHECKLISTS_MAX_PER_RUN", "15"))

CHECKLISTS_HEADERS = [
    "tally_submission_id", "booking_id", "estado", "timestamp", "pdf_drive", "erro",
]
ESTADOS_TERMINAIS = {"gerado", "sem_reserva"}

# Palavras-chave para casar o título de cada pergunta Tally com o campo interno.
# ⚠️ Confirmar contra uma submissão real — ver checklist_template_design.md.
KW_KIT_CONFORTO = ("roupa de cama", "kit conforto")
KW_CAMA_CABINE = ("cabine", "capucine")
KW_BELICHES = ("beliche",)
KW_SALA_GRANDE = ("sala grande",)
KW_SALA_PEQUENA = ("sala pequena",)
KW_MICROONDAS = ("micro-onda", "microonda", "micro onda")
KW_MAQUINA_CAFE = ("máquina de café", "maquina de cafe", "café")
KW_MESA_EXTERIOR = ("mesa exterior", "mesa")
KW_CADEIRAS = ("cadeira", "número de cadeira")
KW_BANCOS = ("banco",)
KW_VIA_VERDE = ("via verde",)
KW_CADEIRA_AUTO = ("cadeirinha", "cadeira de criança", "cadeira auto", "bebé")
KW_PEDIDOS = ("pedido", "indicaç", "observa", "especia")
KW_REF = ("ref", "referência", "referencia")


# --- Logging --------------------------------------------------------------

def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] [checklist] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d_%H:%M:%S")


# --- Funções puras (testáveis) --------------------------------------------

def _norm(s) -> str:
    return str(s or "").strip().lower()


def _is_sim(value) -> bool:
    """True se a resposta a uma pergunta Sim/Não for afirmativa."""
    v = _norm(value)
    return v.startswith("s") or v.startswith("y") or "sim" in v


def answer_for(answers: dict, keywords) -> str:
    """Procura a resposta cuja pergunta contém alguma das palavras-chave.

    `answers` é {titulo_normalizado: resposta_texto}.
    """
    for titulo, resposta in answers.items():
        if any(kw in titulo for kw in keywords):
            return resposta
    return ""


def sala_grande_extensores(resposta: str) -> int:
    """Cama da sala grande: opção A → 1 extensor (meio); B → 2 (laterais)."""
    v = _norm(resposta)
    if v.startswith("b") or "2" in v or "laterais" in v:
        return 2
    return 1


def tally_to_checklist_data(answers: dict, booking: dict) -> dict:
    """Cruza a resposta Tally com a reserva e devolve o dict para o gerador.

    `answers`: {titulo_pergunta_normalizado: resposta}.
    `booking`: linha da folha Reservas (dict).
    """
    veiculo = str(booking.get("Veículo", "") or "").strip()
    matricula = str(booking.get("Matrícula", "") or "").strip()
    viatura = f"{veiculo} ({matricula})".strip() if matricula else (veiculo or matricula)

    data_in = str(booking.get("Data Início", "") or "").strip()
    hora_in = str(booking.get("Hora Início", "") or "").strip()
    data_out = str(booking.get("Data Fim", "") or "").strip()
    hora_out = str(booking.get("Hora Fim", "") or "").strip()

    resp_sala_grande = answer_for(answers, KW_SALA_GRANDE)
    resp_kit = answer_for(answers, KW_KIT_CONFORTO)
    pedidos = answer_for(answers, KW_PEDIDOS)

    return {
        "cliente_nome": full_guest_name(booking),
        "reserva_ref": str(booking.get("ID", "") or "").strip(),
        "viatura": viatura,
        "pickup": f"{data_in} às {hora_in}".strip(" às"),
        "dropoff": f"{data_out} às {hora_out}".strip(" às"),
        "num_viajantes": booking.get("Viajantes", ""),
        "paises": str(booking.get("Países Permitidos", "") or "").strip(),
        # Camas / kit conforto
        "kit_conforto": _is_sim(resp_kit) or "a" in _norm(resp_kit)[:1] or "b" in _norm(resp_kit)[:1],
        "cama_cabine": _is_sim(answer_for(answers, KW_CAMA_CABINE)),
        "beliches": _is_sim(answer_for(answers, KW_BELICHES)),
        "sala_grande": _is_sim(resp_sala_grande) or _norm(resp_sala_grande)[:1] in ("a", "b"),
        "sala_grande_extensores": sala_grande_extensores(resp_sala_grande),
        "sala_pequena": _is_sim(answer_for(answers, KW_SALA_PEQUENA)),
        # Cozinha
        "microondas": _is_sim(answer_for(answers, KW_MICROONDAS)),
        "maquina_cafe": _is_sim(answer_for(answers, KW_MAQUINA_CAFE)),
        # Exterior
        "mesa_exterior": _is_sim(answer_for(answers, KW_MESA_EXTERIOR)),
        "num_cadeiras": answer_for(answers, KW_CADEIRAS),
        "num_bancos": answer_for(answers, KW_BANCOS),
        # Outros
        "via_verde": _is_sim(answer_for(answers, KW_VIA_VERDE)),
        "cadeira_auto": _is_sim(answer_for(answers, KW_CADEIRA_AUTO)),
        # Notas
        "notas": [pedidos] if pedidos else [],
    }


# --- Tally API ------------------------------------------------------------

def _tally_get(path: str) -> dict:
    req = urllib.request.Request(
        f"{TALLY_API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {TALLY_API_KEY}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_submissions(form_id: str) -> list:
    """Devolve todas as submissões completas de um formulário Tally."""
    out = []
    page = 1
    while True:
        payload = _tally_get(f"/forms/{form_id}/submissions?page={page}")
        # Mapa questionId → título da pergunta.
        questions = {
            q.get("id"): q.get("title", "")
            for q in payload.get("questions", [])
        }
        for sub in payload.get("submissions", []):
            if not sub.get("isCompleted", True):
                continue
            out.append({
                "id": str(sub.get("id", "")),
                "answers": _extract_answers(sub, questions),
            })
        if not payload.get("hasMore"):
            break
        page += 1
        if page > 50:  # salvaguarda
            break
    return out


def _extract_answers(submission: dict, questions: dict) -> dict:
    """Constrói {titulo_pergunta_normalizado: resposta_texto} de uma submissão."""
    answers = {}
    for resp in submission.get("responses", []):
        qid = resp.get("questionId")
        titulo = _norm(questions.get(qid, resp.get("title", "")))
        if not titulo:
            continue
        answers[titulo] = _answer_text(resp.get("answer", resp.get("value", "")))
    return answers


def _answer_text(answer) -> str:
    """Normaliza o valor de uma resposta Tally para texto."""
    if answer is None:
        return ""
    if isinstance(answer, bool):
        return "Sim" if answer else "Não"
    if isinstance(answer, (list, tuple)):
        return ", ".join(_answer_text(a) for a in answer if a not in (None, ""))
    if isinstance(answer, dict):
        # Opções de escolha múltipla vêm muitas vezes como {id, text}.
        return str(answer.get("text") or answer.get("label") or answer.get("value") or "")
    return str(answer)


# --- Folha de estado "Checklists" -----------------------------------------

def ensure_checklists_worksheet(spreadsheet):
    try:
        ws = spreadsheet.worksheet(CHECKLISTS_WORKSHEET)
        if ws.row_values(1) != CHECKLISTS_HEADERS:
            ws.update("A1:F1", [CHECKLISTS_HEADERS])
        return ws
    except gspread.exceptions.WorksheetNotFound:
        log(f"Worksheet '{CHECKLISTS_WORKSHEET}' não existe — a criar.")
        ws = spreadsheet.add_worksheet(title=CHECKLISTS_WORKSHEET, rows=2000, cols=6)
        ws.update("A1:F1", [CHECKLISTS_HEADERS])
        return ws


def load_state(ws) -> dict:
    rows = ws.get_all_records()
    state = {}
    for idx, row in enumerate(rows):
        sid = str(row.get("tally_submission_id", "")).strip()
        if sid:
            state[sid] = {
                "estado": str(row.get("estado", "")).strip(),
                "_row_index": idx + 2,
            }
    return state


def upsert_state(ws, state_map: dict, sid: str, booking_id: str, estado: str,
                 pdf_drive: str = "", erro: str = ""):
    sid = str(sid)
    row_values = [sid, str(booking_id), estado, now_iso(), pdf_drive, erro]
    if sid in state_map and "_row_index" in state_map[sid]:
        ws.update(f"A{state_map[sid]['_row_index']}:F{state_map[sid]['_row_index']}", [row_values])
    else:
        ws.append_row(row_values, value_input_option="USER_ENTERED")
        state_map[sid] = {"_row_index": len(state_map) + 2}
    state_map[sid].update({"estado": estado})


# --- Orquestração ---------------------------------------------------------

def run() -> dict:
    log("=== checklist_runner ===")

    if not TALLY_API_KEY:
        log("TALLY_API_KEY não definida — a saltar geração de checklists.")
        return {"gerados": 0, "saltado": True}
    if not DRIVE_DOCS_ROOT_FOLDER_ID:
        log("DRIVE_DOCS_ROOT_FOLDER_ID não definida — a saltar.")
        return {"gerados": 0, "saltado": True}

    sheets, drive = get_google_clients()
    spreadsheet = sheets.open(GOOGLE_SHEET_NAME)
    reservas_ws = spreadsheet.worksheet(WORKSHEET_NAME)
    state_ws = ensure_checklists_worksheet(spreadsheet)

    # Índice das reservas por ref.
    reservas = {}
    for row in reservas_ws.get_all_records():
        ref = str(row.get("ID", "") or "").strip()
        if ref:
            reservas[ref] = row
    log(f"  → {len(reservas)} reservas indexadas")

    state_map = load_state(state_ws)
    log(f"  → {len(state_map)} checklists já registadas")

    # Recolher submissões Tally novas.
    novas = []
    for form_id in TALLY_FORM_IDS:
        if not form_id:
            continue
        try:
            subs = fetch_submissions(form_id)
            log(f"  Tally [{form_id}]: {len(subs)} submissões")
        except urllib.error.HTTPError as e:
            log(f"  ✗ Tally [{form_id}] HTTP {e.code} — a saltar formulário")
            continue
        except Exception as e:
            log(f"  ✗ Tally [{form_id}] erro: {e} — a saltar formulário")
            continue
        for sub in subs:
            if state_map.get(sub["id"], {}).get("estado", "") in ESTADOS_TERMINAIS:
                continue
            novas.append(sub)

    if not novas:
        log("Sem respostas Tally novas para processar.")
        return {"gerados": 0, "sem_reserva": 0, "falhados": 0, "pendentes": 0}

    lote = novas[:CHECKLISTS_MAX_PER_RUN]
    log(f"{len(novas)} respostas pendentes — a processar {len(lote)} neste run.")

    gerados = sem_reserva = falhados = 0

    for sub in lote:
        sid = sub["id"]
        answers = sub["answers"]
        ref = _norm(answer_for(answers, KW_REF)).upper().lstrip("#").strip()

        booking = reservas.get(ref)
        if not booking:
            log(f"  · submissão {sid}: ref '{ref}' não corresponde a nenhuma reserva")
            upsert_state(state_ws, state_map, sid, ref, "sem_reserva",
                         erro="ref sem correspondência na folha Reservas")
            sem_reserva += 1
            time.sleep(0.4)
            continue

        try:
            data = tally_to_checklist_data(answers, booking)
            pdf = generate_checklist_pdf(data)
            nome = full_guest_name(booking)
            folder_id = find_or_create_folder(
                drive, booking_folder_name(ref, nome), DRIVE_DOCS_ROOT_FOLDER_ID,
            )
            filename = f"Checklist #{ref}.pdf"
            existing = find_file(drive, filename, folder_id)
            if existing:
                # Substitui um PDF anterior (resposta atualizada) — apaga e recria.
                drive.files().delete(fileId=existing, supportsAllDrives=True).execute()
            fid = upload_pdf(drive, pdf, filename, folder_id)
            upsert_state(state_ws, state_map, sid, ref, "gerado", pdf_drive=file_url(fid))
            log(f"  ✓ submissão {sid} → checklist #{ref} ({nome})")
            gerados += 1
        except Exception as e:
            tipo = type(e).__name__
            log(f"  ✗ submissão {sid} (#{ref}): {tipo}: {e}")
            upsert_state(state_ws, state_map, sid, ref, "falhou", erro=f"{tipo}: {str(e)[:180]}")
            falhados += 1
        time.sleep(0.4)

    restantes = len(novas) - len(lote)
    log(
        f"=== Fim: gerados={gerados} sem_reserva={sem_reserva} "
        f"falhados={falhados} | {restantes} na fila ==="
    )
    return {
        "gerados": gerados,
        "sem_reserva": sem_reserva,
        "falhados": falhados,
        "pendentes": restantes,
    }


def main():
    """Entry point para ser chamado do run.py."""
    return run()


if __name__ == "__main__":
    main()
