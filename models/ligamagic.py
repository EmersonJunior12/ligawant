"""Model: menor preço da LigaMagic (R$) por impressão, a partir da página pública da carta.

O que se usa da página (`?view=cards/card&card=<nome>`), tudo em JSON de texto limpo:
- `cards_editions`: por edição, o resumo Menor/Médio/Maior por variante (normal, foil, etched...).
- `cards_stock`: as ofertas, das quais só se lê a qualidade (M/NM/SP...) para o selo.
O preço de cada oferta vem ofuscado como sprite de dígitos: isso é proteção deliberada da
Liga e não é decodificado aqui. Por isso o valor é o "Menor" da edição (todas as qualidades
e idiomas) e a qualidade aparece à parte, como a melhor disponível em estoque.

Cortesia com o site: User-Agent honesto, uma carta por chamada, no máximo MAX_CONCURRENT
chamadas abertas ao mesmo tempo (no app inteiro), intervalo mínimo entre inícios e cache de 12h
no SQLite (guarda só o resumo, nunca o HTML).

Quando a Liga sinaliza que quer menos tráfego (HTTP 429/503 ou página de desafio), o app pausa
todas as chamadas até o `Retry-After` ou por um backoff crescente (30 s até 15 min), sem ir à rede
no meio; o usuário pode liberar antes ("Tentar agora"). HTTP 403 é recusa, não pausa: vira aviso.
Nunca se tenta contornar: nada de User-Agent de navegador, troca de IP ou resolver desafio.
"""

import email.utils
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

from models.db import connect

PAGE_URL = "https://www.ligamagic.com.br/"
HEADERS = {"User-Agent": "MtgWantList/1.0", "Accept": "text/html"}
TIMEOUT_SECONDS = 20
# Ritmo: até MAX_CONCURRENT páginas abertas ao mesmo tempo e pelo menos MIN_INTERVAL_SECONDS entre
# dois inícios. Teto efetivo ~1 req/s (antes: 1 a cada 2 s, uma por vez). Vale para o app inteiro,
# não por usuário: todo o tráfego sai do mesmo IP. Voltar a 1 / 2.0 restaura o modo sequencial.
MAX_CONCURRENT = 2
MIN_INTERVAL_SECONDS = 1.0
CACHE_TTL = timedelta(hours=12)
# "Atualizar preços" ignora o cache, mas não busca de novo o que foi buscado há menos que isso.
REFRESH_MIN_AGE = timedelta(minutes=15)
# "Não encontrado" pode ser falha momentânea da Liga: vale bem menos que um resultado de verdade.
NOT_FOUND_TTL = timedelta(hours=1)
CACHE_PURGE_AGE = timedelta(days=7)
MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_NAME_LENGTH = 200

# Disjuntor: status que significam "pare um pouco" e o backoff quando não vem Retry-After.
# 403 não é pausa: é recusa (ver LigaForbidden), avisada como bloqueio de verdade.
BLOCK_STATUSES = {429, 503}
BACKOFF_START = timedelta(seconds=30)
BACKOFF_MAX = timedelta(minutes=15)
# Retry-After absurdo (dias) não trava o app: limita a 6 h. O usuário ainda pode liberar antes (resume).
RETRY_AFTER_MAX = timedelta(hours=6)
# Assinaturas de página de desafio/bloqueio servida com HTTP 200. "captcha" e "challenge" sozinhos
# NÃO entram: aparecem em páginas normais da Liga (verificado em 2026-11-10). E "challenge-platform"
# também aparece em página normal (script do Cloudflare): por isso fetch_html exige ainda que a
# página NÃO seja da Liga (is_liga_page) antes de tratar como desafio.
_CHALLENGE_RE = re.compile(
    r"cf-chl|challenge-platform|cf-browser-verification|<title>\s*(just a moment|attention required|access denied)",
    re.I,
)
# Toda página real da Liga (carta encontrada ou não) tem o título "... | LigaMagic" e a var g_repositorio.
_LIGA_PAGE_RE = re.compile(r"<title>[^<]*LigaMagic\s*</title>|var\s+g_repositorio\s*=", re.I)

# Variante "normal" primeiro; foil e foil etched só quando não há a normal.
EXTRAS_ORDER = ("0", "2", "31")
QUALITY_FALLBACK = {1: "M", 2: "NM", 3: "SP", 4: "MP", 5: "HP", 6: "D"}

# Coleções antigas: o código e a numeração da Liga não seguem os do Scryfall
# (ex.: Alpha é "lea" #269 no Scryfall e "al" #230 na Liga), então casa só pelo código.
SET_ALIASES = {
    "lea": "al", "leb": "be", "2ed": "un", "3ed": "rv",
    "sum": "summer", "ced": "ced", "cei": "cedi",
}

_rate_lock = threading.Lock()
_last_call = 0.0
# Vagas de conexão: segurada do início do intervalo até o fim da leitura da resposta.
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
# Um lock por nome evita que duas abas peçam a mesma carta duas vezes ao mesmo tempo.
_name_locks: dict[str, threading.Lock] = {}
_name_locks_guard = threading.Lock()


class LigaError(Exception):
    """Falha de comunicação com a LigaMagic (rede, timeout, HTTP de erro, página fora do formato)."""


class LigaBlocked(LigaError):
    """A Liga pediu uma pausa (429/403/503 ou desafio): nenhuma chamada sai até `retry_at`."""

    def __init__(self, retry_at: datetime, reason: str):
        super().__init__(f"LigaMagic pediu uma pausa ({reason}) até {retry_at.isoformat()}")
        self.retry_at = retry_at
        self.reason = reason

    @property
    def retry_after_seconds(self) -> int:
        return max(1, int((self.retry_at - datetime.now(timezone.utc)).total_seconds()))


class LigaForbidden(LigaError):
    """HTTP 403: a Liga recusou o acesso (bloqueio de verdade, não pedido de pausa). Não abre o
    disjuntor; quem chama para o lote atual e mostra a recusa, e a próxima ação do usuário tenta de novo."""


def _clean_name(name_en: str) -> str:
    return " ".join(name_en.split())[:MAX_NAME_LENGTH]


def cache_key(name_en: str) -> str:
    """Uma entrada por carta: o nome completo do Scryfall, com as duas metades."""
    return _clean_name(name_en).lower()


def _name_candidates(name_en: str) -> list[str]:
    """Nomes a tentar na Liga, em ordem.

    A Liga indexa cartas com " // " de dois jeitos: split, adventure, flip e aftermath (as
    duas metades na mesma face) pelo nome completo; transform e modal_dfc (duas faces) só
    pela frente. O layout não chega até aqui, então tenta o completo e depois a frente.
    """
    full = _clean_name(name_en)
    front = full.split(" // ")[0].strip()
    return [full, front] if front and front != full else [full]


def _norm(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", ascii_text.lower())


normalize_name = _norm


# ---------- rede ----------

def _throttle(min_interval: float) -> None:
    global _last_call
    with _rate_lock:
        wait = min_interval - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


# ---------- disjuntor (pausa pedida pela Liga) ----------

_state_lock = threading.Lock()


def _state_get(key: str) -> Optional[str]:
    with connect() as conn:
        row = conn.execute("SELECT value FROM liga_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _state_set(**values: Optional[str]) -> None:
    with connect() as conn:
        for key, value in values.items():
            if value is None:
                conn.execute("DELETE FROM liga_state WHERE key = ?", (key,))
            else:
                conn.execute("INSERT OR REPLACE INTO liga_state (key, value) VALUES (?, ?)", (key, value))


def blocked_until() -> Optional[LigaBlocked]:
    """A pausa em vigor, se houver (lida do SQLite: vale também depois de reiniciar o servidor)."""
    raw = _state_get("blocked_until")
    if not raw:
        return None
    retry_at = datetime.fromisoformat(raw)
    if retry_at <= datetime.now(timezone.utc):
        return None
    return LigaBlocked(retry_at, _state_get("blocked_reason") or "pausa")


def _parse_retry_after(value: Optional[str]) -> Optional[timedelta]:
    """Retry-After em segundos ou como data HTTP; inválido/ausente -> None (usa o backoff)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return timedelta(seconds=int(value))
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when - datetime.now(timezone.utc)


def _open_breaker(reason: str, retry_after: Optional[timedelta]) -> LigaBlocked:
    """Abre (ou estende) a pausa. Sem Retry-After, dobra o backoff anterior: 30 s, 1, 2, 4… até 15 min."""
    with _state_lock:
        if retry_after is not None and retry_after > timedelta(0):
            pause = min(retry_after, RETRY_AFTER_MAX)
        else:
            previous = _state_get("backoff_seconds")
            last = timedelta(seconds=float(previous)) if previous else None
            pause = min(last * 2, BACKOFF_MAX) if last else BACKOFF_START
        retry_at = datetime.now(timezone.utc) + pause
        _state_set(blocked_until=retry_at.isoformat(), blocked_reason=reason,
                   backoff_seconds=str(int(pause.total_seconds())))
    print(f"[liga] PAUSA: {reason}; nenhuma chamada até {retry_at.astimezone().strftime('%H:%M:%S')}")
    return LigaBlocked(retry_at, reason)


def resume_now() -> bool:
    """"Tentar agora": o usuário libera a pausa antes do prazo. O backoff fica guardado, então se a
    Liga pedir pausa de novo logo em seguida, a próxima dobra (não volta aos 30 s)."""
    if blocked_until() is None:
        return False
    with _state_lock:
        _state_set(blocked_until=None)
    print("[liga] pausa liberada manualmente pelo usuário")
    return True


def _close_breaker() -> None:
    """Resposta normal: zera backoff e pausa (só escreve se havia algo, para não pesar no SQLite).

    Com pausa em vigor, não mexe: com chamadas em paralelo, esta resposta pode ser de uma requisição
    que saiu ANTES de outra receber o 429, e não prova que a Liga voltou a aceitar. (Nenhuma chamada
    sai durante a pausa; o "Tentar agora" limpa `blocked_until` e aí a próxima resposta boa fecha.)
    """
    if not (_state_get("backoff_seconds") or _state_get("blocked_until")):
        return
    with _state_lock:
        if blocked_until() is not None:
            return
        _state_set(blocked_until=None, blocked_reason=None, backoff_seconds=None)


def is_liga_page(page_html: str) -> bool:
    """Marcador positivo: a página é mesmo da Liga (e não um desafio ou erro de proxy)."""
    return bool(_LIGA_PAGE_RE.search(page_html))


def _fetch_page(name: str) -> str:
    return fetch_html({"view": "cards/card", "card": name})


def fetch_html(params: dict[str, Any], min_interval: float = MIN_INTERVAL_SECONDS) -> str:
    """GET numa página da Liga, sempre em PAGE_URL (quem chama só escolhe os parâmetros), com o
    mesmo intervalo mínimo, timeout e teto de tamanho para todas as páginas que o app lê.

    Com a pausa em vigor, levanta LigaBlocked sem ir à rede. 429/403/503 ou página de desafio
    abrem a pausa; qualquer resposta normal fecha.
    """
    if (blocked := blocked_until()) is not None:
        raise blocked
    url = f"{PAGE_URL}?{urllib.parse.urlencode(params)}"
    try:
        # Vaga primeiro, intervalo depois: assim o intervalo mede inícios reais de conexão.
        with _slots:
            _throttle(min_interval)
            # Outra thread pode ter aberto a pausa enquanto esta esperava a vaga ou o intervalo.
            if (blocked := blocked_until()) is not None:
                raise blocked
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=TIMEOUT_SECONDS) as resp:
                raw = resp.read(MAX_PAGE_BYTES + 1)
                charset = resp.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as exc:
        if exc.code in BLOCK_STATUSES:
            raise _open_breaker(f"HTTP {exc.code}", _parse_retry_after(exc.headers.get("Retry-After"))) from exc
        if exc.code == 403:
            print("[liga] RECUSA: HTTP 403")
            raise LigaForbidden("LigaMagic recusou o acesso (HTTP 403)") from exc
        raise LigaError(f"LigaMagic respondeu HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise LigaError(f"Falha ao falar com a LigaMagic: {exc}") from exc
    if len(raw) > MAX_PAGE_BYTES:
        raise LigaError("Página da LigaMagic maior que o esperado")
    page = raw.decode(charset, errors="replace")
    # O Cloudflare injeta /cdn-cgi/challenge-platform/ também em páginas NORMAIS da Liga (visto em
    # 2026-11-10): só é desafio se tiver a assinatura E não for uma página real da Liga.
    if _CHALLENGE_RE.search(page) and not is_liga_page(page):
        raise _open_breaker("página de verificação", None)
    _close_breaker()
    return page


# ---------- parsing ----------

def _extract_var(html: str, name: str) -> Any:
    match = re.search(rf"var\s+{re.escape(name)}\s*=\s*", html)
    if not match:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(html, match.end())
    except json.JSONDecodeError as exc:
        raise LigaError(f"Formato inesperado em {name}") from exc
    return value


def _to_float(raw: Any) -> Optional[float]:
    try:
        value = round(float(raw), 2)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _edition_prices(raw: Any) -> dict[str, Optional[float]]:
    """`price` vem como dict {extras: {p,m,g}}, como lista (= variante normal) ou vazio."""
    if isinstance(raw, list):
        raw = {str(i): v for i, v in enumerate(raw)}
    if not isinstance(raw, dict):
        return {}
    prices = {}
    for extras, summary in raw.items():
        if isinstance(summary, dict):
            prices[str(extras)] = _to_float(summary.get("p"))
    return prices


def _summarize(html: str) -> dict:
    """Reduz a página ao que o app usa. Carta inexistente: a página não tem `cards_editions`."""
    editions = _extract_var(html, "cards_editions")
    if not isinstance(editions, list):
        return {"editions": []}

    quality_names = dict(QUALITY_FALLBACK)
    for item in _extract_var(html, "dataQuality") or []:
        if isinstance(item, dict) and item.get("id") and item.get("acron"):
            quality_names[int(item["id"])] = str(item["acron"])

    # Melhor qualidade (menor qualid) por (edição, variante), só de ofertas de loja (leilão não tem lj_id).
    best: dict[tuple[str, str], int] = {}
    for offer in _extract_var(html, "cards_stock") or []:
        if not isinstance(offer, dict) or not offer.get("lj_id") or "bids" in offer:
            continue
        try:
            quality = int(offer.get("qualid"))
        except (TypeError, ValueError):
            continue
        key = (str(offer.get("idEdicao")), str(offer.get("extras", 0)))
        if quality in quality_names and quality < best.get(key, 99):
            best[key] = quality

    result = []
    for edition in editions:
        if not isinstance(edition, dict) or "id" not in edition:
            continue
        edition_id = str(edition.get("id"))
        prices = _edition_prices(edition.get("price"))
        result.append({
            "code": str(edition.get("code") or "").lower(),
            "num": str(edition.get("num") or "").lower(),
            "name": str(edition.get("name") or ""),
            "prices": prices,
            "quality": {
                extras: quality_names[best[(edition_id, extras)]]
                for extras in prices if (edition_id, extras) in best
            },
        })
    return {"editions": result}


# ---------- cache ----------

def _cache_get(name: str, max_age: timedelta) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT fetched_at, payload FROM liga_cache WHERE card_name = ?", (name,)).fetchone()
    if row is None:
        return None
    summary = json.loads(row["payload"])
    if not summary.get("editions"):
        max_age = min(max_age, NOT_FOUND_TTL)
    if datetime.fromisoformat(row["fetched_at"]) + max_age < datetime.now(timezone.utc):
        return None
    summary["fetched_at"] = row["fetched_at"]
    return summary


def _cache_put(name: str, summary: dict) -> str:
    fetched_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO liga_cache (card_name, fetched_at, payload) VALUES (?, ?, ?)",
            (name, fetched_at, json.dumps(summary, ensure_ascii=False)),
        )
    return fetched_at


def purge_cache() -> int:
    cutoff = (datetime.now(timezone.utc) - CACHE_PURGE_AGE).isoformat()
    with connect() as conn:
        return conn.execute("DELETE FROM liga_cache WHERE fetched_at < ?", (cutoff,)).rowcount


def _name_lock(name: str) -> threading.Lock:
    with _name_locks_guard:
        return _name_locks.setdefault(name.lower(), threading.Lock())


def get_card_summary(name_en: str, refresh: bool = False) -> dict:
    """Resumo da carta (todas as edições) com `fetched_at`, do cache ou da Liga.

    `refresh` encurta a validade do cache para REFRESH_MIN_AGE em vez de ignorá-lo de vez,
    para cliques repetidos em "Atualizar preços" não virarem rajada de requisições.
    """
    key = _clean_name(name_en)
    if not key:
        return {"editions": [], "fetched_at": None}
    with _name_lock(key):
        cached = _cache_get(key, REFRESH_MIN_AGE if refresh else CACHE_TTL)
        if cached is not None:
            return cached
        summary = {"editions": []}
        for candidate in _name_candidates(key):
            print(f"[liga] buscando {candidate!r}")
            page = _fetch_page(candidate)
            summary = _summarize(page)
            if summary["editions"]:
                break
            # "Não encontrada" só vale (e vai para o cache) se a página for mesmo da Liga.
            if not is_liga_page(page):
                raise LigaError("Página da LigaMagic em formato inesperado")
        # Carta não encontrada também fica no cache (por menos tempo: NOT_FOUND_TTL).
        summary["fetched_at"] = _cache_put(key, summary)
        return summary


# ---------- impressão do Scryfall -> edição da Liga ----------

def _pick(candidates: list[dict]) -> Optional[dict]:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # Empate (ex.: sld231en x sld231jp): a versão em inglês, senão a mais barata.
    english = [e for e in candidates if e["code"].endswith("en")]
    pool = english or candidates
    return min(pool, key=lambda e: _best_price(e)[0] or float("inf"))


def _match_edition(editions: list[dict], set_code: str, number: str, set_name: str) -> Optional[dict]:
    set_code, number, set_name = set_code.lower(), number.lower(), _norm(set_name)
    same_num = [e for e in editions if e["num"] == number]
    rules = (
        lambda e: e["code"] == set_code,                      # trc -> trc
        lambda e: e["code"].startswith(set_code),             # sld -> sld412, cma -> cma2
        lambda e: set_code in e["code"],                      # tle -> eftle, soc -> blsoc
        lambda e: _norm(e["name"]) == set_name,
    )
    for rule in rules:
        found = _pick([e for e in same_num if rule(e)])
        if found:
            return found

    alias = SET_ALIASES.get(set_code)
    if alias:
        found = _pick([e for e in editions if e["code"] == alias])
        if found:
            return found
    # Numeração diferente, mas nome de coleção idêntico e único (ex.: "Revised Edition").
    by_name = [e for e in editions if _norm(e["name"]) == set_name]
    return by_name[0] if len(by_name) == 1 else None


def _best_price(edition: dict) -> tuple[Optional[float], bool, Optional[str]]:
    """(valor, é foil, qualidade) da primeira variante com preço na ordem normal > foil > etched."""
    for index, extras in enumerate(EXTRAS_ORDER):
        value = edition["prices"].get(extras)
        if value is not None:
            return value, index > 0, edition["quality"].get(extras)
    return None, False, None


def find_edition(summary: dict, set_code: str, collector_number: str, set_name: str) -> Optional[dict]:
    """Edição da Liga que corresponde a uma impressão do Scryfall (None se não achar)."""
    return _match_edition(summary.get("editions") or [], set_code, collector_number, set_name)


def find_liga_edition(summary: dict, code: str, number: str) -> Optional[dict]:
    """Edição pelo código e número da própria Liga (ex.: o link de uma coleção, `ed=eld` + `#153`).

    Número diferente mas código único na carta (coleções antigas numeradas de outro jeito): usa o código.
    """
    editions = summary.get("editions") or []
    code, number = code.lower(), number.lower()
    exact = _pick([e for e in editions if e["code"] == code and e["num"] == number])
    if exact:
        return exact
    same_code = [e for e in editions if e["code"] == code]
    return same_code[0] if len(same_code) == 1 else None


def edition_price(edition: dict, foil: bool = False) -> dict:
    """Preço "Menor" de uma edição: a variante foil quando pedida (se houver), senão a ordem normal > foil > etched."""
    if foil:
        value = edition["prices"].get("2")
        if value is not None:
            return {"value": value, "foil": True, "quality": edition["quality"].get("2")}
    value, is_foil, quality = _best_price(edition)
    return {"value": value, "foil": is_foil, "quality": quality}


def price_for_print(summary: dict, set_code: str, collector_number: str, set_name: str) -> dict:
    """Preço da Liga para uma impressão, com a hora da consulta (`fetched_at`, ISO UTC)."""
    price = _price_for_print(summary, set_code, collector_number, set_name)
    price["fetched_at"] = summary.get("fetched_at")
    return price


def _price_for_print(summary: dict, set_code: str, collector_number: str, set_name: str) -> dict:
    """Sem edição correspondente: menor preço normal entre todas as edições, marcado."""
    editions = summary.get("editions") or []
    edition = _match_edition(editions, set_code, collector_number, set_name)
    if edition is not None:
        value, foil, quality = _best_price(edition)
        if value is not None:
            return {"value": value, "foil": foil, "quality": quality, "other_edition": False}

    others = [e for e in editions if e is not edition and e["prices"].get("0") is not None]
    if not others:
        return empty_price()
    cheapest = min(others, key=lambda e: e["prices"]["0"])
    return {"value": cheapest["prices"]["0"], "foil": False,
            "quality": cheapest["quality"].get("0"), "other_edition": True}


def empty_price() -> dict:
    return {"value": None, "foil": False, "quality": None, "other_edition": False}


def cached_prices(cards: Iterable[tuple[str, str, str, str]]) -> list[dict]:
    """Como get_prices, mas só com o que já está no cache (de qualquer idade) e sem nunca buscar na Liga.

    Serve o resumo de "Minhas listas": abrir a página não pode disparar uma busca de 2 s por carta.
    Carta nunca consultada sai sem preço.
    """
    cards = list(cards)
    keys = sorted({cache_key(name) for name, *_ in cards})
    summaries: dict[str, dict] = {}
    with connect() as conn:
        for start in range(0, len(keys), 500):
            chunk = keys[start:start + 500]
            rows = conn.execute(
                f"SELECT card_name, fetched_at, payload FROM liga_cache "
                f"WHERE card_name IN ({','.join('?' * len(chunk))})",
                chunk,
            ).fetchall()
            for row in rows:
                summary = json.loads(row["payload"])
                summary["fetched_at"] = row["fetched_at"]
                summaries[row["card_name"].lower()] = summary
    missing = {"editions": [], "fetched_at": None}
    return [
        price_for_print(summaries.get(cache_key(name), missing), set_code, number, set_name)
        for name, set_code, number, set_name in cards
    ]


def get_summaries(names: Iterable[str], refresh: bool = False) -> dict[str, dict]:
    """cache_key -> resumo, para vários nomes em paralelo (1 busca por nome único).

    O ritmo de rede continua no fetch_html (_slots + _throttle); o pool só deixa os nomes andarem
    juntos. Workers acima de MAX_CONCURRENT servem para acertos de cache não esperarem atrás de
    buscas de rede. Primeira falha (LigaBlocked, LigaForbidden, LigaError): cancela o que ainda
    não começou e propaga, como no modo sequencial. Com a pausa aberta, o que já estava na fila
    sai sem ir à rede.
    """
    unique: dict[str, str] = {}
    for name in names:
        unique.setdefault(cache_key(name), name)
    if len(unique) <= 1:
        return {key: get_card_summary(name, refresh) for key, name in unique.items()}

    pool = ThreadPoolExecutor(max_workers=min(len(unique), MAX_CONCURRENT * 2), thread_name_prefix="liga")
    try:
        futures = {pool.submit(get_card_summary, name, refresh): key for key, name in unique.items()}
        return {futures[future]: future.result() for future in as_completed(futures)}
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def get_prices(cards: Iterable[tuple[str, str, str, str]], refresh: bool = False) -> list[dict]:
    """(name_en, set_code, collector_number, set_name) -> preço da Liga, na mesma ordem. 1 busca por nome."""
    cards = list(cards)
    summaries = get_summaries((name_en for name_en, *_ in cards), refresh)
    return [
        price_for_print(summaries[cache_key(name_en)], set_code, number, set_name)
        for name_en, set_code, number, set_name in cards
    ]
