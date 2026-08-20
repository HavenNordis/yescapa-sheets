"""
shortener.py — Lógica dos URLs curtos com marca Haven Nordis.

Transforma o URL Tally longo e feio (com hidden fields encoded) num link
curto e bonito do tipo:

    https://r.havennordis.com/3461997-tiago-cascais        (PT)
    https://r.havennordis.com/3461997-tiago-cascais-en     (EN)

O slug é `{ref}-{nome}-{ultimo_apelido}`, normalizado (minúsculas, sem
acentos). A `ref` à frente garante unicidade absoluta (cada reserva tem ID
único); o nome a seguir é puramente cosmético — o redirect (Cloudflare Worker)
resolve SÓ pela chave completa do slug, por isso typos ou caracteres estranhos
no nome nunca partem o link.

Este módulo é a single source of truth do formato. NÃO faz chamadas de rede:
- build_* → constroem strings (slug, URL curto, URL Tally longo)
- as entradas para o Cloudflare KV são produzidas por kv_entries_for_booking()
  e escritas por redirect_store.py (Vercel Edge Config)

Single source of truth: se mudares o formato do slug, mudas só aqui.
"""

import os
import re
import unicodedata
import urllib.parse

# Domínio base do shortener. Override por env var no Railway, se um dia mudar.
SHORTENER_BASE_URL = os.getenv("SHORTENER_BASE_URL", "https://r.havennordis.com").rstrip("/")

# Sufixo de idioma. PT é o default (sem sufixo); EN leva "-en".
SUFIXO_EN = "-en"


def slugify(texto: str) -> str:
    """Normaliza um texto para usar num URL: minúsculas, sem acentos,
    espaços/pontuação → hífen. Ex.: 'José Conceição' → 'jose-conceicao'."""
    if not texto:
        return ""
    # Decompõe acentos (á → a + ´) e remove as marcas combinantes.
    nfkd = unicodedata.normalize("NFKD", texto)
    sem_acentos = "".join(c for c in nfkd if not unicodedata.combining(c))
    # Tudo o que não é letra/número vira hífen; colapsa hífens repetidos.
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", sem_acentos).strip("-").lower()
    return slug


def nome_slug(nome: str, apelido: str) -> str:
    """nome + ÚLTIMO apelido, normalizado. Ex.:
        ('Tiago', 'Cascais')                              → 'tiago-cascais'
        ('Francisco', 'Serra Lopes Rebelo de Andrade')    → 'francisco-andrade'
        ('Diogo', 'Arantes F Gonçalves Da Cunha')         → 'diogo-cunha'
    Apelido vazio → só o nome. Ambos vazios → ''.
    """
    nome = (nome or "").strip()
    apelido = (apelido or "").strip()
    ultimo_apelido = apelido.split()[-1] if apelido else ""
    partes = [p for p in (nome, ultimo_apelido) if p]
    return slugify(" ".join(partes))


def build_slug(ref: str, nome: str, apelido: str, language: str = "pt") -> str:
    """Slug completo da reserva. Ex.: '3461997-tiago-cascais' (PT) ou
    '3461997-tiago-cascais-en' (EN). Sem nome → só a ref ('3461997')."""
    ref = (ref or "").strip()
    ns = nome_slug(nome, apelido)
    slug = f"{ref}-{ns}" if ns else ref
    if language and language.lower() == "en":
        slug += SUFIXO_EN
    return slug


def build_short_url(ref: str, nome: str, apelido: str, language: str = "pt",
                    base: str = None) -> str:
    """URL curto completo. Ex.: 'https://r.havennordis.com/3461997-tiago-cascais'."""
    base = (base or SHORTENER_BASE_URL).rstrip("/")
    return f"{base}/{build_slug(ref, nome, apelido, language)}"


def build_long_tally_url(booking: dict, language: str,
                         base_pt: str, base_en: str) -> str:
    """URL Tally longo com hidden fields pré-preenchidos — o DESTINO do redirect.

    Mantém os mesmos hidden fields que o sistema sempre usou
    (name, ref, vehicle, date_in, date_out), por isso a folha Checklists
    continua a cruzar submissões → reserva pela `ref`.
    """
    base_url = base_en if (language or "").lower() == "en" else base_pt
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


def kv_entries_for_booking(booking: dict, base_pt: str, base_en: str) -> dict:
    """Devolve o mapa {slug: url_tally_longo} para PT e EN de uma reserva.
    É exatamente o que a Edge Middleware do Vercel precisa de ter no Edge Config
    para redirecionar.
    """
    ref = booking.get("ref", "")
    nome = booking.get("nome", "")
    apelido = booking.get("apelido", "")
    return {
        build_slug(ref, nome, apelido, "pt"): build_long_tally_url(booking, "pt", base_pt, base_en),
        build_slug(ref, nome, apelido, "en"): build_long_tally_url(booking, "en", base_pt, base_en),
    }
