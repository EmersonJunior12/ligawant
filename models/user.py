"""Model: usuários, hash de senha (scrypt) e bloqueio por tentativas de login."""

import hashlib
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from models.db import connect

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
MIN_PASSWORD_LENGTH = 8
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")

# Sites de preço, na ordem em que aparecem na tela e no JPG.
PRICE_MARKETS = ("liga", "tcgplayer", "cardmarket")

INACTIVITY_LIMIT = timedelta(days=183)  # ~6 meses sem acesso: conta e listas são apagadas

LOCKOUT_WINDOW = timedelta(minutes=15)
MAX_FAILURES_PER_USER = 5
MAX_FAILURES_PER_IP = 20

# Hash calculado contra um salt aleatório quando o usuário não existe:
# o tempo de resposta fica igual e não dá para descobrir quais usuários existem.
_DUMMY_SALT = secrets.token_bytes(16)


@dataclass(frozen=True)
class LoginResult:
    user_id: Optional[int]
    locked: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_password(password: str, salt: bytes) -> str:
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=64
    )
    return digest.hex()


def parse_markets(raw: object) -> tuple[str, ...]:
    """Lista (ou texto "a,b") -> mercados conhecidos, sem repetir, na ordem canônica."""
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        return ()
    chosen = {str(item).strip() for item in raw}
    return tuple(m for m in PRICE_MARKETS if m in chosen)


def set_markets(user_id: int, markets: object) -> tuple[str, ...]:
    """Grava os sites de preço ligados. Lista vazia é válida: nenhum preço na tela nem no JPG."""
    parsed = parse_markets(markets)
    with connect() as conn:
        conn.execute("UPDATE users SET price_markets = ? WHERE id = ?", (",".join(parsed), user_id))
    return parsed


def _validate_username(username: str) -> str:
    """Mensagem de erro, ou "" se o usuário for válido."""
    if not USERNAME_RE.match(username):
        return "Usuário deve ter de 3 a 32 caracteres (letras, números, _ . -)."
    return ""


def _validate_password(password: str) -> str:
    """Mensagem de erro, ou "" se a senha for válida."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"A senha precisa ter pelo menos {MIN_PASSWORD_LENGTH} caracteres."
    if len(password) > 256:
        return "Senha longa demais."
    return ""


def create_user(username: str, password: str) -> tuple[bool, str]:
    """Cria um usuário novo. Retorna (sucesso, mensagem de erro se houver)."""
    username = username.strip()
    error = _validate_username(username) or _validate_password(password)
    if error:
        return False, error

    salt = secrets.token_bytes(16)
    try:
        with connect() as conn:
            now = _now().isoformat()
            conn.execute(
                "INSERT INTO users (username, password_salt, password_hash, created_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (username, salt.hex(), _hash_password(password, salt), now, now),
            )
        return True, ""
    except sqlite3.IntegrityError:
        return False, "Esse usuário já existe."


def change_username(user_id: int, new_username: str) -> tuple[bool, str]:
    """Troca o usuário de login. Quem chama já conferiu a senha atual."""
    new_username = new_username.strip()
    error = _validate_username(new_username)
    if error:
        return False, error
    try:
        with connect() as conn:
            current = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
            if current is None:
                return False, "Usuário não encontrado."
            # Mudar só maiúsculas/minúsculas é permitido; o mesmo nome exato, não.
            if current["username"] == new_username:
                return False, "Esse já é o seu usuário."
            conn.execute("UPDATE users SET username = ? WHERE id = ?", (new_username, user_id))
        return True, ""
    except sqlite3.IntegrityError:
        return False, "Esse usuário já existe."


def change_password(user_id: int, new_password: str) -> tuple[bool, str]:
    """Troca a senha (salt novo). Quem chama já conferiu a senha atual."""
    error = _validate_password(new_password)
    if error:
        return False, error
    salt = secrets.token_bytes(16)
    with connect() as conn:
        conn.execute(
            "UPDATE users SET password_salt = ?, password_hash = ? WHERE id = ?",
            (salt.hex(), _hash_password(new_password, salt), user_id),
        )
    return True, ""


def _is_locked(conn: sqlite3.Connection, username: str, ip: str) -> bool:
    since = (_now() - LOCKOUT_WINDOW).isoformat()
    user_failures = conn.execute(
        "SELECT COUNT(*) FROM login_attempts WHERE username = ? AND success = 0 AND attempted_at >= ?",
        (username, since),
    ).fetchone()[0]
    ip_failures = conn.execute(
        "SELECT COUNT(*) FROM login_attempts WHERE ip = ? AND success = 0 AND attempted_at >= ?",
        (ip, since),
    ).fetchone()[0]
    return user_failures >= MAX_FAILURES_PER_USER or ip_failures >= MAX_FAILURES_PER_IP


def _record_attempt(conn: sqlite3.Connection, username: str, ip: str, success: bool) -> None:
    conn.execute(
        "INSERT INTO login_attempts (username, ip, attempted_at, success) VALUES (?, ?, ?, ?)",
        (username, ip, _now().isoformat(), int(success)),
    )
    if success:
        # Login certo zera o contador desse usuário.
        conn.execute("DELETE FROM login_attempts WHERE username = ? AND success = 0", (username,))


def authenticate(username: str, password: str, ip: str) -> LoginResult:
    """Valida credenciais respeitando o bloqueio por excesso de tentativas."""
    username = username.strip()[:64]
    with connect() as conn:
        if _is_locked(conn, username, ip):
            return LoginResult(user_id=None, locked=True)

        row = conn.execute(
            "SELECT id, password_salt, password_hash FROM users WHERE username = ?",
            (username,),
        ).fetchone()

        if row is None:
            _hash_password(password, _DUMMY_SALT)
            valid = False
        else:
            candidate = _hash_password(password, bytes.fromhex(row["password_salt"]))
            valid = secrets.compare_digest(candidate, row["password_hash"])

        _record_attempt(conn, username, ip, valid)
        if valid:
            conn.execute("UPDATE users SET last_seen_at = ? WHERE id = ?", (_now().isoformat(), row["id"]))
        return LoginResult(user_id=row["id"] if valid else None)


def delete_user(user_id: int) -> bool:
    """Apaga a conta. O cascade leva sessões, listas e cartas; tentativas de login saem pelo username."""
    with connect() as conn:
        row = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM login_attempts WHERE username = ?", (row["username"],))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return True


def purge_inactive_users() -> int:
    """Apaga contas sem acesso há mais de INACTIVITY_LIMIT. Retorna quantas apagou."""
    cutoff = (_now() - INACTIVITY_LIMIT).isoformat()
    with connect() as conn:
        ids = [row["id"] for row in conn.execute(
            "SELECT id FROM users WHERE last_seen_at < ?", (cutoff,))]
    return sum(delete_user(user_id) for user_id in ids)


def purge_old_attempts() -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM login_attempts WHERE attempted_at < ?",
            ((_now() - timedelta(days=1)).isoformat(),),
        )
