"""
supabase_mirror.py — Espelha as reservas (as MESMAS que vão para a folha
"Reservas") para uma tabela no Supabase, para alimentar o Haven Desk.

Princípios (NÃO pode quebrar a folha Google que o Painel Interno usa hoje):
  • OPCIONAL: se SUPABASE_URL ou SUPABASE_KEY não estiverem definidas, é no-op.
    Enquanto as env vars não estiverem no Railway, produção fica igual a hoje.
  • ISOLADO / BEST-EFFORT: é chamado DEPOIS de a folha já estar escrita e está
    envolvido em try/except no chamador. Qualquer erro aqui é apanhado e logado;
    nunca levanta exceção para cima → nunca afeta o sync da folha.
  • UPSERT por `id` (merge-duplicates): não apaga nada. Atualiza a linha
    existente ou insere uma nova. Reservas canceladas ficam com o seu estado.
  • SEM dependências novas: usa só urllib (stdlib) + a REST API (PostgREST).

Env vars:
  SUPABASE_URL      ex.: https://xxxxx.supabase.co   (obrigatória p/ ativar)
  SUPABASE_KEY      service_role key (ou outra com escrita na tabela)  (idem)
  SUPABASE_TABLE    default "reservas"
  SUPABASE_SCHEMA   default "public"
  SUPABASE_DRY_RUN  "1" → não envia; imprime a amostra do payload (p/ validar)

Contrato da tabela (a confirmar com o Wanderson):
  - coluna `id` como PRIMARY KEY / UNIQUE (para o upsert on_conflict=id).
  - colunas com os nomes de FIELD_MAP (snake_case). Ajustar FIELD_MAP se o
    Wanderson preferir outros nomes.
"""
import json
import os
import urllib.request
import urllib.error
from datetime import datetime, timezone

# PT (chaves do parse_booking / cabeçalho da folha) -> snake_case (Supabase).
# É aqui, e só aqui, que se alinham os nomes das colunas com o Haven Desk.
FIELD_MAP = {
    "ID":                  "id",
    "Estado":              "estado",
    "Estado Meta":         "estado_meta",
    "Data Início":         "data_inicio",
    "Hora Início":         "hora_inicio",
    "Data Fim":            "data_fim",
    "Hora Fim":            "hora_fim",
    "Nº Dias":             "num_dias",
    "Hóspede Nome":        "hospede_nome",
    "Hóspede Apelido":     "hospede_apelido",
    "Hóspede Email":       "hospede_email",
    "Hóspede Telefone":    "hospede_telefone",
    "Hóspede Verificado":  "hospede_verificado",
    "Hóspede Reservas":    "hospede_reservas",
    "Viajantes":           "viajantes",
    "2º Condutor":         "segundo_condutor",
    "Veículo":             "veiculo",
    "Matrícula":           "matricula",
    "Cidade":              "cidade",
    "Morada":              "morada",
    "País":                "pais",
    "KM Incluídos":        "km_incluidos",
    "Opção KM":            "opcao_km",
    "Seguro":              "seguro",
    "Cobertura Seguro":    "cobertura_seguro",
    "Preço Hóspede":       "preco_hospede",
    "Ganhos Proprietário": "ganhos_proprietario",
    "Moeda":               "moeda",
    "Caução":              "caucao",
    "Meio Caução":         "meio_caucao",
    "Reserva Instantânea": "reserva_instantanea",
    "Profissional":        "profissional",
    "Confirmado Em":       "confirmado_em",
    "Países Permitidos":   "paises_permitidos",
    "Motivo Cancelamento": "motivo_cancelamento",
    "Contrato":            "contrato",
    "Fatura":              "fatura",
}

CONFLICT_COLUMN = "id"   # coluna única p/ o upsert
BATCH_SIZE = 500         # upsert em lotes, por segurança


def _log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] [supabase] {msg}", flush=True)


def _to_row(booking: dict) -> dict:
    """Converte uma reserva (dict PT) numa linha com colunas snake_case.
    Mantém os tipos nativos (id int, booleanos, números) tal como vêm da API;
    campos em falta ficam None. Acrescenta `atualizado_em` (ISO, UTC)."""
    row = {}
    for pt_key, col in FIELD_MAP.items():
        val = booking.get(pt_key, None)
        if val == "":
            val = None
        row[col] = val
    row["atualizado_em"] = datetime.now(timezone.utc).isoformat()
    return row


def _post_upsert(url: str, key: str, table: str, schema: str, rows: list) -> None:
    endpoint = f"{url.rstrip('/')}/rest/v1/{table}?on_conflict={CONFLICT_COLUMN}"
    body = json.dumps(rows).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, method="POST")
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Content-Profile", schema)
    # merge-duplicates = upsert; return=minimal evita devolver as linhas todas.
    req.add_header("Prefer", "resolution=merge-duplicates,return=minimal")
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def mirror_bookings(bookings: list) -> dict:
    """Espelha `bookings` (a MESMA lista que foi para a folha) no Supabase.
    Nunca levanta exceção. Devolve um pequeno resumo do que aconteceu."""
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_KEY", "").strip()
    table = os.getenv("SUPABASE_TABLE", "reservas").strip()
    schema = os.getenv("SUPABASE_SCHEMA", "public").strip()
    dry = os.getenv("SUPABASE_DRY_RUN", "").lower() in ("1", "true", "yes")

    if not url or not key:
        _log("SUPABASE_URL/KEY não definidas — espelho desativado (no-op).")
        return {"skipped": True}

    if not bookings:
        _log("sem reservas para espelhar.")
        return {"skipped": True, "n": 0}

    try:
        rows = [_to_row(b) for b in bookings]
    except Exception as e:
        _log(f"ERRO a mapear linhas ({type(e).__name__}: {e}) — nada enviado.")
        return {"erro": str(e)}

    if dry:
        _log(f"DRY-RUN: {len(rows)} linhas. Amostra (1.ª linha):")
        print(json.dumps(rows[0], ensure_ascii=False, indent=2))
        return {"dry_run": True, "n": len(rows)}

    enviados = 0
    for i in range(0, len(rows), BATCH_SIZE):
        lote = rows[i:i + BATCH_SIZE]
        try:
            _post_upsert(url, key, table, schema, lote)
            enviados += len(lote)
        except urllib.error.HTTPError as e:
            corpo = ""
            try:
                corpo = e.read().decode("utf-8", "ignore")[:300]
            except Exception:
                pass
            _log(f"ERRO HTTP {e.code} no lote {i//BATCH_SIZE + 1}: {corpo} — abortado.")
            return {"erro": f"HTTP {e.code}", "enviados": enviados}
        except Exception as e:
            _log(f"ERRO {type(e).__name__} no lote {i//BATCH_SIZE + 1}: {e} — abortado.")
            return {"erro": str(e), "enviados": enviados}

    _log(f"OK: {enviados} reservas espelhadas para '{schema}.{table}'.")
    return {"enviados": enviados}
