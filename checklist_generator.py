"""
checklist_generator.py — Gera o PDF da checklist de preparação da autocaravana.

PDF de 2 páginas (A4 landscape):
  Página 1 — Checklist Operacional (preparação da carrinha pela equipa)
  Página 2 — Roteiro de Limpeza (para as empregadas)

Versão parametrizada validada com a reserva Walmyr (#3259112). Suporta
múltiplos layouts de camas via a chave `layout` em `data`:
  "fjord"   — Fjord / Runa / Celta (cabine 160, beliches 90, sala grande 140,
              sala pequena 70x160). Default para retro-compatibilidade.
  "krafie"  — Krafie (cama de cima elétrica 135 e cama convertível sala 150,
              ambas com roupa 140).

A roupa de cama é resolvida bed-a-bed conforme as camas escolhidas (e o kit
conforto): resguardo, lençol de baixo, edredon + capa e — quando aplicável —
extensores. Fronhas e toalhas são por número de viajantes.

Uso:
    from checklist_generator import generate_checklist_pdf
    pdf_bytes = generate_checklist_pdf(data)        # devolve bytes do PDF

`data` é um dict — ver normalize_checklist_data() para as chaves aceites.
Correr este ficheiro diretamente gera dois PDFs de exemplo (Walmyr + Krafie).
"""

import io

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm

# A4 landscape
PW, PH = A4[1], A4[0]  # 297 x 210 mm

COL = {
    "blue": "#1565C0", "blue_light": "#DDEEFF",
    "green": "#2E7D32", "green_light": "#E8F5E9",
    "orange": "#E65100", "orange_light": "#FFF3E0",
    "purple": "#6A1B9A", "purple_light": "#F3E5F5",
    "teal": "#00695C", "teal_light": "#E0F2F1",
    "dark": "#263238", "grey": "#F5F5F5", "grey2": "#E0E0E0",
    "text": "#212121", "subtext": "#616161", "white": "#FFFFFF",
    "lightblue": "#B0BEC5", "amber": "#FF6F00", "amber_light": "#FFF8E1",
}

ML, MR, MT, MB = 8 * mm, 8 * mm, 8 * mm, 8 * mm
GAP = 5 * mm
CW = (PW - ML - MR - GAP) / 2

ROW_H = 4.8 * mm
SEC_H = 5.5 * mm
SUB_H = 4.5 * mm
SP = 1 * mm


def hx(h):
    return colors.HexColor(h)


# --- Especificação das camas e layouts por viatura ------------------------
#
# BED_SPECS — para cada tipo de cama, o título e a lista de itens de roupa
# que essa cama precisa, com quantidades. As quantidades são por instância
# da cama (não por hóspede).
#
# LAYOUTS — para cada tipo de viatura, a sequência ordenada de
# (bed_id, flag_em_data) que define que camas mostrar na checklist.

BED_SPECS = {
    "cabine_160": {
        "title": "Cama cabine (casal 160×210)",
        "items": [
            ("Resguardo 160", 1),
            ("Lençol de baixo 160", 1),
            ("Edredon + capa casal", 1),
        ],
    },
    "beliches_90": {
        "title": "Beliches (2 individuais 90×210)",
        "items": [
            ("Resguardo 90", 2),
            ("Lençol de baixo 90", 2),
            ("Edredon + capa solteiro", 2),
        ],
    },
    "sala_grande_140": {
        "title": "Cama convertível sala grande (casal 140)",
        "items": [
            ("Extensor (meio)", 1),
            ("Resguardo 140", 1),
            ("Lençol de baixo 140", 1),
            ("Edredon + capa casal", 1),
        ],
    },
    "sala_pequena_70x160": {
        "title": "Cama convertível sala pequena (criança 70×160)",
        "items": [
            ("Extensor", 1),
            ("Resguardo 70×160", 1),
            ("Lençol de baixo 70×160", 1),
            ("Edredon + capa solteiro", 1),
        ],
    },
    "cima_eletrica_135": {
        "title": "Cama de cima elétrica (casal 135×200)",
        "items": [
            ("Resguardo 140", 1),
            ("Lençol de baixo 140", 1),
            ("Edredon + capa casal", 1),
        ],
    },
    "convertivel_sala_150": {
        "title": "Cama convertível sala (casal 150×175)",
        "items": [
            ("Resguardo 140", 1),
            ("Lençol de baixo 140", 1),
            ("Edredon + capa casal", 1),
        ],
    },
}

LAYOUTS = {
    "fjord": [
        ("cabine_160", "cama_cabine"),
        ("beliches_90", "beliches"),
        ("sala_grande_140", "sala_grande"),
        ("sala_pequena_70x160", "sala_pequena"),
    ],
    "krafie": [
        ("cima_eletrica_135", "cama_cima_eletrica"),
        ("convertivel_sala_150", "cama_convertivel_sala"),
    ],
}


# --- Normalização de dados ------------------------------------------------

def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "sim", "yes", "y", "s", "x")


def _as_int(v, default=0) -> int:
    try:
        return int(float(str(v).strip()))
    except (ValueError, TypeError):
        return default


def normalize_checklist_data(raw: dict) -> dict:
    """Devolve um dict completo com defaults aplicados.

    Chaves de entrada aceites (todas opcionais):
      cliente_nome, reserva_ref, viatura, pickup, dropoff, num_viajantes,
      paises, kit_conforto, layout (default "fjord"),
      cama_cabine, beliches, sala_grande, sala_grande_extensores, sala_pequena,
      cama_cima_eletrica, cama_convertivel_sala,
      microondas, maquina_cafe, mesa_exterior, num_cadeiras, num_bancos,
      via_verde, cadeira_auto, notas
    """
    raw = raw or {}
    viajantes = _as_int(raw.get("num_viajantes"), 0)
    notas = [str(n).strip() for n in (raw.get("notas") or []) if str(n).strip()]
    layout = str(raw.get("layout") or "fjord").strip().lower()
    if layout not in LAYOUTS:
        layout = "fjord"

    return {
        "cliente_nome": str(raw.get("cliente_nome", "") or "").strip() or "—",
        "reserva_ref": str(raw.get("reserva_ref", "") or "").strip() or "—",
        "viatura": str(raw.get("viatura", "") or "").strip() or "—",
        "pickup": str(raw.get("pickup", "") or "").strip() or "—",
        "dropoff": str(raw.get("dropoff", "") or "").strip() or "—",
        "num_viajantes": viajantes,
        "paises": str(raw.get("paises", "") or "").strip() or "—",
        "kit_conforto": _as_bool(raw.get("kit_conforto")),
        "layout": layout,
        # Fjord beds
        "cama_cabine": _as_bool(raw.get("cama_cabine")),
        "beliches": _as_bool(raw.get("beliches")),
        "sala_grande": _as_bool(raw.get("sala_grande")),
        "sala_grande_extensores": _as_int(raw.get("sala_grande_extensores"), 1),
        "sala_pequena": _as_bool(raw.get("sala_pequena")),
        # Krafie beds
        "cama_cima_eletrica": _as_bool(raw.get("cama_cima_eletrica")),
        "cama_convertivel_sala": _as_bool(raw.get("cama_convertivel_sala")),
        # Cozinha / serviços / exterior
        "microondas": _as_bool(raw.get("microondas")),
        "maquina_cafe": _as_bool(raw.get("maquina_cafe")),
        "mesa_exterior": _as_bool(raw.get("mesa_exterior")),
        "num_cadeiras": _as_int(raw.get("num_cadeiras"), 0),
        "num_bancos": _as_int(raw.get("num_bancos"), 0),
        "via_verde": _as_bool(raw.get("via_verde")),
        "cadeira_auto": _as_bool(raw.get("cadeira_auto")),
        "notas": notas,
        # Totais por hóspede (fronhas + toalhas) — design v2.0
        "total_fronhas": viajantes,
        "total_toalhas_grandes": viajantes,
        "total_toalhas_rosto": viajantes,
    }


# --- Helpers de desenho ---------------------------------------------------

def _set_font(c, bold=False, size=8):
    c.setFont("Helvetica-Bold" if bold else "Helvetica", size)


def _filled_rect(c, x, y, w, h, fill, radius=0):
    c.setFillColor(hx(fill))
    if radius:
        c.roundRect(x, y, w, h, radius, fill=1, stroke=0)
    else:
        c.rect(x, y, w, h, fill=1, stroke=0)


def _text(c, x, y, txt, size=8, bold=False, color="#212121", align="left"):
    _set_font(c, bold, size)
    c.setFillColor(hx(color))
    if align == "center":
        c.drawCentredString(x, y, txt)
    elif align == "right":
        c.drawRightString(x, y, txt)
    else:
        c.drawString(x, y, txt)


def _section(c, x, cy, w, label, bg):
    _filled_rect(c, x, cy - SEC_H, w, SEC_H, bg, radius=3)
    _text(c, x + 5 * mm, cy - SEC_H + 1.8 * mm, label, size=8, bold=True, color=COL["white"])
    return cy - SEC_H - SP


def _sub(c, x, cy, w, label, color):
    c.setFillColor(hx(COL["white"]))
    c.rect(x, cy - SUB_H, w, SUB_H, fill=1, stroke=0)
    _text(c, x + 3 * mm, cy - SUB_H + 1.5 * mm, label, size=7, bold=True, color=color)
    c.setStrokeColor(hx(color))
    c.setLineWidth(0.8)
    c.line(x, cy - SUB_H, x + w, cy - SUB_H)
    return cy - SUB_H


def _items(c, x, cy, w, items, bg_light):
    for i, item in enumerate(items):
        bg = bg_light if i % 2 == 0 else COL["white"]
        _filled_rect(c, x, cy - ROW_H, w, ROW_H, bg)
        c.setStrokeColor(hx(COL["grey2"]))
        c.setLineWidth(0.2)
        c.rect(x, cy - ROW_H, w, ROW_H, fill=0, stroke=1)
        _text(c, x + 2 * mm, cy - ROW_H + 1.5 * mm, "☐", size=8, color=COL["subtext"])
        _text(c, x + 7 * mm, cy - ROW_H + 1.5 * mm, item, size=7.5, color=COL["text"])
        cy -= ROW_H
    return cy - SP


def _header(c, title, subtitle):
    y = PH - MT
    hdr_h = 14 * mm
    _filled_rect(c, ML, y - hdr_h, PW - ML - MR, hdr_h, COL["dark"], radius=4)
    _text(c, PW / 2, y - 6 * mm, title, size=13, bold=True, color=COL["white"], align="center")
    _text(c, PW / 2, y - 10.5 * mm, subtitle, size=8, color=COL["lightblue"], align="center")
    return y - hdr_h


# --- Resolução dos campos condicionais em texto ---------------------------

def _bed_lines(d: dict) -> list:
    """Constrói as linhas de roupa de cama para a checklist.

    Cada cama escolhida produz 2 linhas:
      1) nome da cama
      2) itens de roupa separados por · (ponto-mediano)
    Camas não escolhidas viram 1 linha "<nome> — N/A".
    Sem kit conforto vira 1 linha "<nome> — hóspede traz a sua roupa".

    Quantidades:
      - 1× → só o nome do item ("Resguardo 160")
      - 2× ou mais → prefixado ("2× Resguardo 90")
    O Fjord sala grande acrescenta "+ 2× Extensor lateral" quando o hóspede
    escolhe a opção com os 2 extensores laterais.
    """
    layout = LAYOUTS.get(d.get("layout", "fjord"), LAYOUTS["fjord"])
    lines = []
    for bed_id, flag in layout:
        spec = BED_SPECS[bed_id]
        title = spec["title"]
        chosen = bool(d.get(flag))
        if not chosen:
            lines.append(f"{title} — N/A")
            continue
        if not d.get("kit_conforto"):
            lines.append(f"{title} — hóspede traz a sua roupa")
            continue
        parts = [f"{qty}× {name}" if qty > 1 else name for name, qty in spec["items"]]
        if bed_id == "sala_grande_140" and _as_int(d.get("sala_grande_extensores"), 1) >= 2:
            parts.append("2× Extensor lateral")
        lines.append(title)
        lines.append("   " + " · ".join(parts))
    return lines


def _towel_lines(d: dict) -> list:
    """Fronhas + toalhas, sempre 1 por hóspede."""
    if not d.get("kit_conforto"):
        return [
            "Fronhas — hóspede traz  |  Toalhas — hóspede traz",
        ]
    n = d["num_viajantes"]
    return [
        f"Fronhas — {n}  |  Toalhas grandes — {n}  |  Toalhas rosto — {n}",
    ]


# --- Página 1 — Checklist Operacional -------------------------------------

def _draw_page1(c, d: dict):
    y = _header(
        c, "CHECKLIST OPERACIONAL",
        f"{d['cliente_nome']}  ·  Ref. #{d['reserva_ref']}  ·  {d['viatura']}",
    )
    y -= 2 * mm

    # Tabela de info
    info_h = 16 * mm
    _filled_rect(c, ML, y - info_h, PW - ML - MR, info_h, COL["grey"])
    c.setStrokeColor(hx(COL["grey2"]))
    c.setLineWidth(0.3)
    c.rect(ML, y - info_h, PW - ML - MR, info_h, fill=0, stroke=1)

    def sn(flag, na="✘ Não"):
        return "✔ Sim" if flag else na

    info = [
        [("Pick-up", d["pickup"]), ("Drop-off", d["dropoff"]),
         ("Viajantes", str(d["num_viajantes"])), ("Países", d["paises"]), None],
        [("Via Verde", sn(d["via_verde"])), ("Kit conforto", sn(d["kit_conforto"])),
         ("Micro-ondas", sn(d["microondas"])), ("Máquina de café", sn(d["maquina_cafe"])),
         ("Cadeira auto", sn(d["cadeira_auto"], na="✘ N/A"))],
    ]
    col5 = (PW - ML - MR) / 5
    row_h = info_h / 2
    for ri, row in enumerate(info):
        ry = y - (ri + 1) * row_h + 1.5 * mm
        for ci, item in enumerate(row):
            if not item:
                continue
            label, val = item
            rx = ML + ci * col5 + 2 * mm
            _text(c, rx, ry + 2.5 * mm, label, size=7, color=COL["subtext"])
            _text(c, rx, ry, val, size=7.5, bold=True, color=COL["text"])
    y -= info_h + 3 * mm

    # Coluna esquerda
    cx, cy = ML, y
    cy = _section(c, cx, cy, CW, "🔵  SACO AZUL — ROUPA", COL["blue"])
    cy = _sub(c, cx, cy, CW, "Roupa de Cama (conforme camas escolhidas)", COL["blue"])
    cy = _items(c, cx, cy, CW, _bed_lines(d), COL["blue_light"])
    cy = _sub(c, cx, cy, CW, f"Fronhas e Toalhas ({d['num_viajantes']} hóspedes)", COL["blue"])
    cy = _items(c, cx, cy, CW, _towel_lines(d), COL["blue_light"])

    cy -= 2 * mm
    cy = _section(c, cx, cy, CW, "🟢  SACO VERDE — CONSUMÍVEIS + DOCS + VIA VERDE", COL["green"])
    cy = _sub(c, cx, cy, CW, "Consumíveis", COL["green"])
    cy = _items(c, cx, cy, CW, [
        "Papel higiénico — 3 rolos",
        "Sabão para as mãos (refill)",
        "Esponja da loiça",
        "Panos da loiça",
        "Detergente da loiça",
        "Sacos lixo 30L",
        "Pano de limpeza",
        "Pastilhas sanita química",
    ] + (["Café (cápsulas/pó para máquina)"] if d["maquina_cafe"] else []), COL["green_light"])
    cy = _sub(c, cx, cy, CW, "Documentos (impressos)", COL["green"])
    cy = _items(c, cx, cy, CW, [
        "Contrato de aluguer impresso",
        "Certificado de seguro impresso",
    ], COL["green_light"])
    cy = _sub(c, cx, cy, CW, "Serviços", COL["green"])
    cy = _items(c, cx, cy, CW, [
        "Dispositivo Via Verde" if d["via_verde"] else "Dispositivo Via Verde — N/A",
    ], COL["green_light"])

    cy -= 2 * mm
    cy = _section(c, cx, cy, CW, "🟣  KIT EXTERIOR", COL["purple"])
    if d["mesa_exterior"]:
        kit = f"Mesa exterior — 1  |  Cadeiras — {d['num_cadeiras']}  |  Bancos — {d['num_bancos']}"
    else:
        kit = "Kit exterior — N/A"
    _items(c, cx, cy, CW, [kit], COL["purple_light"])

    # Coluna direita
    cx, cy = ML + CW + GAP, y
    cy = _section(c, cx, cy, CW, "🟠  SACO LARANJA — LIMPEZA", COL["orange"])
    cy = _items(c, cx, cy, CW, [
        "Aspirador", "Desengordurante", "Vassoura", "Pá",
        "Limpa vidros", "Papel toalha", "Pano de limpeza",
    ], COL["orange_light"])

    cy -= 2 * mm
    cy = _section(c, cx, cy, CW, "✅  CONFIRMAÇÕES — AUTOCARAVANA", COL["dark"])
    cy = _items(c, cx, cy, CW, [
        "Comando da TV presente",
        "Comando do ar condicionado (AC) presente",
        "Hotspot presente + cabo carregador",
        "Extintor + manta + primeiros socorros — presentes",
        "Triângulo + colete refletor — presentes",
        "Extensão tripla — presente",
        "Micro-ondas — presente e limpo" if d["microondas"] else "Micro-ondas — N/A",
        "Máquina de café — presente e limpa" if d["maquina_cafe"] else "Máquina de café — N/A",
        "Cadeira auto — presente" if d["cadeira_auto"] else "Cadeira auto — N/A",
    ], COL["grey"])
    cy = _sub(c, cx, cy, CW, "Garagem", COL["dark"])
    cy = _items(c, cx, cy, CW, [
        "Calços",
        "Mangueira + adaptador",
        "Extensão elétrica + 2 adaptadores",
        "Alguidares — 2",
        "Manivela do toldo + fixador",
    ], COL["grey"])

    cy -= 2 * mm
    cy = _section(c, cx, cy, CW, "💧  ÁGUA / WC / GÁS / LIMPEZA (execução)", COL["dark"])
    cy = _items(c, cx, cy, CW, [
        "Água limpa: Cheio",
        "Cinzentas: Vazio",
        "Cassete sanita química: Limpa + Pastilhas",
        "Gás: Botija cheia",
        "Frigorífico: Ligar",
    ], COL["grey"])

    _text(c, PW / 2, MB,
          f"Checklist gerada automaticamente · {d['viatura']} · Reserva #{d['reserva_ref']}",
          size=6.5, color=COL["lightblue"], align="center")


# --- Página 2 — Roteiro de Limpeza ----------------------------------------

def _draw_page2(c, d: dict):
    y = _header(
        c, "ROTEIRO DE LIMPEZA — EMPREGADAS",
        f"{d['cliente_nome']}  ·  Ref. #{d['reserva_ref']}  ·  {d['viatura']}"
        "  |  Verso da Checklist de Preparação",
    )
    y -= 3 * mm

    # Coluna esquerda
    cx, cy = ML, y
    cy = _section(c, cx, cy, CW, "🛏️  QUARTOS / CAMAS", COL["blue"])
    cy = _items(c, cx, cy, CW, [
        "Retirar roupa suja",
        "Pó nas prateleiras e cabeceiras",
        "Aspirar debaixo das camas",
        "Fazer as camas (roupa de cama do saco azul)",
        "Verificar redes de segurança infantil — ok?",
        "Extensor + roupa cama convertível → guardar no roupeiro",
        "Tapete do corredor",
    ], COL["blue_light"])

    cy -= 2 * mm
    cy = _section(c, cx, cy, CW, "🍳  COZINHA", COL["orange"])
    cy = _items(c, cx, cy, CW, [
        "Limpar frigorífico (interior e exterior)",
        "Limpar fogão, bancada (retirar placa)",
        "Limpar gavetas da cozinha",
        "Contagem: talheres, pratos, copos, copos café",
        "Contagem: utensílios e panelas",
        "Colocar saco do lixo na porta",
        "Repor consumíveis de cozinha",
        "Verificar micro-ondas — limpo e a funcionar" if d["microondas"]
        else "Micro-ondas — N/A",
        "Verificar máquina de café — limpa e a funcionar" if d["maquina_cafe"]
        else "Máquina de café — N/A",
    ], COL["orange_light"])

    # Coluna direita
    cx, cy = ML + CW + GAP, y
    cy = _section(c, cx, cy, CW, "🚿  WC / CASA DE BANHO", COL["teal"])
    cy = _items(c, cx, cy, CW, [
        "Repor papel higiénico",
        "Colocar sabão para as mãos (refill)",
        "Limpar e desinfetar sanita",
        "Limpar duche (box, torneiras, ralo)",
        "Limpar espelho e pia",
    ], COL["teal_light"])

    cy -= 2 * mm
    cy = _section(c, cx, cy, CW, "✅  CONFIRMAÇÕES FINAIS", COL["dark"])
    cy = _items(c, cx, cy, CW, [
        "Comando TV — presente e a funcionar",
        "Hotspot — presente + cabo carregador",
        "Comando ar condicionado (AC) — presente",
        "Documentos: contrato + seguro — presentes",
        "Dispositivo Via Verde — presente" if d["via_verde"]
        else "Dispositivo Via Verde — N/A",
        "Extintor + manta + primeiros socorros — presentes",
        "Triângulo + colete refletor — presentes",
        "Extensão tripla — presente",
        "Testar blackouts — funcionam corretamente?",
        "Micro-ondas — presente e limpo" if d["microondas"] else "Micro-ondas — N/A",
        "Máquina de café — presente e limpa" if d["maquina_cafe"] else "Máquina de café — N/A",
        "Cadeira auto — presente" if d["cadeira_auto"] else "Cadeira auto — N/A",
    ], COL["grey"])

    cy -= 2 * mm
    cy = _section(c, cx, cy, CW, "📝  NOTAS DO SISTEMA", COL["amber"])
    note_h = ROW_H
    _filled_rect(c, cx, cy - note_h * 4, CW, note_h * 4, COL["amber_light"])
    c.setStrokeColor(hx(COL["amber"]))
    c.setLineWidth(0.5)
    c.setDash(3, 2)
    c.rect(cx, cy - note_h * 4, CW, note_h * 4, fill=0, stroke=1)
    c.setDash()
    notas = (d["notas"] + ["", "", "", ""])[:4]
    for i, nota in enumerate(notas):
        txt = f"◈  {nota}" if nota else "◈  —"
        _text(c, cx + 3 * mm, cy - note_h * (i + 1) + 1.5 * mm, txt,
              size=7.5, color=COL["amber"], bold=True)

    _text(c, ML, MB,
          "◈ = preenchido automaticamente pelo sistema  ·  ☐ = a confirmar na preparação",
          size=6.5, color=COL["amber"])
    _text(c, PW / 2, MB, "Roteiro de Limpeza Autocaravana · Haven Nordis",
          size=6.5, color=COL["lightblue"], align="center")


# --- API pública ----------------------------------------------------------

def generate_checklist_pdf(data: dict) -> bytes:
    """Gera o PDF de 2 páginas e devolve os bytes."""
    d = normalize_checklist_data(data)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(PW, PH))
    c.setTitle(f"Checklist #{d['reserva_ref']} — {d['cliente_nome']}")
    _draw_page1(c, d)
    c.showPage()
    _draw_page2(c, d)
    c.showPage()
    c.save()
    return buf.getvalue()


def save_checklist_pdf(data: dict, path: str) -> str:
    """Gera o PDF e grava-o no caminho indicado."""
    pdf = generate_checklist_pdf(data)
    with open(path, "wb") as f:
        f.write(pdf)
    return path


# Dados de exemplo — reserva Walmyr #3259112 (Fjord, modelo de validação v1.0).
EXEMPLO_WALMYR = {
    "cliente_nome": "Walmyr",
    "reserva_ref": "3259112",
    "viatura": "CF-68-JJ",
    "pickup": "06/05/2026 às 09:00",
    "dropoff": "21/05/2026 às 20:00",
    "num_viajantes": 6,
    "paises": "Portugal + Espanha",
    "kit_conforto": True,
    "layout": "fjord",
    "cama_cabine": True,
    "beliches": True,
    "sala_grande": True,
    "sala_grande_extensores": 1,
    "sala_pequena": False,
    "microondas": True,
    "maquina_cafe": False,
    "mesa_exterior": True,
    "num_cadeiras": 3,
    "num_bancos": 3,
    "via_verde": True,
    "cadeira_auto": False,
    "notas": [
        "Hóspede chega depois das 09:00 — confirmar hora exata por WhatsApp.",
        "Família com 2 crianças — verificar redes de segurança nos beliches.",
    ],
}

# Dados de exemplo — reserva Krafie (validação do layout krafie).
EXEMPLO_KRAFIE = {
    "cliente_nome": "Exemplo Krafie",
    "reserva_ref": "3500000",
    "viatura": "52-US-19",
    "pickup": "10/07/2026 às 10:00",
    "dropoff": "17/07/2026 às 18:00",
    "num_viajantes": 4,
    "paises": "Portugal",
    "kit_conforto": True,
    "layout": "krafie",
    "cama_cima_eletrica": True,
    "cama_convertivel_sala": True,
    "microondas": True,
    "maquina_cafe": True,
    "mesa_exterior": True,
    "num_cadeiras": 4,
    "num_bancos": 0,
    "via_verde": True,
    "cadeira_auto": False,
    "notas": ["Família com 2 adultos + 2 crianças — confirmar idades."],
}


if __name__ == "__main__":
    out = save_checklist_pdf(EXEMPLO_WALMYR, "checklist_exemplo_walmyr.pdf")
    print(f"PDF Fjord (Walmyr) gerado: {out}")
    out = save_checklist_pdf(EXEMPLO_KRAFIE, "checklist_exemplo_krafie.pdf")
    print(f"PDF Krafie gerado: {out}")
