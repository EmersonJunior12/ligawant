"""Model: sessões server-side (só o hash do token vai para o banco) e token CSRF."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from models.db import connect
from models.user import parse_markets

SESSION_LIFETIME = timedelta(hours=12)
LAST_SEEN_RESOLUTION = timedelta(days=1)  # "último acesso" só é regravado 1x por dia por conta


@dataclass(frozen=True)
class Session:
    user_id: int
    username: str
    csrf_token: str
    markets: tuple[str, ...] = ()  # sites de preço ligados (users.price_markets)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(user_id: int) -> str:
    """Cria uma sessão nova e devolve o token em claro (só vai para o cookie)."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (_hash_token(token), user_id, secrets.token_urlsafe(32),
             now.isoformat(), (now + SESSION_LIFETIME).isoformat()),
        )
    return token


def get_session(token: str) -> Optional[Session]:
    if not token:
        return None
    with connect() as conn:
        row = conn.execute(
            """
            SELECT sessions.user_id, sessions.csrf_token, sessions.expires_at,
                   users.username, users.last_seen_at, users.price_markets
            FROM sessions JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ?
            """,
            (_hash_token(token),),
        ).fetchone()

    if row is None:
        return None
    now = datetime.now(timezone.utc)
    if datetime.fromisoformat(row["expires_at"]) < now:
        delete_session(token)
        return None
    last_seen = row["last_seen_at"]
    if last_seen is None or datetime.fromisoformat(last_seen) < now - LAST_SEEN_RESOLUTION:
        with connect() as conn:
            conn.execute("UPDATE users SET last_seen_at = ? WHERE id = ?", (now.isoformat(), row["user_id"]))
    return Session(user_id=row["user_id"], username=row["username"], csrf_token=row["csrf_token"],
                   markets=parse_markets(row["price_markets"]))


def delete_session(token: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),))


def delete_user_sessions(user_id: int) -> None:
    """Derruba todas as sessões do usuário (ex.: depois de trocar a senha)."""
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def purge_expired() -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM sessions WHERE expires_at < ?",
            (datetime.now(timezone.utc).isoformat(),),
        )
