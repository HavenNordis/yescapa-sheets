"""
links_config.py — Configuração centralizada dos Guias de Funcionamento.

Single source of truth para o link YouTube de cada autocaravana × idioma,
partilhada entre pre_check_in_sender.py e whatsapp_notification.py.

REGRA DE OURO: o link existe APENAS aqui. Se gravares novo vídeo, edita só
este ficheiro e os dois canais (email + WhatsApp) ficam alinhados.

Estrutura: (matrícula, idioma) → video_id YouTube.
Os 4 vídeos atuais (2026-05-30):
  - Fjord/Runa/Celta PT: pEwfTeeLwGc
  - Fjord/Runa/Celta EN: VXnCT8dLDus
  - Krafie PT:           OKneOLVQM3c
  - Krafie EN:           q3k37lBj554
"""

import os

# Idiomas suportados pelo sistema (PT e EN).
IDIOMAS_VALIDOS = ("pt", "en")

# Override individual via env var, se quiseres apontar para outro vídeo sem
# tocar no código (ex.: testar uma nova versão em staging).
GUIAS_VIDEO_ID = {
    ("CH-61-GD", "pt"): os.getenv("GUIA_VIDEO_RUNA_PT",   "pEwfTeeLwGc"),  # Runa PT
    ("CH-61-GD", "en"): os.getenv("GUIA_VIDEO_RUNA_EN",   "VXnCT8dLDus"),  # Runa EN
    ("CF-68-JJ", "pt"): os.getenv("GUIA_VIDEO_FJORD_PT",  "pEwfTeeLwGc"),  # Fjord PT
    ("CF-68-JJ", "en"): os.getenv("GUIA_VIDEO_FJORD_EN",  "VXnCT8dLDus"),  # Fjord EN
    ("CE-60-LH", "pt"): os.getenv("GUIA_VIDEO_CELTA_PT",  "pEwfTeeLwGc"),  # Celta PT
    ("CE-60-LH", "en"): os.getenv("GUIA_VIDEO_CELTA_EN",  "VXnCT8dLDus"),  # Celta EN
    ("52-US-19", "pt"): os.getenv("GUIA_VIDEO_KRAFIE_PT", "OKneOLVQM3c"),  # Krafie PT
    ("52-US-19", "en"): os.getenv("GUIA_VIDEO_KRAFIE_EN", "q3k37lBj554"),  # Krafie EN
}

# Fallback usado quando a matrícula não está no mapa.
# Vídeo padrão = Fjord/Runa/Celta (que cobre a maior parte da frota).
VIDEO_ID_FALLBACK_PT = GUIAS_VIDEO_ID[("CF-68-JJ", "pt")]
VIDEO_ID_FALLBACK_EN = GUIAS_VIDEO_ID[("CF-68-JJ", "en")]


def _normalizar(matricula: str, idioma: str) -> tuple[str, str]:
    return (matricula or "").strip().upper(), (idioma or "pt").strip().lower()


def video_id(matricula: str, idioma: str = "pt") -> str:
    """Devolve o video_id YouTube para (matrícula, idioma).
    Idioma desconhecido → 'pt'. Matrícula desconhecida → fallback.
    """
    mat, lang = _normalizar(matricula, idioma)
    if lang not in IDIOMAS_VALIDOS:
        lang = "pt"
    if (mat, lang) in GUIAS_VIDEO_ID:
        return GUIAS_VIDEO_ID[(mat, lang)]
    return VIDEO_ID_FALLBACK_PT if lang == "pt" else VIDEO_ID_FALLBACK_EN


def link_guia(matricula: str, idioma: str = "pt") -> str:
    """URL completo. Ex.: https://youtu.be/pEwfTeeLwGc"""
    return f"https://youtu.be/{video_id(matricula, idioma)}"


def link_guia_curto(matricula: str, idioma: str = "pt") -> str:
    """Versão curta para texto visível. Ex.: youtu.be/pEwfTeeLwGc"""
    return f"youtu.be/{video_id(matricula, idioma)}"


def link_guia_thumb(matricula: str, idioma: str = "pt") -> str:
    """URL da thumbnail YouTube. Ex.: https://img.youtube.com/vi/<id>/maxresdefault.jpg"""
    return f"https://img.youtube.com/vi/{video_id(matricula, idioma)}/maxresdefault.jpg"


def todas_as_variaveis_guia(matricula: str) -> dict:
    """Devolve as 6 variáveis prontas para injetar num template bilingue:
        link_guia_pt, link_guia_pt_curto, link_guia_pt_thumb,
        link_guia_en, link_guia_en_curto, link_guia_en_thumb
    Cobre simultaneamente o bloco PT e o bloco EN do mesmo email.
    """
    vid_pt = video_id(matricula, "pt")
    vid_en = video_id(matricula, "en")
    return {
        "link_guia_pt":       f"https://youtu.be/{vid_pt}",
        "link_guia_pt_curto": f"youtu.be/{vid_pt}",
        "link_guia_pt_thumb": f"https://img.youtube.com/vi/{vid_pt}/maxresdefault.jpg",
        "link_guia_en":       f"https://youtu.be/{vid_en}",
        "link_guia_en_curto": f"youtu.be/{vid_en}",
        "link_guia_en_thumb": f"https://img.youtube.com/vi/{vid_en}/maxresdefault.jpg",
    }
