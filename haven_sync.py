"""
haven_sync.py — Entrada direta Yescapa → Haven Desk (contrato v1.1). v2 (revisão).

ESTADO: PREPARADO E DESLIGADO. Não está ligado ao sync das planilhas e não envia
nada sem as env vars de ativação. Não usa chaves Supabase.

Correções da revisão independente cobertas aqui:
  (1) instante com milissegundos aceite (.000/.001/.123/.999 e segundos inteiros),
      num único `now`.
  (2A) prova POSITIVA dos 7 counts: uma chave em falta (count nunca recebido) →
      complete=false (não se inventa zero). + sinal detail_ok (ver build_envelope).
  (3) send_envelope normaliza sucesso E erro HTTP para um formato único com `code`
      e `retry_after`, para a classificação funcionar no caminho real.
  (8) is_enabled() também exige HAVEN_INGEST_ENABLED == "true".
  (9) validação local confere a distribuição das linhas da matriz por grupo
      (Estado + Estado Meta) e recusa estados inesperados / arquivadas não cobertas.
  (11) send_envelope nunca devolve texto bruto do corpo nem a mensagem da exceção
      (sem fuga de segredos); só códigos agregados permitidos.

Env vars (só na ATIVAÇÃO; ausência/≠true = desligado):
  HAVEN_INGEST_ENABLED  "true" habilita (qualquer outro valor = desligado)
  HAVEN_INGEST_URL      endpoint documentado
  HAVEN_INGEST_TOKEN    credencial exclusiva (base64url 43–128) — nunca logar
"""
import json
import os
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone

# As 37 colunas, na ordem exata da folha Reservas / do fixture do Haven.
HEADERS = [
    "ID", "Estado", "Estado Meta", "Data Início", "Hora Início", "Data Fim",
    "Hora Fim", "Nº Dias", "Hóspede Nome", "Hóspede Apelido", "Hóspede Email",
    "Hóspede Telefone", "Hóspede Verificado", "Hóspede Reservas", "Viajantes",
    "2º Condutor", "Veículo", "Matrícula", "Cidade", "Morada", "País",
    "KM Incluídos", "Opção KM", "Seguro", "Cobertura Seguro", "Preço Hóspede",
    "Ganhos Proprietário", "Moeda", "Caução", "Meio Caução", "Reserva Instantânea",
    "Profissional", "Confirmado Em", "Países Permitidos", "Motivo Cancelamento",
    "Contrato", "Fatura",
]
COL_ESTADO = HEADERS.index("Estado")          # 1
COL_META = HEADERS.index("Estado Meta")       # 2

BY_STATE_KEYS = [
    "confirmed", "waiting", "todo",
    "archived/TO_COME", "archived/CANCELLED_GUEST",
    "archived/CANCELLED_OWNER", "archived/CANCELLED_BOTH",
]
ARCHIVED_KEYS = [k for k in BY_STATE_KEYS if k.startswith("archived/")]
ARCHIVED_SUBS = {k.split("/", 1)[1] for k in ARCHIVED_KEYS}

SOURCE_LOCATOR = "1xEgmflWQZNo3o0AEzkt1n4XgdgSNw4UWoULkbCosnWQ/Reservas"
MAX_ROWS = 2000
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,100}$")
SNAPSHOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z$")

# Códigos 409 e as ações correspondentes (contrato v1.1).
_409_OBSOLETO = {"before_cutover", "stale_snapshot", "snapshot_time_conflict", "pending_conflict"}
_409_PARAR = {"configuration_changed", "writer_not_enabled"}

MAX_RESPONSE_BYTES = 65536   # corpo de resposta lido de forma LIMITADA


def _as_int_or_none(v):
    """Coerção segura para inteiro; devolve None se não for coercível (nunca levanta)."""
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --- Construção da matriz (idêntica à linha escrita na folha) --------------

def booking_row(b: dict) -> list:
    return [str(b.get(h, "") or "") for h in HEADERS]


def build_matrix(yescapa_bookings: list) -> list:
    return [list(HEADERS)] + [booking_row(b) for b in yescapa_bookings]


# --- Construção do envelope ------------------------------------------------

def build_extraction(api_totals: dict, captured: dict, archived_api_total,
                     detail_ok: bool, pagination_ok: bool) -> dict:
    """`api_totals` é o dicionário CRU dos counts recebidos: uma chave EM FALTA
    significa que o count nunca foi recebido (não se inventa zero). `complete` só
    é True com prova positiva dos 7 counts, detalhes necessários obtidos, tudo a
    bater e o total independente de arquivadas coerente."""
    by_state = {}
    missing = []
    for k in BY_STATE_KEYS:
        cap = _as_int_or_none(captured.get(k, 0)) or 0
        at = _as_int_or_none(api_totals.get(k)) if k in api_totals else None
        if at is None:          # count não recebido OU malformado → não inventa
            at = cap            # placeholder; complete=false bloqueia o envio
            missing.append(k)
        by_state[k] = {"api_total": at, "captured": cap}

    soma_arq = sum(by_state[k]["api_total"] for k in ARCHIVED_KEYS)
    todos_batem = all(g["api_total"] == g["captured"] for g in by_state.values())
    arquivadas_ok = (archived_api_total is not None and int(archived_api_total) == soma_arq)
    complete = bool(pagination_ok and detail_ok and not missing and todos_batem and arquivadas_ok)

    return {
        "complete": complete,
        "archived_api_total": int(archived_api_total) if archived_api_total is not None else -1,
        "by_state": by_state,
    }


def build_envelope(yescapa_bookings: list, api_totals: dict, captured: dict,
                   archived_api_total, source_snapshot_at: str, event_id: str,
                   detail_ok: bool = True, pagination_ok: bool = True) -> dict:
    matrix = build_matrix(yescapa_bookings)
    extraction = build_extraction(api_totals, captured, archived_api_total,
                                  detail_ok, pagination_ok)
    return {
        "version": 1,
        "event_id": event_id,
        "source_locator": SOURCE_LOCATOR,
        "source_snapshot_at": source_snapshot_at,
        "rows_total": len(matrix) - 1,
        "extraction": extraction,
        "matrix": matrix,
    }


# --- Validação local (guarda antes de enviar) ------------------------------

def _parse_snapshot(snap):
    """Devolve datetime UTC ou None. Aceita segundos inteiros e .NNN (ms)."""
    if not (isinstance(snap, str) and SNAPSHOT_RE.match(snap)):
        return None
    base = snap[:-1]  # tira o Z
    fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in base else "%Y-%m-%dT%H:%M:%S"
    try:
        return datetime.strptime(base, fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _row_group(estado: str, meta: str):
    """Mapeia (Estado, Estado Meta) para um dos 7 grupos, ou None se não coberto."""
    if meta in ("confirmed", "waiting", "todo"):
        return meta
    if meta == "archived" and estado in ARCHIVED_SUBS:
        return f"archived/{estado}"
    return None


def validate_envelope(env: dict) -> list:
    e = []
    top = {"version", "event_id", "source_locator", "source_snapshot_at",
           "rows_total", "extraction", "matrix"}
    if not isinstance(env, dict) or set(env.keys()) != top:
        return [f"chaves de topo != esperadas: {set(env.keys()) ^ top if isinstance(env, dict) else 'não-dict'}"]

    if env["version"] != 1:
        e.append("version != 1")
    if not (isinstance(env["event_id"], str) and EVENT_ID_RE.match(env["event_id"])):
        e.append("event_id inválido (8–100 [A-Za-z0-9._-])")
    if env["source_locator"] != SOURCE_LOCATOR:
        e.append("source_locator != esperado")

    dt = _parse_snapshot(env.get("source_snapshot_at"))
    if dt is None:
        e.append("source_snapshot_at fora do formato ISO UTC Z (seg ou .NNN)")
    elif (dt - datetime.now(timezone.utc)).total_seconds() > 300:
        e.append("source_snapshot_at mais de 5 min no futuro")

    rt = env["rows_total"]
    if not (isinstance(rt, int) and 0 <= rt <= MAX_ROWS):
        e.append("rows_total fora de 0..2000")

    ext = env["extraction"]
    if not isinstance(ext, dict) or set(ext.keys()) != {"complete", "archived_api_total", "by_state"}:
        return e + ["extraction com chaves erradas"]
    if not isinstance(ext.get("complete"), bool):
        e.append("extraction.complete não é booleano")
    if set(ext["by_state"].keys()) != set(BY_STATE_KEYS):
        return e + ["by_state não tem exatamente os 7 grupos"]

    soma_cap = 0
    for k in BY_STATE_KEYS:
        g = ext["by_state"][k]
        if set(g.keys()) != {"api_total", "captured"}:
            e.append(f"grupo {k} tem chaves além de api_total/captured")
            continue
        if not all(isinstance(g[x], int) and g[x] >= 0 for x in ("api_total", "captured")):
            e.append(f"grupo {k}: api_total/captured não são inteiros >=0")
            continue
        if g["api_total"] != g["captured"]:
            e.append(f"grupo {k}: api_total != captured")
        soma_cap += g["captured"]

    if isinstance(rt, int) and soma_cap != rt:
        e.append(f"soma captured ({soma_cap}) != rows_total ({rt})")

    soma_arq = sum(ext["by_state"][k]["api_total"] for k in ARCHIVED_KEYS)
    if not isinstance(ext["archived_api_total"], int) or ext["archived_api_total"] < 0:
        e.append("archived_api_total ausente/negativo (consulta sem filtro falhou?)")
    elif ext["archived_api_total"] != soma_arq:
        e.append(f"archived_api_total ({ext['archived_api_total']}) != soma dos 4 grupos ({soma_arq})")

    m = env["matrix"]
    if not isinstance(m, list) or not m:
        return e + ["matrix vazia/inválida"]
    if m[0] != HEADERS:
        e.append("cabeçalho da matriz != 37 colunas exatas")
    ids, dist, uncovered = [], {k: 0 for k in BY_STATE_KEYS}, 0
    for i, row in enumerate(m[1:], start=1):
        if not isinstance(row, list) or len(row) != 37:
            e.append(f"linha {i} não tem 37 células")
            continue
        if any(not isinstance(c, str) for c in row):
            e.append(f"linha {i} tem célula não-texto (null/número são inválidos)")
            continue
        ids.append(row[0])
        g = _row_group(row[COL_ESTADO], row[COL_META])
        if g is None:
            uncovered += 1
        else:
            dist[g] += 1
    if isinstance(rt, int) and len(m) - 1 != rt:
        e.append(f"nº de linhas da matriz ({len(m)-1}) != rows_total ({rt})")
    if len(ids) != len(set(ids)):
        e.append("IDs de reserva duplicados na matriz")
    for _id in ids:
        if not _id.isdigit():
            e.append(f"ID não-Yescapa (não numérico) na matriz: {_id!r}")
            break
    if uncovered:
        e.append(f"matriz tem {uncovered} linha(s) de estado inesperado/arquivada não coberta")
    # (9) distribuição da matriz por grupo tem de bater com captured
    for k in BY_STATE_KEYS:
        if dist[k] != ext["by_state"][k]["captured"]:
            e.append(f"distribuição da matriz no grupo {k} ({dist[k]}) != captured "
                     f"({ext['by_state'][k]['captured']})")
    return e


# --- Envio (DESLIGADO sem env de ativação) ---------------------------------

def is_enabled() -> bool:
    return bool(
        os.getenv("HAVEN_INGEST_ENABLED", "").strip().lower() == "true"
        and os.getenv("HAVEN_INGEST_URL", "").strip()
        and os.getenv("HAVEN_INGEST_TOKEN", "").strip()
    )


def _retry_after(headers) -> int:
    try:
        v = headers.get("Retry-After") if headers else None
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def _normalize(http, raw_body, headers) -> dict:
    """Formato único e LIMITADO: {http, response:{ok?,status?,code?}, retry_after}.
    Nunca inclui o corpo bruto nem mensagens — só os campos permitidos."""
    parsed = {}
    try:
        data = json.loads(raw_body.decode("utf-8", "ignore") or "{}")
        if isinstance(data, dict):
            for key in ("ok", "status", "code", "replayed"):
                if key in data:
                    parsed[key] = data[key]
    except (ValueError, AttributeError):
        parsed = {}
    return {"http": http, "response": parsed, "retry_after": _retry_after(headers)}


def send_envelope(env: dict) -> dict:
    """POST ao Haven. Desligado (no-op) sem ativação. Nunca levanta, nunca
    devolve texto bruto nem segredos. Devolve o formato normalizado para a fila."""
    if not is_enabled():
        return {"skipped": True, "reason": "desligado (HAVEN_INGEST_ENABLED!=true ou sem URL/TOKEN)"}

    erros = validate_envelope(env)
    if erros:
        return {"skipped": True, "reason": "envelope inválido", "erros": erros[:30]}
    if not env["extraction"]["complete"]:
        return {"skipped": True, "reason": "extração incompleta — não enviar"}

    url = os.environ["HAVEN_INGEST_URL"].strip()
    token = os.environ["HAVEN_INGEST_TOKEN"].strip()
    body = json.dumps(env, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    # Identificar o cliente: o alojamento recusa Python-urllib com HTTP 403/1010.
    # Não imitar um navegador nem alterar autenticação ou verificação TLS.
    req.add_header("User-Agent", "HavenDesk-Yescapa/1.1 (+https://havennordis.com)")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")  # nunca logado/retornado
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return _normalize(resp.status, resp.read(MAX_RESPONSE_BYTES), getattr(resp, "headers", None))
    except urllib.error.HTTPError as ex:
        try:
            raw = ex.read(MAX_RESPONSE_BYTES)
        except Exception:
            raw = b""
        return _normalize(ex.code, raw, getattr(ex, "headers", None))
    except Exception:
        # Só um código agregado — nunca o tipo/mensagem (podem conter segredos).
        return {"error_code": "transport_error"}
