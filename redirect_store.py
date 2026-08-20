"""
redirect_store.py — Escreve o mapa slug → URL Tally no Vercel Edge Config.

O redirect corre numa Edge Middleware do Vercel (ver pasta vercel-redirect/):
recebe /{slug}, lê o Edge Config e faz 302 para o URL Tally longo. Quem POVOA o
Edge Config é este módulo, chamado pelo cron do Railway (whatsapp_notification.py)
no momento em que prepara a mensagem.

Sem dependências novas — usa só urllib da stdlib.

Porque Vercel (e não Cloudflare): o custom domain r.havennordis.com entra com
UM registo CNAME na GoDaddy, sem mover os nameservers do domínio. O email
(Google Workspace), o site (Netlify) e o forms (Tally) ficam intactos.

Configuração (env vars no Railway):
    VERCEL_EDGE_CONFIG_ID   ID do Edge Config (ecfg_...)
    VERCEL_API_TOKEN        token de API Vercel com permissão de escrita
    VERCEL_TEAM_ID          (opcional) se o projeto estiver numa team
    SHORTENER_ENABLED       "true" para ativar (default: ativa se as 2 acima existirem)

DESIGN DE ROBUSTEZ: se não estiver configurado ou a escrita falhar, as funções
devolvem False (sem exceção). O chamador usa então o URL Tally LONGO na mensagem
— o hóspede recebe sempre um link que funciona. Nunca se envia um link curto que
ainda não existe no store.
"""

import json
import os
import urllib.error
import urllib.request

VERCEL_API_BASE = "https://api.vercel.com"


def _config() -> dict | None:
    edge_config = os.getenv("VERCEL_EDGE_CONFIG_ID", "").strip()
    token = os.getenv("VERCEL_API_TOKEN", "").strip()
    if not (edge_config and token):
        return None
    return {
        "edge_config": edge_config,
        "token": token,
        "team": os.getenv("VERCEL_TEAM_ID", "").strip(),
    }


def is_enabled() -> bool:
    """True só se o shortener estiver ligado E configurado."""
    flag = os.getenv("SHORTENER_ENABLED", "").strip().lower()
    if flag in ("false", "0", "no"):
        return False
    return _config() is not None


def put_bulk(entries: dict, timeout: int = 15) -> bool:
    """Faz upsert de {slug: url, ...} no Edge Config numa única chamada.
    Devolve True em sucesso; False (sem exceção) se não configurado ou falhar.
    """
    if not entries:
        return True
    cfg = _config()
    if cfg is None:
        return False

    url = f"{VERCEL_API_BASE}/v1/edge-config/{cfg['edge_config']}/items"
    if cfg["team"]:
        url += f"?teamId={cfg['team']}"

    items = [{"operation": "upsert", "key": str(k), "value": str(v)}
             for k, v in entries.items()]
    data = json.dumps({"items": items}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="PATCH")
    req.add_header("Authorization", f"Bearer {cfg['token']}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, TimeoutError):
        return False


def put(key: str, value: str, timeout: int = 15) -> bool:
    """Escreve uma única chave. Conveniência; o cron usa put_bulk."""
    return put_bulk({key: value}, timeout=timeout)
