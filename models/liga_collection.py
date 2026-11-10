"""Model: coleção pública da LigaMagic (`?view=colecao/colecao&id=N`) cruzada com as want lists.

A página de coleção não traz JSON como a da carta: as cartas vêm numa tabela HTML, 80 linhas
por página (`&page=N`). Cada linha `<tr id='cc_card_...'>` tem quantidade, `#número`, a edição
(nome no `title`, código no link), o link `/?view=cards/card&card=<nome EN>&ed=<código>` (o nome
em inglês canônico vem dele), Extras (Foil), idioma (alt da bandeira), qualidade e os preços
"Menor P. Compra" / "Menor P. Venda" em texto.

Cortesia com o site: as páginas passam pelo mesmo `fetch_html` da Liga (User-Agent honesto,
pausa quando a Liga pede) e ficam 1 h no SQLite, já reduzidas às linhas.

O robots.txt da Liga desaconselha robôs em `?view=colecao/` (lido em 2026-11-10). Por isso esta
leitura é conservadora: só acontece quando o usuário clica, uma página a cada
COLLECTION_INTERVAL_SECONDS e no máximo MAX_PAGES páginas por coleção.
"""

import html
import json
import math
import re
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from models import ligamagic
from models.db import connect
from models.want_list import ListCard

ROWS_PER_PAGE = 80
MAX_PAGES = 15  # 1.200 linhas: acima disso lê só o começo (ver docstring sobre o robots.txt).
COLLECTION_INTERVAL_SECONDS = 5.0  # mais devagar que a página de carta (2 s)
CACHE_TTL = timedelta(hours=1)
CACHE_PURGE_AGE = timedelta(days=7)
ALLOWED_HOSTS = {"ligamagic.com.br", "www.ligamagic.com.br"}
_ID_RE = re.compile(r"[1-9]\d{0,9}")

# Só a tabela desktop (class='pointer'): a página repete as linhas numa versão mobile (class 'zebra'/'').
# Não dá para deduplicar pela id: duas cópias iguais em estoque podem vir com a mesma cc_card_*.
_ROW_RE = re.compile(r"<tr\b[^>]*\bclass=['\"][^'\"]*\bpointer\b[^'\"]*['\"][^>]*\bid=['\"]cc_card_[\d_]+['\"][^>]*>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<td\b([^>]*)>(.*?)</td>", re.S)
_LINK_RE = re.compile(r"<a\b[^>]*\bhref=(['\"])([^'\"]*view=cards/card[^'\"]*)\1[^>]*>(.*?)</a>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")

_page_locks: dict[tuple[int, int], threading.Lock] = {}
_page_locks_guard = threading.Lock()


class InvalidCollection(ValueError):
    """Texto que não é um link de coleção da LigaMagic nem uma id."""


class CollectionNotFound(Exception):
    """A página veio sem nenhuma carta: coleção vazia, privada ou inexistente."""


# ---------- entrada do usuário ----------

def parse_collection_id(text: str) -> int:
    """Id da coleção a partir do link colado (ou da id pura).

    Só o número sai daqui: a URL que o servidor busca é montada por `ligamagic.fetch_html`,
    nunca a que o usuário colou (nada de requisição para host arbitrário).
    """
    text = " ".join(str(text or "").split())
    if _ID_RE.fullmatch(text):
        return int(text)
    parsed = urllib.parse.urlparse(text if "://" in text else f"https://{text}")
    if (parsed.hostname or "").lower() not in ALLOWED_HOSTS:
        raise InvalidCollection("Cole o link de uma coleção da LigaMagic (ligamagic.com.br).")
    query = urllib.parse.parse_qs(parsed.query)
    if query.get("view", [""])[0] != "colecao/colecao":
        raise InvalidCollection("Esse link da LigaMagic não é de uma coleção (precisa ter view=colecao/colecao).")
    raw_id = query.get("id", [""])[0]
    if not _ID_RE.fullmatch(raw_id):
        raise InvalidCollection("O link da coleção está sem a id (…&id=123456).")
    return int(raw_id)


# ---------- parsing ----------

def _text(fragment: str) -> str:
    return " ".join(html.unescape(_TAG_RE.sub(" ", fragment)).split())


def _money(fragment: str) -> Optional[float]:
    """'R$ 1.200,00' -> 1200.0; vazio ou zero -> None."""
    digits = re.sub(r"[^\d,]", "", _text(fragment)).replace(",", ".")
    try:
        value = round(float(digits), 2)
    except ValueError:
        return None
    return value if value > 0 else None


def _parse_row(row: str) -> Optional[dict]:
    cells = _CELL_RE.findall(row)
    name_index = next((i for i, (_, body) in enumerate(cells) if "view=cards/card" in body), None)
    if name_index is None:
        return None
    links = _LINK_RE.findall(cells[name_index][1])
    if not links:
        return None
    href = html.unescape(links[0][1])
    query = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    name_en = " ".join(query.get("card", [""])[0].split())
    ed = query.get("ed", [""])[0].strip().lower()
    if not name_en or not ed:
        return None
    # Dois links = nome em português + nome em inglês; um só = a carta só tem o nome em inglês.
    name_pt = _text(links[0][2]) if len(links) > 1 else None

    quantity = re.search(r"<b>\s*(\d+)\s*x\s*</b>", row)
    number = next((m.group(1) for _, body in cells[:name_index]
                   if (m := re.match(r"#\s*(\S+)", _text(body)))), "")
    ed_name = next((html.unescape(m.group(1)) for attrs, _ in cells[:name_index]
                    if "col-ed-txt" in attrs and (m := re.search(r"title=\"([^\"]*)\"", attrs))), "")

    after = cells[name_index + 1:]
    extras = _text(after[0][1]) if len(after) > 0 else ""
    language = ""
    if len(after) > 1 and (m := re.search(r"alt=['\"]([^'\"]*)['\"]", after[1][1])):
        language = html.unescape(m.group(1))
    quality = _text(after[2][1]) if len(after) > 2 else ""
    buy = next((body for attrs, body in cells if "col-pcompra" in attrs), "")
    sell = next((body for attrs, body in cells if "col-pvenda" in attrs), "")

    return {
        "name_en": name_en,
        "name_pt": name_pt if name_pt and name_pt != name_en else None,
        "ed": ed,
        "ed_name": ed_name,
        "number": number.lower(),
        "quantity": int(quantity.group(1)) if quantity else 1,
        "foil": "foil" in extras.lower(),
        "extras": extras,
        "language": language,
        "quality": quality,
        "price_buy": _money(buy),
        "price_sell": _money(sell),
    }


def parse_page(page_html: str, collection_id: int, page: int) -> dict:
    rows = [parsed for row in _ROW_RE.findall(page_html) if (parsed := _parse_row(row))]

    total_rows = None
    if m := re.search(r"name=['\"]pgA['\"][^>]*value=['\"](\d+)['\"]", page_html):
        total_rows = int(m.group(1))
    total_cards = None
    if m := re.search(r"name=['\"]pgB['\"][^>]*value=['\"](\d+)['\"]", page_html):
        total_cards = int(m.group(1))
    if not rows and not total_rows:
        # Sem linhas numa página que nem é da Liga (erro de proxy, formato novo): não é "coleção vazia".
        if not ligamagic.is_liga_page(page_html):
            raise ligamagic.LigaError("Página da LigaMagic em formato inesperado")
        raise CollectionNotFound("Coleção vazia, privada ou inexistente.")

    linked_pages = [int(n) for n in re.findall(rf"id={collection_id}[^\"']*?page=(\d+)", page_html)]
    pages = max([page, *linked_pages, math.ceil((total_rows or 0) / ROWS_PER_PAGE)])

    title = ""
    if m := re.search(rf"<option\s+selected\s+value=['\"]{collection_id}['\"]>([^<]*)</option>", page_html):
        # "Venda / Troca (1.553 cards)" -> "Venda / Troca"
        title = re.sub(r"\s*\([\d.]+\s+cards?\)\s*$", "", html.unescape(m.group(1))).strip()

    return {"title": title, "pages": pages, "total_rows": total_rows,
            "total_cards": total_cards, "rows": rows}


# ---------- cache e rede ----------

def _cache_get(collection_id: int, page: int) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute(
            "SELECT fetched_at, payload FROM liga_collection_cache WHERE collection_id = ? AND page = ?",
            (collection_id, page),
        ).fetchone()
    if row is None or datetime.fromisoformat(row["fetched_at"]) + CACHE_TTL < datetime.now(timezone.utc):
        return None
    data = json.loads(row["payload"])
    data["fetched_at"] = row["fetched_at"]
    return data


def _cache_put(collection_id: int, page: int, data: dict) -> str:
    fetched_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO liga_collection_cache (collection_id, page, fetched_at, payload) "
            "VALUES (?, ?, ?, ?)",
            (collection_id, page, fetched_at, json.dumps(data, ensure_ascii=False)),
        )
    return fetched_at


def purge_cache() -> int:
    cutoff = (datetime.now(timezone.utc) - CACHE_PURGE_AGE).isoformat()
    with connect() as conn:
        return conn.execute("DELETE FROM liga_collection_cache WHERE fetched_at < ?", (cutoff,)).rowcount


def _page_lock(collection_id: int, page: int) -> threading.Lock:
    with _page_locks_guard:
        return _page_locks.setdefault((collection_id, page), threading.Lock())


def get_page(collection_id: int, page: int) -> dict:
    """Uma página da coleção já parseada, do cache (1 h) ou da Liga."""
    if not 1 <= page <= MAX_PAGES:
        raise InvalidCollection(f"Página fora do intervalo (1 a {MAX_PAGES}).")
    with _page_lock(collection_id, page):
        cached = _cache_get(collection_id, page)
        if cached is not None:
            return cached
        print(f"[liga] coleção {collection_id}, página {page}")
        data = parse_page(
            ligamagic.fetch_html({"view": "colecao/colecao", "id": collection_id, "page": page},
                                 min_interval=COLLECTION_INTERVAL_SECONDS),
            collection_id, page,
        )
        data["fetched_at"] = _cache_put(collection_id, page, data)
        return data


# ---------- cruzamento com as listas ----------

def _name_keys(name: str) -> set[str]:
    """Nome completo e só a frente: a Liga lista dupla face pela frente e split pelo nome inteiro."""
    keys = {ligamagic.normalize_name(name), ligamagic.normalize_name(name.split(" // ")[0])}
    keys.discard("")
    return keys


def match_rows(rows: Iterable[dict], cards_by_list: dict[int, list[ListCard]],
               list_names: dict[int, str]) -> list[dict]:
    """Linhas da coleção cujo nome está em alguma lista, cada uma com as cartas da lista que casaram."""
    index: dict[str, list[dict]] = {}
    for list_id, cards in cards_by_list.items():
        for card in cards:
            want = {
                "list_id": list_id, "list_name": list_names.get(list_id, ""),
                "scryfall_id": card.scryfall_id, "quantity": card.quantity,
                "name_en": card.name_en, "name_pt": card.name_pt, "set_code": card.set_code,
                "set_name": card.set_name, "collector_number": card.collector_number,
            }
            for key in _name_keys(card.name_en):
                index.setdefault(key, []).append(want)

    matches = []
    for row in rows:
        seen: set[tuple[int, str]] = set()
        wants = []
        for key in _name_keys(row["name_en"]):
            for want in index.get(key, []):
                ident = (want["list_id"], want["scryfall_id"])
                if ident not in seen:
                    seen.add(ident)
                    wants.append(want)
        if wants:
            matches.append({"row": row, "wants": wants})
    return matches


def _diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return round(a - b, 2) if a is not None and b is not None else None


def compare(items: list[dict[str, Any]], refresh: bool = False) -> list[dict]:
    """Para cada linha da coleção: o preço da cópia dela e, por carta da lista, se é a mesma edição.

    item = {"ed", "number", "foil", "price_buy", "wants": [{"name_en", "set_code", "collector_number", "set_name"}]}.
    Preços pelo mesmo critério do editor (o "Menor" da Liga por edição, todas as qualidades e idiomas);
    a página da carta é a mesma que o editor usa, então normalmente sai do cache.
    """
    # Todas as cartas de uma vez, em paralelo (mesmo ritmo de rede do editor; ver ligamagic.get_summaries).
    summaries = ligamagic.get_summaries((w["name_en"] for item in items for w in item["wants"]), refresh)

    def summary_for(name_en: str) -> dict:
        return summaries[ligamagic.cache_key(name_en)]

    results = []
    for item in items:
        ed, number, foil = item["ed"], item["number"], item["foil"]
        copy_price: Optional[dict] = None
        wants_out = []
        for want in item["wants"]:
            summary = summary_for(want["name_en"])
            set_code, collector_number = want["set_code"].lower(), want["collector_number"].lower()
            want_edition = ligamagic.find_edition(summary, set_code, collector_number, want["set_name"])
            copy_edition = ligamagic.find_liga_edition(summary, ed, number)
            if want_edition is not None and copy_edition is not None:
                same = want_edition is copy_edition
            elif want_edition is not None:
                same = want_edition["code"] == ed and want_edition["num"] == number
            else:
                # A edição da lista não existe na Liga: compara direto os códigos (iguais na maioria das coleções).
                same = set_code == ed and collector_number == number

            if copy_price is None and copy_edition is not None:
                copy_price = ligamagic.edition_price(copy_edition, foil)
            want_price = ligamagic.price_for_print(summary, set_code, collector_number, want["set_name"])
            wants_out.append({"same_edition": same, "price": want_price})

        if copy_price is None or copy_price["value"] is None:
            # Sem a edição na página da carta: vale o "Menor P. Compra" que a própria coleção mostra.
            copy_price = {"value": item.get("price_buy"), "foil": foil, "quality": None}
        for want in wants_out:
            want["diff"] = _diff(copy_price["value"], want["price"]["value"])
        results.append({"copy": copy_price, "wants": wants_out})
    return results
