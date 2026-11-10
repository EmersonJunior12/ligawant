"""Model: conexão SQLite e schema da aplicação."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "app.db"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Conexão transacional: commit no sucesso, rollback em exceção, sempre fecha."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_seen_at TEXT
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                csrf_token TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS login_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE,
                ip TEXT NOT NULL,
                attempted_at TEXT NOT NULL,
                success INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_login_attempts_user ON login_attempts (username, attempted_at);
            CREATE INDEX IF NOT EXISTS ix_login_attempts_ip ON login_attempts (ip, attempted_at);

            CREATE TABLE IF NOT EXISTS want_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL COLLATE NOCASE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (user_id, name),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS want_list_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                list_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 99),
                scryfall_id TEXT NOT NULL,
                name_en TEXT NOT NULL,
                name_pt TEXT,
                set_code TEXT NOT NULL,
                set_name TEXT NOT NULL,
                collector_number TEXT NOT NULL,
                FOREIGN KEY (list_id) REFERENCES want_lists(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS ix_want_list_cards_list ON want_list_cards (list_id, position);

            -- Resumo de preços da LigaMagic por carta (JSON pequeno, nunca o HTML).
            CREATE TABLE IF NOT EXISTS liga_cache (
                card_name TEXT PRIMARY KEY COLLATE NOCASE,
                fetched_at TEXT NOT NULL,
                payload TEXT NOT NULL
            );

            -- Páginas de coleções da LigaMagic já reduzidas às linhas de carta (JSON, nunca o HTML).
            CREATE TABLE IF NOT EXISTS liga_collection_cache (
                collection_id INTEGER NOT NULL,
                page INTEGER NOT NULL,
                fetched_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (collection_id, page)
            );

            -- Estado do disjuntor da LigaMagic (pausa pedida pelo site, backoff atual): sobrevive a reinício.
            CREATE TABLE IF NOT EXISTS liga_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        _migrate_last_seen(conn)
        _migrate_price_markets(conn)


def _migrate_last_seen(conn: sqlite3.Connection) -> None:
    """Bancos anteriores ao "último acesso": cria a coluna e começa a contar a inatividade a partir de agora."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "last_seen_at" in columns:
        return
    conn.execute("ALTER TABLE users ADD COLUMN last_seen_at TEXT")
    conn.execute("UPDATE users SET last_seen_at = ?", (datetime.now(timezone.utc).isoformat(),))


def _migrate_price_markets(conn: sqlite3.Connection) -> None:
    """Sites de preço ligados por conta (barra de preços). Contas existentes começam com todos."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "price_markets" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN price_markets TEXT NOT NULL DEFAULT 'liga,tcgplayer,cardmarket'")
