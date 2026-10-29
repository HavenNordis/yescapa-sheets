"""
haven_emitter.py — Orquestração da entrega independente ao Haven. v2 (revisão).

ESTADO: PREPARADO E DESLIGADO. Não toca nas planilhas e não envia sem ativação.

Item 4 da revisão — três resultados SEPARADOS e independentes:
  (a) captura nova VÁLIDA → enfileirada;
  (b) fluxo original das planilhas (fica no yescapa_sheets.py, inalterado);
  (c) drenagem LIMITADA da fila (reenvio de pendentes antigos).
`delivery_cycle` cobre (a) e (c): drena SEMPRE as pendentes (recuperação não
depende da captura atual nem das planilhas) e só enfileira a nova se for válida.
Nunca levanta → uma falha deste ramo não impede as planilhas nem a recuperação.

Prova positiva da extração (item 2): usa os counts CRUS da captura (chave em
falta = count não recebido → incompleto) e o sinal detail_ok (todos os detalhes
necessários obtidos). Incompleto/ inválido → não enfileira a nova, mas drena as
antigas válidas na mesma.
"""
import os
import uuid

import haven_sync as HS
import haven_queue as HQ


def _captured(yp, key: str) -> int:
    meta, state = (key.split("/", 1) + [None])[:2] if "/" in key else (key, None)
    intercepted = getattr(yp, "_intercepted", {}) or {}
    return sum(1 for b in intercepted.values()
               if b.get("meta_state") == meta and (state is None or b.get("state") == state))


def build_capture_envelope(yp, yescapa_only, snapshot_at, event_id=None):
    """Devolve (envelope, erros). envelope=None se incompleto/ inválido.
    - api_totals CRUS (getattr _api_counts) — chave em falta = não recebido.
    - captured recontado do _intercepted (dedup por id).
    - detail_ok = nenhum detalhe necessário falhou (yp.detail_failures == 0).
    """
    try:
        api_totals = dict(getattr(yp, "_api_counts", {}) or {})   # CRU (pode faltar chaves)
        captured = {k: _captured(yp, k) for k in HS.BY_STATE_KEYS}
        archived = getattr(yp, "archived_api_total", None)
        detail_ok = (getattr(yp, "detail_failures", 0) == 0)
        paginacao_ok = all(api_totals.get(k) == captured[k]
                           for k in HS.BY_STATE_KEYS if k in api_totals)
        env = HS.build_envelope(
            yescapa_only or [], api_totals, captured, archived, snapshot_at,
            event_id or uuid.uuid4().hex, detail_ok=detail_ok, pagination_ok=paginacao_ok,
        )
        erros = HS.validate_envelope(env)
    except Exception as ex:
        # Isolar: uma captura malformada NUNCA pode impedir a drenagem de pendentes.
        return None, [f"erro ao construir captura: {type(ex).__name__}"]
    if erros or not env["extraction"]["complete"]:
        return None, (erros or ["extração incompleta"])
    return env, []


def delivery_cycle(queue, send_fn, new_env=None, max_requests=None, time_budget_s=None):
    """(a) enfileira a captura nova SE válida; (c) drena pendentes (limitado).
    Independente das planilhas e da validade da captura. Nunca levanta."""
    resumo = {"enqueued": False}
    try:
        if new_env is not None:
            resumo["enqueued"] = queue.enqueue(new_env)
    except Exception as e:
        resumo["enqueue_error"] = type(e).__name__
    try:
        kwargs = {}
        if max_requests is not None:
            kwargs["max_requests"] = max_requests
        if time_budget_s is not None:
            kwargs["time_budget_s"] = time_budget_s
        resumo["drain"] = queue.process(send_fn, **kwargs)
    except Exception as e:
        resumo["drain_error"] = type(e).__name__
    return resumo
