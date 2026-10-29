"""
haven_queue.py — Fila durável + repetições para a entrada direta Haven (v1.1). v3.

ESTADO: PREPARADO E DESLIGADO. O envio real só acontece via haven_sync.send_envelope,
no-op sem as env vars de ativação.

Correções da revisão v2 (sobre a v2):
  (2) enqueue é TRANSACIONAL também em memória: o novo item só fica disponível
      DEPOIS de _save com sucesso; se a gravação falhar, é desfeito e a exceção
      propaga → nunca se envia uma captura nova não persistida.
  (3) _compact só compacta entregas CONFIRMADAS (done). fatal/obsoleto CONSERVAM
      envelope, instante e código agregado (evidência p/ investigar/retomar).
      `requeue()` permite retomar um fatal/obsoleto com o MESMO event_id.
  (4) se a QUARENTENA de um ficheiro corrompido falhar, a abertura LEVANTA e NÃO
      devolve [] nem escreve — a evidência nunca é sobrescrita.
  (5) queue_path, em produção, confere a CONTENÇÃO do caminho da fila dentro de
      RAILWAY_VOLUME_MOUNT_PATH (resolvendo links e '..') e recusa fora dele.

Ajuste (6): o orçamento de tempo em process limita o INÍCIO de cada pedido (entre
pedidos), não a duração total — um pedido lento pode ultrapassá-lo. O transporte
lê o corpo de forma LIMITADA (haven_sync.MAX_RESPONSE_BYTES). Um prazo total
rígido exigiria passar o tempo restante ao transporte (follow-up na ativação).

⚠️ Persistência Railway: disco efémero. HAVEN_QUEUE_DIR tem de estar dentro de um
volume montado; o intervalo real entre tentativas é o do cron (~15 min).
"""
import json
import os
import random
import tempfile
import time as _time
from datetime import datetime, timezone, timedelta

BASE_BACKOFF_S = 60
MAX_BACKOFF_S = 900           # 15 min (inclui jitter)
GIVE_UP_AFTER_S = 24 * 3600   # 24 h → sinal de atraso + alerta (NÃO deixa de tentar)
DEFAULT_QUEUE_DIR = ".haven_queue"
DEFAULT_MAX_REQUESTS = 5
DEFAULT_TIME_BUDGET_S = 90
MAX_RECEIPTS = 500            # recibos (done) retidos após compactação

_409_OBSOLETO = {"before_cutover", "stale_snapshot", "snapshot_time_conflict", "pending_conflict"}
_409_PARAR = {"configuration_changed", "writer_not_enabled"}


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def queue_path() -> str:
    """Caminho do ficheiro da fila.
    Com HAVEN_INGEST_ENABLED=true: exige RAILWAY_VOLUME_MOUNT_PATH, recusa
    o default/efémero e exige que o caminho RESOLVIDO da fila
    fique DENTRO dessa raiz (links e '..' resolvidos). Em testes/desligado aceita
    um caminho explícito."""
    d = os.getenv("HAVEN_QUEUE_DIR", "").strip()
    enabled = os.getenv("HAVEN_INGEST_ENABLED", "").strip().lower() == "true"
    if enabled:
        if not d or d == DEFAULT_QUEUE_DIR:
            raise RuntimeError("HAVEN_QUEUE_DIR tem de apontar para um volume "
                               "persistente quando a integração está ligada — "
                               "sem fallback efémero.")
        vol = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
        if not vol:
            raise RuntimeError("RAILWAY_VOLUME_MOUNT_PATH ausente; integração ligada exige volume declarado.")
        if vol:
            vol_r = os.path.realpath(vol)
            d_r = os.path.realpath(d)
            if not (d_r == vol_r or d_r.startswith(vol_r + os.sep)):
                raise RuntimeError(
                    f"HAVEN_QUEUE_DIR ({d}) está fora do volume "
                    f"RAILWAY_VOLUME_MOUNT_PATH ({vol}) — recusado.")
    d = d or DEFAULT_QUEUE_DIR
    return os.path.join(d, "haven_fila.json")


def classify(result: dict) -> str:
    """Resultado de send_envelope → ação: done|retry|fatal|obsoleto|parar."""
    if not isinstance(result, dict):
        return "retry"
    if result.get("skipped"):
        return "parar"
    if "error_code" in result and "http" not in result:
        return "retry"
    http = result.get("http")
    code = _code_of(result)
    if http == 200:
        body = result.get("response") or {}
        if body.get("replayed") or code in ("accepted", "accepted_with_reviews"):
            return "done"
        return "retry"
    if http in (408, 429, 503) or (isinstance(http, int) and 500 <= http <= 599):
        return "retry"
    if http == 401:
        return "fatal"
    if http == 409:
        if code in _409_OBSOLETO:
            return "obsoleto"
        if code in _409_PARAR:
            return "parar"
        return "fatal"
    if http in (413, 415, 422):
        return "fatal"
    return "retry"


def _code_of(result: dict) -> str:
    body = (result or {}).get("response") or {}
    return str(body.get("code") or body.get("status") or "")


class HavenQueue:
    def __init__(self, path: str):
        self.path = path
        self.corrupt_quarantined = None
        self._items = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return []
        except json.JSONDecodeError:
            # (4) corrompido: tentar quarentena; se a quarentena FALHAR, LEVANTAR
            # (não devolver [] nem permitir sobrescrever a evidência).
            quar = f"{self.path}.corrupt-{_now().strftime('%Y%m%dT%H%M%SZ')}"
            try:
                os.replace(self.path, quar)
            except OSError as ex:
                raise RuntimeError(
                    f"[haven-fila] ERRO: fila corrompida e a quarentena falhou "
                    f"({type(ex).__name__}); recuso abrir/escrever para não apagar "
                    f"a evidência em {os.path.basename(self.path)}.") from None
            self.corrupt_quarantined = quar
            print(f"[haven-fila] ERRO operacional: fila corrompida movida para "
                  f"quarentena ({os.path.basename(quar)}); a começar fila nova sem "
                  f"apagar a evidência.", flush=True)
            return []

    def _save(self):
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._items, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def enqueue(self, envelope: dict) -> bool:
        """(2) Transacional: o item só fica em memória DEPOIS de persistir."""
        eid = envelope["event_id"]
        if any(it["event_id"] == eid for it in self._items):
            return False
        rec = {
            "event_id": eid,
            "source_snapshot_at": envelope["source_snapshot_at"],
            "envelope": envelope,          # conteúdo imutável
            "status": "pending",
            "attempts": 0,
            "first_attempt_at": None,
            "next_attempt_at": _iso(_now()),
            "delayed": False,
            "last_action": None,
            "last_code": None,
        }
        self._items.append(rec)
        try:
            self._save()
        except Exception:
            self._items.pop()              # desfaz: nunca disponível sem persistir
            raise
        return True

    def due(self, now=None):
        now = now or _now()
        out = []
        for it in self._items:
            if it.get("status") != "pending":
                continue
            nxt = datetime.fromisoformat(it["next_attempt_at"].replace("Z", "+00:00"))
            if nxt <= now:
                out.append(it)
        return out

    def _backoff(self, attempts: int) -> float:
        delay = min(BASE_BACKOFF_S * (2 ** max(0, attempts - 1)), MAX_BACKOFF_S)
        delay += random.uniform(0, 0.25 * delay)
        return min(delay, MAX_BACKOFF_S)   # teto final inclui jitter

    def process(self, send_fn, now=None, max_requests=DEFAULT_MAX_REQUESTS,
                time_budget_s=DEFAULT_TIME_BUDGET_S) -> dict:
        now = now or _now()
        resumo = {"done": 0, "retry": 0, "fatal": 0, "obsoleto": 0, "parar": 0,
                  "delayed": 0, "parou_lote": False, "restantes": 0}
        start = _time.monotonic()
        feitos = 0
        novos_atrasados = 0
        for it in self.due(now):
            # (6) orçamento verificado ENTRE pedidos (não interrompe um pedido lento)
            if feitos >= max_requests or (_time.monotonic() - start) > time_budget_s:
                break
            if it["first_attempt_at"] is None:
                it["first_attempt_at"] = _iso(now)
            it["attempts"] += 1
            feitos += 1
            try:
                result = send_fn(it["envelope"])
            except Exception:
                result = {"error_code": "transport_error"}
            action = classify(result)
            it["last_action"] = action
            code = _code_of(result) if isinstance(result, dict) else ""

            if action == "done":
                it["status"] = "done"
                resumo["done"] += 1
            elif action == "fatal":
                it["status"] = "fatal"
                it["last_code"] = code        # (3) conserva o código agregado
                resumo["fatal"] += 1
            elif action == "obsoleto":
                it["status"] = "obsoleto"
                it["last_code"] = code
                resumo["obsoleto"] += 1
            elif action == "parar":
                it["attempts"] -= 1
                resumo["parar"] += 1
                resumo["parou_lote"] = True
                self._save()
                break                          # suspende o lote
            else:  # retry
                resumo["retry"] += 1
                ra = result.get("retry_after", 0) if isinstance(result, dict) else 0
                delay = max(int(ra or 0), self._backoff(it["attempts"]))
                it["next_attempt_at"] = _iso(now + timedelta(seconds=delay))
                first = datetime.fromisoformat(it["first_attempt_at"].replace("Z", "+00:00"))
                if (now - first).total_seconds() > GIVE_UP_AFTER_S:
                    if not it.get("delayed"):
                        it["delayed"] = True
                        novos_atrasados += 1
                    resumo["delayed"] += 1
            self._save()

        if novos_atrasados:
            print(f"[haven-fila] ALERTA: {novos_atrasados} entrega(s) pendente(s) há "
                  f"mais de 24h; continuam elegíveis para recuperação.", flush=True)
        resumo["restantes"] = len(self.due(now))
        self._compact()
        return resumo

    def _compact(self):
        """(3) só compacta CONFIRMADAS (done) em recibos leves. pending, fatal e
        obsoleto mantêm o registo completo (envelope/instante/código)."""
        receipts, live = [], []
        for it in self._items:
            if it.get("status") == "done":
                receipts.append({"event_id": it["event_id"], "status": "done"})
            else:
                live.append(it)
        self._items = live + receipts[-MAX_RECEIPTS:]
        self._save()

    def requeue(self, event_id: str) -> bool:
        """Retoma explícita (após corrigir credencial/config) SEM gerar novo ID:
        volta um fatal/obsoleto a pending, conservando envelope/instante/event_id."""
        it = self.get(event_id)
        if it and it.get("status") in ("fatal", "obsoleto") and it.get("envelope"):
            it["status"] = "pending"
            it["delayed"] = False
            it["next_attempt_at"] = _iso(_now())
            self._save()
            return True
        return False

    def get(self, event_id: str):
        for it in self._items:
            if it["event_id"] == event_id:
                return it
        return None

    def counts(self):
        c = {}
        for it in self._items:
            c[it.get("status")] = c.get(it.get("status"), 0) + 1
        return c
