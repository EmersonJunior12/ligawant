"""Controller: páginas de lista, salvar, excluir e geração do JPG."""

import re
import unicodedata

from controllers.router import HttpError, route
from models import ligamagic, scryfall
from models import want_list as want_list_model
from models.user import parse_markets
from models.want_list import ValidationError
from views.image_renderer import render_want_list
from views.renderer import render_editor, render_lists


def _owned_list(req, list_id: str) -> want_list_model.WantList:
    want_list = want_list_model.get_list(req.session.user_id, int(list_id))
    if want_list is None:
        # 404 também para lista de outro usuário: não revela que o id existe.
        raise HttpError(404, "Lista não encontrada")
    return want_list


def _slug(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-") or "lista"


def _merge_liga_prices(want_list: want_list_model.WantList, prices: dict[str, dict]) -> None:
    """Acrescenta prices[id]["liga"]. Normalmente vem do cache (o editor já carregou); se a Liga
    falhar, o JPG sai do mesmo jeito, com a Liga sem preço."""
    cards = want_list.cards
    try:
        liga = ligamagic.get_prices((c.name_en, c.set_code, c.collector_number, c.set_name) for c in cards)
    except ligamagic.LigaError as exc:
        print(f"[liga] JPG sem preços da Liga: {exc}")
        liga = [ligamagic.empty_price() for _ in cards]
    for card, price in zip(cards, liga):
        prices.setdefault(card.scryfall_id, {}).setdefault("liga", price)


@route("GET", "/api/lists/summaries", json_api=True)
def summaries(req):
    """Total de cada lista por site de preço ligado: {"lists": {id: {mercado: {value, missing}}}}.

    TCG/CM vêm do Scryfall (lote de 75, cache de 10 min). A Liga vem só do cache: o resumo
    nunca dispara busca no site; carta que nunca foi aberta no editor conta como sem preço.
    """
    markets = req.session.markets
    by_list = want_list_model.all_cards(req.session.user_id)
    every_card = [card for cards in by_list.values() for card in cards]
    prices: dict[str, dict] = {}

    scryfall_markets = [m for m in markets if m != "liga"]
    if scryfall_markets and every_card:
        try:
            details = scryfall.get_card_details(c.scryfall_id for c in every_card)
        except scryfall.ScryfallError:
            raise HttpError(502, "Scryfall indisponível agora.") from None
        for card_id, detail in details.items():
            prices[card_id] = dict(detail["prices"])

    liga_by_card: list[dict] = []
    if "liga" in markets and every_card:
        liga_by_card = ligamagic.cached_prices(
            (c.name_en, c.set_code, c.collector_number, c.set_name) for c in every_card)
    liga_iter = iter(liga_by_card)

    result = {}
    for list_id, cards in by_list.items():
        totals = {m: {"value": 0.0, "missing": 0} for m in markets}
        for card in cards:
            liga = next(liga_iter) if liga_by_card else None
            for market in markets:
                price = liga if market == "liga" else prices.get(card.scryfall_id, {}).get(market)
                value = (price or {}).get("value")
                if value is None:
                    totals[market]["missing"] += card.quantity
                else:
                    totals[market]["value"] += value * card.quantity
        for total in totals.values():
            total["value"] = round(total["value"], 2)
        result[str(list_id)] = totals
    req.send_json({"markets": list(markets), "lists": result})


@route("GET", "/lists")
def index(req):
    req.send_html(render_lists(req.session, want_list_model.list_summaries(req.session.user_id)))


@route("GET", "/lists/new")
def new(req):
    req.send_html(render_editor(req.session, None))


@route("GET", r"/lists/(?P<list_id>\d{1,9})")
def edit(req, list_id: str):
    req.send_html(render_editor(req.session, _owned_list(req, list_id)))


@route("POST", "/api/lists", json_api=True)
def create(req):
    _save(req, None)


@route("POST", r"/api/lists/(?P<list_id>\d{1,9})", json_api=True)
def update(req, list_id: str):
    _save(req, int(list_id))


def _save(req, list_id):
    data = req.json_body()
    try:
        saved_id = want_list_model.save_list(req.session.user_id, list_id, data.get("name"), data.get("cards"))
    except ValidationError as exc:
        req.send_json({"error": str(exc)}, 400)
        return
    except scryfall.ScryfallError:
        req.send_json({"error": "Scryfall indisponível agora. Tente salvar de novo em instantes."}, 502)
        return
    req.send_json({"id": saved_id, "url": f"/lists/{saved_id}"})


@route("POST", r"/lists/(?P<list_id>\d{1,9})/delete")
def delete(req, list_id: str):
    if not want_list_model.delete_list(req.session.user_id, int(list_id)):
        raise HttpError(404, "Lista não encontrada")
    req.redirect("/lists")


@route("GET", r"/lists/(?P<list_id>\d{1,9})/want\.jpg")
def want_image(req, list_id: str):
    want_list = _owned_list(req, list_id)
    if not want_list.cards:
        raise HttpError(400, "A lista está vazia")
    # ?prices=liga,tcgplayer -> esses sites (escolhidos no diálogo, só para esta imagem);
    # ?prices=1 -> os da barra "Preços de" da conta; ausente, vazio ou só valores inválidos -> sem preços.
    raw = req.query.get("prices", "")
    markets = req.session.markets if raw == "1" else parse_markets(raw)
    with_prices = bool(markets)
    try:
        prices, images = scryfall.fetch_export_data(c.scryfall_id for c in want_list.cards)
    except scryfall.ScryfallError:
        raise HttpError(502, "Scryfall indisponível agora. Tente de novo em instantes.") from None
    if with_prices and "liga" in markets:
        _merge_liga_prices(want_list, prices)

    jpg = render_want_list(want_list, images, prices if with_prices else None, markets)
    suffix = "-com-precos" if with_prices else ""
    filename = f"want-{_slug(want_list.name)}{suffix}.jpg"
    req.send_body(jpg, "image/jpeg", extra_headers=[
        ("Content-Disposition", f'attachment; filename="{filename}"'),
        ("Cache-Control", "no-store"),
    ])
