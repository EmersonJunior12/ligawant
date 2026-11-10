"""Controller: endpoints JSON de busca de cartas e versões no Scryfall e preços da LigaMagic."""

from controllers.liga_errors import liga_http_error
from controllers.router import HttpError, route
from models import ligamagic, scryfall
from models import user as user_model

MAX_DETAIL_IDS = 200
# O modal de edições manda todas as impressões de uma carta (até 3 páginas do Scryfall).
MAX_LIGA_ITEMS = 600
# Cada nome distinto é uma página da Liga (com intervalo entre chamadas): limita a latência por requisição.
MAX_LIGA_NAMES = 10
_LIGA_FIELDS = (("name_en", 200), ("set_code", 20), ("collector_number", 20), ("set_name", 120))


@route("POST", "/api/settings/markets", json_api=True)
def set_markets(req):
    markets = req.json_body().get("markets")
    if not isinstance(markets, list) or len(markets) > len(user_model.PRICE_MARKETS):
        raise HttpError(400, "Envie a lista de sites de preço.")
    saved = user_model.set_markets(req.session.user_id, markets)
    req.send_json({"markets": list(saved)})


@route("POST", "/api/cards/liga", json_api=True)
def liga(req):
    body = req.json_body()
    items = body.get("cards")
    if not isinstance(items, list) or not 0 < len(items) <= MAX_LIGA_ITEMS:
        raise HttpError(400, f"Envie uma lista com até {MAX_LIGA_ITEMS} cartas.")
    cards = []
    for item in items:
        if not isinstance(item, dict):
            raise HttpError(400, "Formato de carta inválido.")
        values = []
        for field, limit in _LIGA_FIELDS:
            value = item.get(field)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise HttpError(400, "Formato de carta inválido.")
            values.append(value.strip())
        cards.append(tuple(values))
    if len({ligamagic.cache_key(c[0]) for c in cards}) > MAX_LIGA_NAMES:
        raise HttpError(400, f"No máximo {MAX_LIGA_NAMES} cartas distintas por requisição.")
    try:
        prices = ligamagic.get_prices(cards, refresh=body.get("refresh") is True)
    except ligamagic.LigaError as exc:
        raise liga_http_error(exc) from None
    req.send_json({"prices": prices})


@route("GET", "/api/cards/search", json_api=True)
def search(req):
    try:
        results = scryfall.search_names(req.query.get("q", ""))
    except scryfall.ScryfallError:
        raise HttpError(502, "Scryfall indisponível agora.") from None
    req.send_json({"results": results})


@route("POST", "/api/cards/details", json_api=True)
def details(req):
    body = req.json_body()
    ids = body.get("ids")
    if not isinstance(ids, list) or len(ids) > MAX_DETAIL_IDS:
        raise HttpError(400, f"Envie uma lista com até {MAX_DETAIL_IDS} ids.")
    if not all(isinstance(i, str) and scryfall.is_valid_scryfall_id(i) for i in ids):
        raise HttpError(400, "Identificador de carta inválido.")
    try:
        result = scryfall.get_card_details(ids, refresh=body.get("refresh") is True)
    except scryfall.ScryfallError:
        raise HttpError(502, "Scryfall indisponível agora.") from None
    req.send_json({"details": result})


@route("GET", "/api/cards/prints", json_api=True)
def prints(req):
    try:
        results = scryfall.get_prints(req.query.get("name", ""))
    except scryfall.ScryfallError:
        raise HttpError(502, "Scryfall indisponível agora.") from None
    req.send_json({"prints": results})
