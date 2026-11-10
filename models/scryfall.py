"""Model: cliente da API do Scryfall (https://scryfall.com/docs/api).

Regras da API respeitadas aqui: User-Agent e Accept explícitos e no máximo ~10
requisições por segundo (intervalo mínimo de 100 ms entre chamadas). Imagens em
cards.scryfall.io não entram nesse limite.
"""

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable, Optional

API_BASE = "https://api.scryfall.com"
IMAGE_HOST = "cards.scryfall.io"
HEADERS = {"User-Agent": "MtgWantList/1.0", "Accept": "application/json"}
TIMEOUT_SECONDS = 15
MIN_INTERVAL_SECONDS = 0.1
CACHE_TTL_SECONDS = 600
COLLECTION_BATCH = 75
MAX_PRINT_PAGES = 3
MAX_IMAGE_BYTES = 5 * 1024 * 1024

# Só deixa passar o que pode existir num nome de carta: evita que o texto
# digitado vire sintaxe de busca do Scryfall (o:, t:, -, parênteses...).
_SEARCH_TERM_RE = re.compile(r"[^\w\s',]", re.UNICODE)
_SCRYFALL_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_rate_lock = threading.Lock()
_last_call = 0.0
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, Any]] = {}
_details_cache: dict[str, tuple[float, dict]] = {}


class ScryfallError(Exception):
    """Falha de comunicação com o Scryfall (rede, timeout, 5xx)."""


class ScryfallBadRequest(ScryfallError):
    """O Scryfall recusou a requisição (HTTP 400), ex.: identificador que não é um UUID válido."""


def _throttle() -> None:
    global _last_call
    with _rate_lock:
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _request_json(url: str, body: Optional[dict] = None) -> Optional[dict]:
    """GET (ou POST JSON) na API. Retorna None em 404, que é como o Scryfall diz 'nada encontrado'."""
    headers = dict(HEADERS)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    _throttle()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=data, headers=headers), timeout=TIMEOUT_SECONDS
        ) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        if exc.code == 400:
            raise ScryfallBadRequest("Scryfall recusou a requisição (HTTP 400)") from exc
        raise ScryfallError(f"Scryfall respondeu HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ScryfallError(f"Falha ao falar com o Scryfall: {exc}") from exc


def _cached_get(url: str) -> Optional[dict]:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(url)
        if hit and hit[0] > now:
            return hit[1]

    result = _request_json(url)
    with _cache_lock:
        _cache[url] = (now + CACHE_TTL_SECONDS, result)
        if len(_cache) > 2000:
            for key in [k for k, (exp, _) in _cache.items() if exp <= now]:
                del _cache[key]
    return result


def _api_url(path: str, **params: str) -> str:
    return f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"


def _small_image(card: dict) -> Optional[str]:
    uris = card.get("image_uris") or (card.get("card_faces") or [{}])[0].get("image_uris") or {}
    return uris.get("small")


def _face_uris(card: dict) -> list[dict]:
    """image_uris de cada face: carta normal tem na raiz; dupla face (transform/MDFC), por face."""
    if card.get("image_uris"):
        return [card["image_uris"]]
    return [face.get("image_uris") or {} for face in card.get("card_faces") or []]


def _english(card: dict) -> dict:
    """A mesma impressão em inglês. O app só salva e lista `lang:en`, então isto é só uma garantia:
    se algum dia chegar uma impressão em outro idioma, a carta ampliada continua sendo a inglesa."""
    if card.get("lang", "en") == "en":
        return card
    set_code = urllib.parse.quote(card.get("set", ""))
    number = urllib.parse.quote(card.get("collector_number", ""))
    english = _cached_get(f"{API_BASE}/cards/{set_code}/{number}/en")
    return english or card


def preview_images(card: dict) -> dict:
    """Imagens grandes (672x936, sempre da versão em inglês) para ampliar a carta: frente e, se houver, verso."""
    faces = [uris.get("large") or uris.get("normal") for uris in _face_uris(_english(card))]
    faces = [url for url in faces if url]
    return {"front": faces[0] if faces else None, "back": faces[1] if len(faces) > 1 else None}


def normal_image_url(card: dict) -> Optional[str]:
    """URL da imagem 'normal' da face da frente (cartas dupla face não têm image_uris na raiz)."""
    uris = card.get("image_uris") or (card.get("card_faces") or [{}])[0].get("image_uris") or {}
    return uris.get("normal")


def _first_price(prices: dict, keys: tuple[str, ...]) -> dict:
    """Primeiro preço disponível na ordem dada; o 1º key é o não-foil, os demais são foil/etched."""
    for index, key in enumerate(keys):
        raw = prices.get(key)
        if not raw:
            continue
        try:
            return {"value": round(float(raw), 2), "foil": index > 0}
        except ValueError:
            continue
    return {"value": None, "foil": False}


def extract_prices(card: dict) -> dict:
    """Preço do TCGplayer (US$) e do Cardmarket (€). Sem preço normal, cai para o foil e marca."""
    prices = card.get("prices") or {}
    return {
        "tcgplayer": _first_price(prices, ("usd", "usd_foil", "usd_etched")),
        "cardmarket": _first_price(prices, ("eur", "eur_foil", "eur_etched")),
    }


def is_valid_scryfall_id(value: str) -> bool:
    return bool(_SCRYFALL_ID_RE.match(value or ""))


def search_names(term: str, limit: int = 15) -> list[dict]:
    """Busca nomes em inglês (autocomplete) e em português (printed_name das impressões PT)."""
    term = _SEARCH_TERM_RE.sub(" ", term).strip()
    term = " ".join(term.split())[:80]
    if len(term) < 2:
        return []

    results: list[dict] = []
    seen: set[str] = set()

    auto = _cached_get(_api_url("/cards/autocomplete", q=term)) or {}
    for name in auto.get("data", [])[:10]:
        if name not in seen:
            seen.add(name)
            results.append({"name_en": name, "name_pt": None, "lang": "en"})

    pt = _cached_get(_api_url("/cards/search", q=f"{term} lang:pt", unique="cards")) or {}
    for card in pt.get("data", [])[:10]:
        if card.get("lang") != "pt":
            continue
        name_en = card["name"]
        name_pt = card.get("printed_name") or (card.get("card_faces") or [{}])[0].get("printed_name")
        if name_en in seen:
            # Mesmo card já veio pelo inglês: só acrescenta o nome PT.
            for item in results:
                if item["name_en"] == name_en and not item["name_pt"]:
                    item["name_pt"] = name_pt
            continue
        seen.add(name_en)
        results.append({"name_en": name_en, "name_pt": name_pt, "lang": "pt"})

    # Quem começa com o termo digitado aparece primeiro, em qualquer idioma.
    lowered = term.lower()

    def rank(item: dict) -> int:
        names = [item["name_en"], item["name_pt"] or ""]
        return 0 if any(n.lower().startswith(lowered) for n in names) else 1

    results.sort(key=rank)
    return results[:limit]


def get_prints(name_en: str) -> list[dict]:
    """Todas as impressões físicas em inglês de uma carta, da mais nova para a mais antiga."""
    name_en = name_en.strip()[:200]
    if not name_en:
        return []

    card = _cached_get(_api_url("/cards/named", exact=name_en))
    if not card or not card.get("oracle_id"):
        return []

    url: Optional[str] = _api_url(
        "/cards/search",
        q=f"oracleid:{card['oracle_id']} lang:en game:paper",
        unique="prints", order="released", dir="desc",
    )
    prints: list[dict] = []
    for _ in range(MAX_PRINT_PAGES):
        if not url:
            break
        page = _cached_get(url) or {}
        for c in page.get("data", []):
            prints.append({
                "id": c["id"],
                "name_en": c["name"],
                "set_code": c["set"].upper(),
                "set_name": c["set_name"],
                "collector_number": c["collector_number"],
                "year": (c.get("released_at") or "")[:4],
                "rarity": c.get("rarity", ""),
                "image": _small_image(c),
                "preview": preview_images(c),
                "prices": extract_prices(c),
            })
        url = page.get("next_page") if page.get("has_more") else None
    return prints


def get_cards_by_ids(ids: Iterable[str]) -> dict[str, dict]:
    """Busca impressões específicas pelo scryfall_id (sempre chamada nova, sem cache)."""
    unique_ids = list(dict.fromkeys(i for i in ids if is_valid_scryfall_id(i)))
    found: dict[str, dict] = {}
    for start in range(0, len(unique_ids), COLLECTION_BATCH):
        batch = unique_ids[start:start + COLLECTION_BATCH]
        resp = _request_json(
            f"{API_BASE}/cards/collection",
            body={"identifiers": [{"id": i} for i in batch]},
        ) or {}
        for card in resp.get("data", []):
            found[card["id"]] = card
    return found


def get_card_details(ids: Iterable[str], refresh: bool = False) -> dict[str, dict]:
    """Preços e miniatura por impressão, para a tela do editor (cache em memória por id; `refresh` ignora)."""
    now = time.monotonic()
    details: dict[str, dict] = {}
    missing: list[str] = []
    with _cache_lock:
        for card_id in dict.fromkeys(i for i in ids if is_valid_scryfall_id(i)):
            hit = _details_cache.get(card_id)
            if hit and hit[0] > now and not refresh:
                details[card_id] = hit[1]
            else:
                missing.append(card_id)

    if missing:
        fresh = {
            card_id: {"prices": extract_prices(card), "image": _small_image(card), "preview": preview_images(card)}
            for card_id, card in get_cards_by_ids(missing).items()
        }
        with _cache_lock:
            for card_id, detail in fresh.items():
                _details_cache[card_id] = (now + CACHE_TTL_SECONDS, detail)
        details.update(fresh)
    return details


def download_image(url: str) -> bytes:
    """Baixa uma imagem de carta. Só aceita o CDN do Scryfall (evita SSRF)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != IMAGE_HOST:
        raise ScryfallError(f"Host de imagem não permitido: {parsed.hostname}")
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"]}),
            timeout=TIMEOUT_SECONDS,
        ) as resp:
            data = resp.read(MAX_IMAGE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ScryfallError(f"Falha ao baixar imagem: {exc}") from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise ScryfallError("Imagem maior que o permitido")
    return data


def fetch_export_data(scryfall_ids: Iterable[str]) -> tuple[dict[str, dict], dict[str, Optional[bytes]]]:
    """Consulta a API agora e devolve (preços por id, imagem 'normal' em inglês por id).

    Nada é persistido: cada geração de JPG faz a chamada de novo, e a mesma resposta
    do /cards/collection serve imagens e preços. Imagem que falhar vem como None e a
    view desenha um placeholder no lugar.
    """
    ids = list(dict.fromkeys(scryfall_ids))
    cards = get_cards_by_ids(ids)
    urls = {i: normal_image_url(cards[i]) for i in ids if i in cards}

    def fetch(item: tuple[str, Optional[str]]) -> tuple[str, Optional[bytes]]:
        card_id, url = item
        if not url:
            return card_id, None
        try:
            return card_id, download_image(url)
        except ScryfallError:
            return card_id, None

    images: dict[str, Optional[bytes]] = {i: None for i in ids}
    with ThreadPoolExecutor(max_workers=8) as pool:
        images.update(pool.map(fetch, urls.items()))
    prices = {i: extract_prices(card) for i, card in cards.items()}
    return prices, images
