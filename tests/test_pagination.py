"""Teste de integracao para _collect_state — paginacao via API directa.

Este teste cobre o bug do dia 18/05/2026: o sync nao apanhava a p3 do
meta_state confirmed (3 reservas em falta, incluindo Marcel #3396233).
A reescrita force pagina via API directa com page_size=10 deve apanhar
todas as paginas. Este teste protege contra regressoes.
"""

from unittest.mock import MagicMock
import re


def make_page_mock(api_responses, api_count):
    """Mock minimal de Playwright page.

    api_responses: dict {page_num: list[dict]} — bookings devolvidos por pagina.
    api_count: int — valor do campo 'count' no response.
    """
    page = MagicMock()
    page.goto.return_value = None
    page.wait_for_response.return_value = None
    page.wait_for_timeout.return_value = None

    def request_get(url, headers=None):
        m = re.search(r"page=(\d+)", url)
        page_num = int(m.group(1)) if m else 1
        bookings = api_responses.get(page_num, [])
        resp = MagicMock()
        resp.ok = True
        resp.status = 200
        resp.json.return_value = {"count": api_count, "results": bookings}
        resp.text.return_value = ""
        return resp

    page.request.get.side_effect = request_get
    return page


def test_pagination_confirmed_three_pages():
    """23 reservas confirmed espalhadas por 3 paginas (10/10/3) — apanhar todas.

    Cenario: a SPA da Yescapa pre-fetcha 2 paginas (20 bookings) no carregamento
    da p1. O nosso codigo deve detectar que faltam 3 e fazer fetch da p3.
    """
    from yescapa_sheets import YescapaPlaywright

    yp = YescapaPlaywright()
    yp._intercepted = {}
    yp._api_counts = {"confirmed": 23}
    # SPA da Yescapa devolveu URL com page=1 e page_size=10 — registado em _api_next
    yp._api_next = {"confirmed": "https://api.example.com/v4/bookings-owner/?meta_state=confirmed&page=1&page_size=10"}
    yp._api_headers = {"Authorization": "Bearer fake"}

    # Simular que a SPA ja carregou as 2 primeiras paginas (20 bookings)
    for i in range(1, 21):
        yp._intercepted[i] = {"id": i, "meta_state": "confirmed", "state": "PAID"}

    # Em producao a Yescapa devolve resultados tambem em p2 (10 items duplicados das
    # primeiras 2 paginas ja pre-fetched pela SPA). Aqui simulamos isso: p2 devolve
    # 10 items ja no _intercepted (sao ignorados pelo dedup). p3 traz os 3 novos.
    api_responses = {
        2: [{"id": i, "meta_state": "confirmed", "state": "PAID"} for i in range(11, 21)],
        3: [{"id": i, "meta_state": "confirmed", "state": "PAID"} for i in range(21, 24)],
    }
    page = make_page_mock(api_responses, api_count=23)

    yp._collect_state(page, "confirmed", None)

    confirmed = [b for b in yp._intercepted.values() if b["meta_state"] == "confirmed"]
    assert len(confirmed) == 23, f"Esperava 23 reservas confirmed, obtive {len(confirmed)}"
    # Confirma que o ID 23 (era o que faltava — Marcel-like) foi apanhado
    assert 23 in yp._intercepted, "Reserva da p3 nao foi capturada"


def test_pagination_one_page_does_not_loop():
    """5 reservas em 1 pagina — nao deve fazer fetches adicionais."""
    from yescapa_sheets import YescapaPlaywright

    yp = YescapaPlaywright()
    yp._intercepted = {}
    yp._api_counts = {"confirmed": 5}
    yp._api_next = {}
    yp._api_headers = {}

    for i in range(1, 6):
        yp._intercepted[i] = {"id": i, "meta_state": "confirmed", "state": "PAID"}

    page = make_page_mock({}, api_count=5)
    yp._collect_state(page, "confirmed", None)

    # request.get nao deve ter sido chamado porque ja temos 5/5
    page.request.get.assert_not_called()
    assert len([b for b in yp._intercepted.values() if b["meta_state"] == "confirmed"]) == 5


def test_pagination_zero_total_skips():
    """api_total=0 -> sai sem fazer paginacao."""
    from yescapa_sheets import YescapaPlaywright

    yp = YescapaPlaywright()
    yp._intercepted = {}
    yp._api_counts = {"todo": 0}
    yp._api_next = {}
    yp._api_headers = {}

    page = make_page_mock({}, api_count=0)
    yp._collect_state(page, "todo", None)

    page.request.get.assert_not_called()
    assert len(yp._intercepted) == 0


def test_pagination_archived_with_state_three_pages():
    """26 reservas archived/CANCELLED_GUEST em 3 paginas (10/10/6) — apanhar todas."""
    from yescapa_sheets import YescapaPlaywright

    yp = YescapaPlaywright()
    yp._intercepted = {}
    yp._api_counts = {"archived/CANCELLED_GUEST": 26}
    yp._api_next = {"archived/CANCELLED_GUEST": "https://api.example.com/v4/bookings-owner/?meta_state=archived&state=CANCELLED_GUEST&page=1&page_size=10"}
    yp._api_headers = {}

    # SPA carregou 10 na p1 (carregamento inicial)
    for i in range(100, 110):
        yp._intercepted[i] = {"id": i, "meta_state": "archived", "state": "CANCELLED_GUEST"}

    api_responses = {
        2: [{"id": i, "meta_state": "archived", "state": "CANCELLED_GUEST"} for i in range(110, 120)],
        3: [{"id": i, "meta_state": "archived", "state": "CANCELLED_GUEST"} for i in range(120, 126)],
    }
    page = make_page_mock(api_responses, api_count=26)
    yp._collect_state(page, "archived", "CANCELLED_GUEST")

    cancelled = [
        b for b in yp._intercepted.values()
        if b["meta_state"] == "archived" and b["state"] == "CANCELLED_GUEST"
    ]
    assert len(cancelled) == 26


def test_pagination_http_error_stops_gracefully():
    """Se a API devolver HTTP 500 numa pagina intermedia, parar sem rebentar."""
    from yescapa_sheets import YescapaPlaywright

    yp = YescapaPlaywright()
    yp._intercepted = {}
    yp._api_counts = {"confirmed": 30}
    yp._api_next = {}
    yp._api_headers = {}

    for i in range(1, 11):
        yp._intercepted[i] = {"id": i, "meta_state": "confirmed"}

    page = MagicMock()
    page.goto.return_value = None
    page.wait_for_response.return_value = None
    page.wait_for_timeout.return_value = None

    resp = MagicMock()
    resp.ok = False
    resp.status = 500
    resp.text.return_value = "Internal Server Error"
    page.request.get.return_value = resp

    # Nao deve levantar excepcao
    yp._collect_state(page, "confirmed", None)
    # Sai com as 10 que ja tinha — nao adiciona mais
    assert len(yp._intercepted) == 10
