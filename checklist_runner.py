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

Mapeamento calibrado contra o formulário real (zx2ORZ) em 21/05/2026:
os títulos das perguntas e os formatos das respostas foram confirmados
contra submissões reais da API do Tally.

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
from zoneinfo import ZoneInfo

LISBON_TZ = ZoneInfo("Europe/Lisbon")

import gspread
from dotenv import load_dotenv

from checklist_generator import generate_checklist_pdf, generate_tally_pdf
from drive_archiver import (
    get_google_clients, find_or_create_folder, find_file, upload_pdf, update_pdf,
    booking_folder_name, resolve_booking_folder, full_guest_name, folder_url, file_url,
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
    "tally_submission_id", "booking_id", "estado", "timestamp", "pdf_drive", "erro", "via_verde",
]
# Versão do gerador. Ao subir a versão, todas as checklists antigas (estado "gerado"
# ou "gerado:vN" anterior) são regeneradas UMA vez e depois voltam a ser terminais.
# v2: suporte a respostas do formulário Tally em inglês (antes saíam a "N/A").
# v3: texto livre (crianças/pedidos) e kit parcial nas notas + página 3 de conferência.
# v4: checklist volta a 2 páginas; respostas do Tally passam a PDF SEPARADO
#     ("Formulário Tally #ref.pdf") na mesma pasta, com ícone próprio na app.
CHECKLIST_VERSION = "5"  # v5: mapeamento EN por posição (robusto p/ respostas inglesas)
ESTADO_GERADO = f"gerado:v{CHECKLIST_VERSION}"
ESTADOS_TERMINAIS = {ESTADO_GERADO}  # sem_reserva/falhou/versões antigas são reprocessados

# Palavras-chave para casar o título de cada pergunta Tally com o campo interno.
# ⚠️ Confirmar contra uma submissão real — ver checklist_template_design.md.
# Incluem PT (form zx2ORZ) E EN (form BzAOr5) — sem as EN, respostas em inglês
# não casavam com nenhuma pergunta e a checklist saía toda a "N/A".
KW_KIT_CONFORTO = ("roupa de cama", "kit conforto", "bedding")
KW_CAMA_CABINE = ("cabine", "capucine", "over-cab", "cab bed")
KW_BELICHES = ("beliche", "bunk")
KW_SALA_GRANDE = ("sala grande", "main lounge")
KW_SALA_PEQUENA = ("sala pequena", "rear lounge", "small lounge")
KW_MICROONDAS = ("micro-onda", "microonda", "micro onda", "microwave")
KW_MAQUINA_CAFE = ("máquina de café", "maquina de cafe", "café", "coffee", "nespresso")
KW_MESA_EXTERIOR = ("mesa exterior", "mesa", "outdoor table", "table")
KW_CADEIRAS = ("cadeira", "número de cadeira", "chair")
KW_BANCOS = ("banco", "stool")
KW_VIA_VERDE = ("via verde", "toll")
KW_CADEIRA_AUTO = ("cadeirinha", "cadeira de criança", "cadeira auto", "bebé", "car seat")
KW_CRIANCAS = ("idade", "altura", "peso", "children", "weight and height")
KW_PEDIDOS = ("pedido", "indicaç", "observa", "especia", "request", "special", "notes")
KW_REF = ("ref", "referência", "referencia", "reference")

# Campo da checklist → palavras-chave que identificam a pergunta no Tally.
# Usado (1) no matching normal e (2) para o mapeamento POR POSIÇÃO entre o form
# PT e o EN (ver build_en_override), que torna a captação de respostas EN robusta
# mesmo quando a tradução usa palavras que não estão nas keywords.
FIELD_KEYWORDS = {
    "cama_cabine":  KW_CAMA_CABINE,
    "beliches":     KW_BELICHES,
    "sala_grande":  KW_SALA_GRANDE,
    "sala_pequena": KW_SALA_PEQUENA,
    "microondas":   KW_MICROONDAS,
    "maquina_cafe": KW_MAQUINA_CAFE,
    "mesa_exterior": KW_MESA_EXTERIOR,
    "num_cadeiras": KW_CADEIRAS,
    "num_bancos":   KW_BANCOS,
    "via_verde":    KW_VIA_VERDE,
    "cadeira_auto": KW_CADEIRA_AUTO,
    "kit":          KW_KIT_CONFORTO,
    "criancas":     KW_CRIANCAS,
    "pedidos":      KW_PEDIDOS,
}


# --- Logging --------------------------------------------------------------

def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] [checklist] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(LISBON_TZ).strftime("%Y-%m-%d_%H:%M:%S")


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


def field_of_title(titulo: str):
    """Devolve o campo da checklist que uma pergunta (título) identifica, ou None.
    É o inverso de FIELD_KEYWORDS — usado para alinhar PT↔EN por posição."""
    t = _norm(titulo)
    for field, kws in FIELD_KEYWORDS.items():
        if any(kw in t for kw in kws):
            return field
    return None


def build_en_override(pt_titles: list, en_titles: list) -> dict:
    """Mapeia títulos de perguntas do form EN → campo da checklist, por POSIÇÃO
    relativa ao form PT (os dois forms são traduções com as mesmas perguntas na
    mesma ordem). Devolve {titulo_en_normalizado: campo}.

    Rede de segurança para respostas EN: só mapeia uma pergunta EN quando as
    palavras-chave NÃO a apanharam (field_of_title(en) is None) mas a pergunta PT
    na mesma posição tem campo conhecido — assim nunca sobrepõe um match correto.
    Guardas: exige o MESMO nº de perguntas nos dois forms (senão desliga e avisa).
    """
    if not pt_titles or not en_titles:
        return {}
    if len(pt_titles) != len(en_titles):
        log(f"  ⚠ forms PT/EN com nº de perguntas diferente "
            f"({len(pt_titles)} vs {len(en_titles)}) — mapeamento por posição "
            f"DESLIGADO; a usar só palavras-chave. Verificar alinhamento dos forms.")
        return {}
    override = {}
    for pt_t, en_t in zip(pt_titles, en_titles):
        campo_pt = field_of_title(pt_t)
        campo_en = field_of_title(en_t)
        if campo_pt and campo_en and campo_pt != campo_en:
            log(f"  ⚠ possível desalinhamento PT/EN: PT={pt_t!r}→{campo_pt} vs "
                f"EN={en_t!r}→{campo_en} (mantém keyword).")
            continue
        if campo_pt and campo_en is None:
            override[_norm(en_t)] = campo_pt
    return override


def sala_grande_extensores(resposta: str) -> int:
    """Cama da sala grande: opção A → 1 extensor (meio); B → 2 (laterais)."""
    v = _norm(resposta)
    if v.startswith("b") or "2" in v or "laterais" in v:
        return 2
    return 1


def kit_modo(resposta: str) -> str:
    """Modo do kit conforto: 'full' | 'roupa' | 'toalhas' | 'nenhum'.
    Opções PT/EN: kit completo / apenas roupa de cama / apenas toalhas / nenhum.
    """
    v = _norm(resposta)
    if not v:
        return "nenhum"
    if "completo" in v or "full" in v:
        return "full"
    if v.startswith("não") or v.startswith("nao") or v.startswith("no"):
        return "nenhum"
    if v.startswith("sim") or v.startswith("yes"):
        return "full"
    tem_roupa = "cama" in v or "bedding" in v
    tem_toalha = "toalha" in v or "towel" in v
    if tem_roupa and tem_toalha:
        return "full"
    if tem_roupa:
        return "roupa"
    if tem_toalha:
        return "toalhas"
    if v.startswith("s") or v.startswith("y"):
        return "full"
    return "nenhum"


def tally_to_checklist_data(answers: dict, booking: dict, respostas: list = None,
                            field_override: dict = None) -> dict:
    """Cruza a resposta Tally com a reserva e devolve o dict para o gerador.

    `answers`: {titulo_pergunta_normalizado: resposta}.
    `booking`: linha da folha Reservas (dict).
    `respostas`: lista ordenada [{q: titulo_original, a: resposta}] p/ a página de conferência.
    `field_override`: {campo: resposta} de reserva — fallback por POSIÇÃO (forms EN),
        usado só quando as palavras-chave não apanham a pergunta. Ver build_en_override.
    """
    fo = field_override or {}

    def pick(campo: str, keywords) -> str:
        """Resposta para um campo: palavras-chave primeiro; se falhar, o mapeamento
        por posição (robusto para o form EN)."""
        v = answer_for(answers, keywords)
        return v if v else fo.get(campo, "")

    veiculo = str(booking.get("Veículo", "") or "").strip()
    matricula = str(booking.get("Matrícula", "") or "").strip()
    viatura = f"{veiculo} ({matricula})".strip() if matricula else (veiculo or matricula)

    data_in = str(booking.get("Data Início", "") or "").strip()
    hora_in = str(booking.get("Hora Início", "") or "").strip()
    data_out = str(booking.get("Data Fim", "") or "").strip()
    hora_out = str(booking.get("Hora Fim", "") or "").strip()

    resp_sala_grande = pick("sala_grande", KW_SALA_GRANDE)
    resp_kit = pick("kit", KW_KIT_CONFORTO)
    modo = kit_modo(resp_kit)
    pedidos = pick("pedidos", KW_PEDIDOS)
    criancas = pick("criancas", KW_CRIANCAS)

    # Notas: texto livre do hóspede + kit conforto parcial → tudo fica visível
    # na checklist, sem a equipa ter de ir ao Tally.
    notas = []
    if modo == "roupa":
        notas.append("Kit conforto: APENAS roupa de cama (sem toalhas)")
    elif modo == "toalhas":
        notas.append("Kit conforto: APENAS toalhas (sem roupa de cama)")
    if criancas:
        notas.append(f"Crianças (idade/peso/altura): {criancas}")
    if pedidos:
        notas.append(f"Pedido do hóspede: {pedidos}")

    return {
        "cliente_nome": full_guest_name(booking),
        "reserva_ref": str(booking.get("ID", "") or "").strip(),
        "viatura": viatura,
        "pickup": f"{data_in} às {hora_in}".strip(" às"),
        "dropoff": f"{data_out} às {hora_out}".strip(" às"),
        "num_viajantes": booking.get("Viajantes", ""),
        "paises": str(booking.get("Países Permitidos", "") or "").strip(),
        # Camas / kit conforto (kit parcial: bedding e towels separados)
        "kit_conforto": modo != "nenhum",
        "kit_bedding": modo in ("full", "roupa"),
        "kit_towels": modo in ("full", "toalhas"),
        "cama_cabine": _is_sim(pick("cama_cabine", KW_CAMA_CABINE)),
        "beliches": _is_sim(pick("beliches", KW_BELICHES)),
        "sala_grande": _is_sim(resp_sala_grande),
        "sala_grande_extensores": sala_grande_extensores(resp_sala_grande),
        "sala_pequena": _is_sim(pick("sala_pequena", KW_SALA_PEQUENA)),
        # Cozinha
        "microondas": _is_sim(pick("microondas", KW_MICROONDAS)),
        "maquina_cafe": _is_sim(pick("maquina_cafe", KW_MAQUINA_CAFE)),
        # Exterior
        "mesa_exterior": _is_sim(pick("mesa_exterior", KW_MESA_EXTERIOR)),
        "num_cadeiras": pick("num_cadeiras", KW_CADEIRAS),
        "num_bancos": pick("num_bancos", KW_BANCOS),
        # Outros
        "via_verde": _is_sim(pick("via_verde", KW_VIA_VERDE)),
        "cadeira_auto": _is_sim(pick("cadeira_auto", KW_CADEIRA_AUTO)),
        # Notas (texto livre + kit parcial)
        "notas": notas,
        # Respostas completas p/ página de conferência
        "respostas_tally": respostas or [],
    }


# --- Tally API ------------------------------------------------------------

def _tally_get(path: str) -> dict:
    req = urllib.request.Request(
        f"{TALLY_API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {TALLY_API_KEY}",
            "Accept": "application/json",
            # A API do Tally está atrás de Cloudflare e devolve 403 ao
            # User-Agent por defeito do urllib — usar um UA de browser.
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_submissions(form_id: str):
    """Devolve (submissões_completas, titulos_ordenados) de um formulário Tally.
    `titulos_ordenados` é a lista de perguntas na ordem do form (p/ alinhar PT↔EN)."""
    out = []
    ordered_titles = []
    page = 1
    while True:
        payload = _tally_get(f"/forms/{form_id}/submissions?page={page}")
        # Mapa questionId → título. Campos escondidos (ref, name…) têm
        # title=null no nível da pergunta — o nome real está em fields[].title.
        questions = {}
        for q in payload.get("questions", []):
            title = q.get("title")
            if not title:
                fields = q.get("fields") or []
                if fields:
                    title = fields[0].get("title", "")
            questions[q.get("id")] = title or ""
        if page == 1:
            ordered_titles = [t for t in questions.values() if str(t).strip()]
        for sub in payload.get("submissions", []):
            if not sub.get("isCompleted", True):
                continue
            ans, respostas = _extract_answers(sub, questions)
            out.append({
                "id": str(sub.get("id", "")),
                "answers": ans,
                "respostas": respostas,
                "form_id": form_id,
            })
        if not payload.get("hasMore"):
            break
        page += 1
        if page > 50:  # salvaguarda
            break
    return out, ordered_titles


def _extract_answers(submission: dict, questions: dict):
    """Devolve (answers, respostas):
      answers   = {titulo_normalizado: resposta_texto}  — p/ o mapeamento
      respostas = [{q: titulo_original, a: resposta}]    — ordenado, p/ a conferência
    """
    answers = {}
    respostas = []
    for resp in submission.get("responses", []):
        qid = resp.get("questionId")
        titulo_orig = str(questions.get(qid, resp.get("title", "")) or "").strip()
        titulo = _norm(titulo_orig)
        if not titulo:
            continue
        txt = _answer_text(resp.get("answer", resp.get("value", "")))
        answers[titulo] = txt
        if str(txt).strip():
            respostas.append({"q": titulo_orig, "a": txt})
    return answers, respostas


def _answer_text(answer) -> str:
    """Normaliza o valor de uma resposta Tally para texto."""
    if answer is None:
        return ""
    if isinstance(answer, bool):
        return "Sim" if answer else "Não"
    if isinstance(answer, (list, tuple)):
        return ", ".join(_answer_text(a) for a in answer if a not in (None, ""))
    if isinstance(answer, dict):
        # Escolha múltipla: {id, text}. Campo escondido: {nome_campo: valor}.
        v = answer.get("text") or answer.get("label") or answer.get("value")
        if v:
            return str(v)
        if len(answer) == 1:
            return _answer_text(next(iter(answer.values())))
        return ""
    return str(answer)


# --- Folha de estado "Checklists" -----------------------------------------

def ensure_checklists_worksheet(spreadsheet):
    try:
        ws = spreadsheet.worksheet(CHECKLISTS_WORKSHEET)
        if ws.row_values(1) != CHECKLISTS_HEADERS:
            ws.update("A1:G1", [CHECKLISTS_HEADERS])
        return ws
    except gspread.exceptions.WorksheetNotFound:
        log(f"Worksheet '{CHECKLISTS_WORKSHEET}' não existe — a criar.")
        ws = spreadsheet.add_worksheet(title=CHECKLISTS_WORKSHEET, rows=2000, cols=7)
        ws.update("A1:G1", [CHECKLISTS_HEADERS])
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
                 pdf_drive: str = "", erro: str = "", via_verde: str = ""):
    sid = str(sid)
    row_values = [sid, str(booking_id), estado, now_iso(), pdf_drive, erro, via_verde]
    if sid in state_map and "_row_index" in state_map[sid]:
        ws.update(f"A{state_map[sid]['_row_index']}:G{state_map[sid]['_row_index']}", [row_values])
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
    form_titles = {}
    for form_id in TALLY_FORM_IDS:
        if not form_id:
            continue
        try:
            subs, titles = fetch_submissions(form_id)
            form_titles[form_id] = titles
            log(f"  Tally [{form_id}]: {len(subs)} submissões, {len(titles)} perguntas")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "ignore")[:160]
            except Exception:
                pass
            log(f"  ✗ Tally [{form_id}] HTTP {e.code}: {body} — a saltar formulário")
            continue
        except Exception as e:
            log(f"  ✗ Tally [{form_id}] erro: {e} — a saltar formulário")
            continue
        for sub in subs:
            if state_map.get(sub["id"], {}).get("estado", "") in ESTADOS_TERMINAIS:
                continue
            novas.append(sub)

    # Mapeamento por posição PT↔EN: rede de segurança para respostas do form EN
    # cujas perguntas as palavras-chave não apanham (ex.: "small lounge bed").
    pt_id = TALLY_FORM_IDS[0] if TALLY_FORM_IDS else ""
    en_id = TALLY_FORM_IDS[1] if len(TALLY_FORM_IDS) > 1 else ""
    en_override = build_en_override(form_titles.get(pt_id, []), form_titles.get(en_id, []))
    if en_override:
        log(f"  Mapeamento EN por posição ativo: {len(en_override)} pergunta(s) cobertas "
            f"além das palavras-chave")

    if not novas:
        log("Sem respostas Tally novas para processar.")
        return {"gerados": 0, "sem_reserva": 0, "falhados": 0, "pendentes": 0}

    lote = novas[:CHECKLISTS_MAX_PER_RUN]
    log(f"{len(novas)} respostas pendentes — a processar {len(lote)} neste run.")

    gerados = sem_reserva = falhados = 0

    for sub in lote:
        sid = sub["id"]
        answers = sub["answers"]
        # Fallback por posição: só para submissões do form EN e só preenche campos
        # que as palavras-chave não apanharam (build_en_override já garante isso).
        field_override = {}
        if sub.get("form_id") == en_id and en_override:
            for titulo_norm, resp in answers.items():
                campo = en_override.get(titulo_norm)
                if campo and str(resp).strip():
                    field_override[campo] = resp
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
            data = tally_to_checklist_data(answers, booking, sub.get("respostas"), field_override)
            pdf = generate_checklist_pdf(data)
            nome = full_guest_name(booking)
            folder_id = resolve_booking_folder(
                drive, DRIVE_DOCS_ROOT_FOLDER_ID, ref, nome, booking,
            )
            filename = f"Checklist #{ref}.pdf"
            existing = find_file(drive, filename, folder_id)
            if existing:
                # Atualiza o conteúdo no MESMO ficheiro (mantém o link). Antes
                # apagava+recriava, mas o delete dava 404 (conta de serviço tem
                # edição mas não é dona → só o dono apaga em My Drive).
                fid = update_pdf(drive, pdf, existing)
            else:
                fid = upload_pdf(drive, pdf, filename, folder_id)

            # Formulário Tally como documento SEPARADO (ícone próprio na app).
            # Nome sem "checklist"/"check-in" para não colidir com a deteção da checklist.
            form_pdf = generate_tally_pdf(data)
            if form_pdf:
                form_name = f"Formulário Tally #{ref}.pdf"
                form_existing = find_file(drive, form_name, folder_id)
                if form_existing:
                    update_pdf(drive, form_pdf, form_existing)
                else:
                    upload_pdf(drive, form_pdf, form_name, folder_id)

            vv_str = "Sim" if data.get("via_verde") else "Não"
            upsert_state(state_ws, state_map, sid, ref, ESTADO_GERADO,
                         pdf_drive=file_url(fid), via_verde=vv_str)
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
