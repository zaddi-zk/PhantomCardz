"""Database layer for PhantomCardz.

The application is written against the small slice of the ``sqlite3`` API it
needs (``?`` placeholders, ``executescript``, ``lastrowid``, name/index row
access).  This module keeps that surface intact while transparently targeting
either SQLite (local development / the persistent Render disk) or PostgreSQL
(Supabase) selected through the ``DATABASE_URL`` environment variable.

PostgreSQL differences handled here:

* ``?`` -> ``%s`` parameter style.
* ``INTEGER PRIMARY KEY AUTOINCREMENT`` -> ``BIGSERIAL PRIMARY KEY``;
  ``REAL`` -> ``DOUBLE PRECISION``.
* ``cursor.lastrowid`` (absent on psycopg) -> ``INSERT ... RETURNING id``.
* unquoted camelCase identifiers fold to lowercase -> case-insensitive rows.
* ``executescript`` (absent on psycopg) -> multi-statement ``execute``.
* pooled, pre-pinged connections (``psycopg_pool``) instead of one socket/request.
"""
import os
import re
import threading
import sqlite3 as _sqlite3

try:
    import psycopg
    from psycopg_pool import ConnectionPool
except ImportError:  # pragma: no cover - only required for PostgreSQL
    psycopg = None
    ConnectionPool = None


def _normalize_url(url: str) -> str:
    # Heroku/Render hand out ``postgres://`` which SQLAlchemy rejects.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


DATABASE_URL = _normalize_url(os.environ.get("DATABASE_URL", "").strip())
IS_POSTGRES = DATABASE_URL.startswith("postgresql://")

# sqlite3 exposes ``IntegrityError``; psycopg raises ``UniqueViolation`` which
# subclasses its ``IntegrityError``.  Callers catch this tuple in one ``except``.
INTEGRITY_ERRORS = (_sqlite3.IntegrityError,)
if psycopg is not None:
    INTEGRITY_ERRORS += (psycopg.IntegrityError,)

# Columns the application declares in camelCase.  PostgreSQL folds unquoted
# identifiers to lowercase, so the migrator canonicalizes both sides to these.
CANONICAL_COLUMNS = {
    name.lower(): name.lower()
    for name in ("productId", "comparePrice", "minQuantity", "bannerColor")
}

_AUTOINCREMENT = re.compile(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", re.IGNORECASE)
_REAL = re.compile(r"\bREAL\b", re.IGNORECASE)


def _ddl_for_postgres(script: str) -> str:
    script = _AUTOINCREMENT.sub("BIGSERIAL PRIMARY KEY", script)
    return _REAL.sub("DOUBLE PRECISION", script)


class Row:
    """Tuple-like row with case-insensitive column lookup and index access."""

    __slots__ = ("_keys", "_values", "_lookup")

    def __init__(self, keys, values):
        self._keys = tuple(keys)
        self._values = tuple(values)
        self._lookup = {key.lower(): i for i, key in enumerate(self._keys)}

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        try:
            return self._values[self._lookup[key.lower()]]
        except KeyError:
            raise KeyError(key) from None

    def get(self, key, default=None):
        index = self._lookup.get(key.lower()) if isinstance(key, str) else None
        return default if index is None else self._values[index]

    def keys(self):
        return list(self._keys)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __contains__(self, key):
        return isinstance(key, str) and key.lower() in self._lookup

    def __eq__(self, other):
        if isinstance(other, Row):
            return self._values == other._values
        if isinstance(other, tuple):
            return self._values == other
        return NotImplemented

    def __repr__(self):
        return "Row(" + repr(dict(zip(self._keys, self._values))) + ")"


class Cursor:
    """Thin cursor wrapper normalizing sqlite3 and psycopg cursors."""

    def __init__(self, connection):
        self._connection = connection
        self._raw = connection._raw.cursor()
        self.rowcount = -1

    def execute(self, sql, params=None):
        statement = self._connection._prepare(sql)
        if params is None:
            self._raw.execute(statement)
        else:
            self._raw.execute(statement, self._connection._adapt_params(params))
        self.rowcount = self._raw.rowcount
        return self

    def executemany(self, sql, seq_of_params):
        statement = self._connection._prepare(sql)
        if self._connection.is_postgres:
            seq_of_params = [self._connection._adapt_params(p) for p in seq_of_params]
        self._raw.executemany(statement, seq_of_params)
        self.rowcount = self._raw.rowcount
        return self

    def _wrap(self, values):
        if values is None or self._raw.description is None:
            return values
        keys = [column[0] for column in self._raw.description]
        return Row(keys, values)

    def fetchone(self):
        return self._wrap(self._raw.fetchone())

    def fetchmany(self, size=None):
        rows = self._raw.fetchmany() if size is None else self._raw.fetchmany(size)
        return [self._wrap(row) for row in rows]

    def fetchall(self):
        return [self._wrap(row) for row in self._raw.fetchall()]

    def close(self):
        self._raw.close()

    def __iter__(self):
        return iter(self.fetchall())


class Connection:
    """sqlite3-compatible connection over SQLite or PostgreSQL."""

    def __init__(self, raw, is_postgres: bool, pool=None):
        self._raw = raw
        self.is_postgres = is_postgres
        self._pool = pool

    def _prepare(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.is_postgres else sql

    def _adapt_params(self, params):
        # SQLite stores Python ``bool`` as 0/1; every PostgreSQL flag column is
        # declared INTEGER, so mirror that coercion explicitly.
        if not self.is_postgres or not isinstance(params, (list, tuple)):
            return params
        return tuple(int(value) if isinstance(value, bool) else value for value in params)

    def cursor(self):
        return Cursor(self)

    def execute(self, sql, params=None):
        return Cursor(self).execute(sql, params)

    def executemany(self, sql, seq_of_params):
        return Cursor(self).executemany(sql, seq_of_params)

    def executescript(self, script: str):
        if self.is_postgres:
            raw = self._raw.cursor()
            try:
                raw.execute(_ddl_for_postgres(script))
            finally:
                raw.close()
        else:
            self._raw.executescript(script)
        return self

    def table_columns(self, table: str):
        raw = self._raw.cursor()
        try:
            if self.is_postgres:
                raw.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = %s",
                    (table.lower(),),
                )
                return {row[0].lower() for row in raw.fetchall()}
            return {row[1].lower() for row in raw.execute(f'PRAGMA table_info("{table}")').fetchall()}
        finally:
            raw.close()

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        if self._pool is not None:
            pool, self._pool = self._pool, None
            try:
                self._raw.rollback()
            except Exception:
                pass
            pool.putconn(self._raw)
        else:
            self._raw.close()

    def __del__(self):
        # Return leaked connections (e.g. a route that raised before close())
        # to the pool instead of exhausting it.
        pool = getattr(self, "_pool", None)
        if pool is None:
            return
        self._pool = None
        try:
            self._raw.rollback()
        except Exception:
            pass
        try:
            pool.putconn(self._raw)
        except Exception:
            pass


_pool = None
_pool_lock = threading.Lock()


def _postgres_pool():
    global _pool
    if _pool is None:
        if ConnectionPool is None:
            raise RuntimeError("psycopg[pool] is required when DATABASE_URL points at PostgreSQL")
        with _pool_lock:
            if _pool is None:
                # SSL/sslmode and the Supabase session-pooler host come from the
                # URL itself; ``check_connection`` pings borrowed connections.
                _pool = ConnectionPool(
                    DATABASE_URL,
                    min_size=2,
                    max_size=10,
                    max_idle=300,
                    timeout=30,
                    check=ConnectionPool.check_connection,
                    open=True,
                )
    return _pool


def connect_db(path: str) -> Connection:
    """Open a connection to PostgreSQL (``DATABASE_URL``) or SQLite at ``path``."""
    if IS_POSTGRES:
        pool = _postgres_pool()
        return Connection(pool.getconn(), is_postgres=True, pool=pool)
    raw = _sqlite3.connect(path, timeout=30, check_same_thread=False)
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("PRAGMA busy_timeout=30000")
    return Connection(raw, is_postgres=False)


def insert_id(target, sql: str, params=()):
    """Run an INSERT and return the generated ``id``."""
    is_postgres = target.is_postgres if isinstance(target, Connection) else target._connection.is_postgres
    if is_postgres:
        row = target.execute(sql.rstrip().rstrip(";") + " RETURNING id", params).fetchone()
        return None if row is None else row[0]
    cursor = target.execute(sql, params)
    raw = cursor._raw if isinstance(cursor, Cursor) else cursor
    return raw.lastrowid
