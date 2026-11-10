"""Controller: "Buscar em coleção" — cruza uma coleção pública da LigaMagic com as listas do usuário.

O navegador conduz em duas fases curtas (cada requisição cabe folgada no servidor com threads):
1. GET /api/collection/page, página a página: lê a coleção e devolve só as linhas cujo nome está
   em alguma lista, junto das cartas da lista que casaram.
2. POST /api/collection/compare, em lotes de até MAX_COMPARE_NAMES nomes: mesma edição ou não,
   e os preços da Liga (uma página de carta por nome ainda fora do cache).
"""

from controllers.liga_errors import liga_http_error
from controllers.router import HttpError, route
from models import liga_collection, ligamagic
from models import want_list as want_list_model
from views.renderer import render_collection

# Mesmo motivo do MAX_LIGA_NAMES do api_controller: cada nome é uma página da Liga com intervalo entre chamadas.
MAX_COMPARE_NAMES = 10
MAX_COMPARE_ITEMS = 60
MAX_WANTS_PER_ITEM = 30
_WANT_FIELDS = (("name_en", 200), ("set_code", 20), ("collector_number", 20), ("set_name", 120))


@route("GET", "/collection")
def page(req):
    req.send_html(render_collection(req.session))


@route("GET", "/api/collection/page", json_api=True)
def collection_page(req):
    try:
        collection_id = liga_collection.parse_collection_id(req.query.get("collection", ""))
        page_number = int(req.query.get("page", "1"))
        data = liga_collection.get_page(collection_id, page_number)
    except ValueError as exc:  # InvalidCollection é ValueError; int() inválido também
        message = str(exc) if isinstance(exc, liga_collection.InvalidCollection) else "Página inválida."
        raise HttpError(400, message) from None
    except liga_collection.CollectionNotFound as exc:
        raise HttpError(404, str(exc)) from None
    except ligamagic.LigaError as exc:
        raise liga_http_error(exc) from None

    user_id = req.session.user_id
    list_names = {s.id: s.name for s in want_list_model.list_summaries(user_id)}
    matches = liga_collection.match_rows(data["rows"], want_list_model.all_cards(user_id), list_names)
    # Coleção maior que o teto: lê só as primeiras MAX_PAGES (o navegador avisa que ficou parcial).
    req.send_json({
        "collection_id": collection_id,
        "page": page_number,
        "pages": min(data["pages"], liga_collection.MAX_PAGES),
        "pages_total": data["pages"],
        "title": data["title"],
        "total_rows": data["total_rows"],
        "total_cards": data["total_cards"],
        "rows_in_page": len(data["rows"]),
        "matches": matches,
    })


def _text(value, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise HttpError(400, "Formato de carta inválido.")
    return value.strip()


def _price(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 1_000_000:
        raise HttpError(400, "Formato de carta inválido.")
    return round(float(value), 2)


@route("POST", "/api/collection/compare", json_api=True)
def compare(req):
    body = req.json_body()
    raw_items = body.get("items")
    if not isinstance(raw_items, list) or not 0 < len(raw_items) <= MAX_COMPARE_ITEMS:
        raise HttpError(400, f"Envie de 1 a {MAX_COMPARE_ITEMS} cartas.")

    items = []
    for raw in raw_items:
        if not isinstance(raw, dict) or not isinstance(raw.get("wants"), list) \
                or not 0 < len(raw["wants"]) <= MAX_WANTS_PER_ITEM:
            raise HttpError(400, "Formato de carta inválido.")
        wants = []
        for want in raw["wants"]:
            if not isinstance(want, dict):
                raise HttpError(400, "Formato de carta inválido.")
            wants.append({field: _text(want.get(field), limit) for field, limit in _WANT_FIELDS})
        items.append({
            "ed": _text(raw.get("ed"), 20).lower(),
            "number": _text(raw.get("number"), 20).lower(),
            "foil": raw.get("foil") is True,
            "price_buy": _price(raw.get("price_buy")),
            "wants": wants,
        })

    names = {ligamagic.cache_key(w["name_en"]) for item in items for w in item["wants"]}
    if len(names) > MAX_COMPARE_NAMES:
        raise HttpError(400, f"No máximo {MAX_COMPARE_NAMES} cartas distintas por requisição.")
    try:
        results = liga_collection.compare(items)
    except ligamagic.LigaError as exc:
        raise liga_http_error(exc) from None
    req.send_json({"results": results})
