"""Model: listas de want e suas cartas. Toda consulta filtra pelo dono (user_id)."""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from models import scryfall
from models.db import connect

MAX_LIST_NAME = 80
MAX_CARDS_PER_LIST = 200
MAX_NAME_PT = 150


class ValidationError(Exception):
    """Dado de entrada inválido; a mensagem vai para o usuário."""


@dataclass
class ListCard:
    scryfall_id: str
    quantity: int
    name_en: str
    name_pt: Optional[str]
    set_code: str
    set_name: str
    collector_number: str


@dataclass
class WantList:
    id: int
    name: str
    owner: str
    created_at: str
    updated_at: str
    cards: list[ListCard] = field(default_factory=list)

    @property
    def total_quantity(self) -> int:
        return sum(c.quantity for c in self.cards)


@dataclass(frozen=True)
class ListSummary:
    id: int
    name: str
    card_count: int
    total_quantity: int
    updated_at: str


def list_summaries(user_id: int) -> list[ListSummary]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT l.id, l.name, l.updated_at,
                   COUNT(c.id) AS card_count, COALESCE(SUM(c.quantity), 0) AS total_quantity
            FROM want_lists l LEFT JOIN want_list_cards c ON c.list_id = l.id
            WHERE l.user_id = ?
            GROUP BY l.id
            ORDER BY l.updated_at DESC
            """,
            (user_id,),
        ).fetchall()
    return [ListSummary(r["id"], r["name"], r["card_count"], r["total_quantity"], r["updated_at"]) for r in rows]


def all_cards(user_id: int) -> dict[int, list[ListCard]]:
    """Cartas de todas as listas do usuário, por id de lista, numa consulta só (resumo de "Minhas listas")."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT c.list_id, c.scryfall_id, c.quantity, c.name_en, c.name_pt, c.set_code, c.set_name,
                   c.collector_number
            FROM want_list_cards c JOIN want_lists l ON l.id = c.list_id
            WHERE l.user_id = ?
            ORDER BY c.list_id, c.position
            """,
            (user_id,),
        ).fetchall()
    cards: dict[int, list[ListCard]] = {}
    for row in rows:
        data = dict(row)
        cards.setdefault(data.pop("list_id"), []).append(ListCard(**data))
    return cards


def get_list(user_id: int, list_id: int) -> Optional[WantList]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT l.id, l.name, l.created_at, l.updated_at, u.username
            FROM want_lists l JOIN users u ON u.id = l.user_id
            WHERE l.id = ? AND l.user_id = ?
            """,
            (list_id, user_id),
        ).fetchone()
        if row is None:
            return None
        cards = conn.execute(
            """
            SELECT scryfall_id, quantity, name_en, name_pt, set_code, set_name, collector_number
            FROM want_list_cards WHERE list_id = ? ORDER BY position
            """,
            (list_id,),
        ).fetchall()
    return WantList(
        id=row["id"], name=row["name"], owner=row["username"],
        created_at=row["created_at"], updated_at=row["updated_at"],
        cards=[ListCard(**dict(c)) for c in cards],
    )


def _parse_payload_cards(raw_cards: object) -> list[dict]:
    if not isinstance(raw_cards, list):
        raise ValidationError("Formato de cartas inválido.")
    if not raw_cards:
        raise ValidationError("Adicione pelo menos uma carta.")
    if len(raw_cards) > MAX_CARDS_PER_LIST:
        raise ValidationError(f"Máximo de {MAX_CARDS_PER_LIST} cartas por lista.")

    parsed = []
    for item in raw_cards:
        if not isinstance(item, dict):
            raise ValidationError("Formato de cartas inválido.")
        scryfall_id = str(item.get("scryfall_id", ""))
        if not scryfall.is_valid_scryfall_id(scryfall_id):
            raise ValidationError("Carta com identificador inválido.")
        try:
            quantity = int(item.get("quantity", 1))
        except (TypeError, ValueError):
            raise ValidationError("Quantidade inválida.") from None
        if not 1 <= quantity <= 99:
            raise ValidationError("Quantidade deve ficar entre 1 e 99.")
        name_pt = item.get("name_pt")
        name_pt = str(name_pt).strip()[:MAX_NAME_PT] if name_pt else None
        parsed.append({"scryfall_id": scryfall_id, "quantity": quantity, "name_pt": name_pt or None})
    return parsed


def save_list(user_id: int, list_id: Optional[int], name: str, raw_cards: object) -> int:
    """Cria ou atualiza uma lista. Os dados da carta vêm do Scryfall, não do navegador."""
    name = " ".join(str(name or "").split())
    if not name:
        raise ValidationError("Dê um nome para a lista.")
    if len(name) > MAX_LIST_NAME:
        raise ValidationError(f"Nome da lista com no máximo {MAX_LIST_NAME} caracteres.")

    payload = _parse_payload_cards(raw_cards)
    try:
        api_cards = scryfall.get_cards_by_ids(c["scryfall_id"] for c in payload)
    except scryfall.ScryfallBadRequest:
        raise ValidationError("A lista tem uma carta com identificador inválido.") from None

    cards: list[ListCard] = []
    for item in payload:
        card = api_cards.get(item["scryfall_id"])
        if card is None or card.get("lang") != "en":
            raise ValidationError("Uma das cartas não foi encontrada no Scryfall (ou não é uma versão em inglês).")
        cards.append(ListCard(
            scryfall_id=card["id"], quantity=item["quantity"], name_en=card["name"],
            name_pt=item["name_pt"], set_code=card["set"].upper(), set_name=card["set_name"],
            collector_number=card["collector_number"],
        ))

    now = datetime.now(timezone.utc).isoformat()
    try:
        with connect() as conn:
            if list_id is None:
                cur = conn.execute(
                    "INSERT INTO want_lists (user_id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (user_id, name, now, now),
                )
                list_id = cur.lastrowid
            else:
                cur = conn.execute(
                    "UPDATE want_lists SET name = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                    (name, now, list_id, user_id),
                )
                if cur.rowcount == 0:
                    raise ValidationError("Lista não encontrada.")
                conn.execute("DELETE FROM want_list_cards WHERE list_id = ?", (list_id,))

            conn.executemany(
                """
                INSERT INTO want_list_cards
                    (list_id, position, quantity, scryfall_id, name_en, name_pt, set_code, set_name, collector_number)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (list_id, pos, c.quantity, c.scryfall_id, c.name_en, c.name_pt,
                     c.set_code, c.set_name, c.collector_number)
                    for pos, c in enumerate(cards)
                ],
            )
    except sqlite3.IntegrityError:
        raise ValidationError("Você já tem uma lista com esse nome.") from None
    return list_id


def delete_list(user_id: int, list_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM want_lists WHERE id = ? AND user_id = ?", (list_id, user_id))
        return cur.rowcount > 0
