#!/usr/bin/env python3
"""Copy every PhantomCardz table from SQLite into an empty PostgreSQL database."""
import argparse
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main
from database import CANONICAL_COLUMNS


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
BATCH_SIZE = 1000


def _canonical_column(name):
    return CANONICAL_COLUMNS.get(name.lower(), name)


def _quoted_identifier(name):
    if not IDENTIFIER.fullmatch(name):
        raise ValueError(f"Unsafe database identifier: {name!r}")
    return '"' + name.lower() + '"'


def _table_names():
    return re.findall(r"CREATE TABLE IF NOT EXISTS\s+(\w+)", main.SCHEMA, re.IGNORECASE)


def _snapshot_sqlite(source_path, snapshot_path):
    source = sqlite3.connect(f"file:{Path(source_path).resolve()}?mode=ro", uri=True)
    snapshot = sqlite3.connect(snapshot_path)
    try:
        source.backup(snapshot)
    finally:
        snapshot.close()
        source.close()


def _reset_sequences(target, table_names):
    if not target.is_postgres:
        return
    for table in table_names:
        if "id" not in target.table_columns(table):
            continue
        maximum = target.execute(
            f"SELECT COALESCE(MAX(id), 0) FROM {_quoted_identifier(table)}"
        ).fetchone()[0]
        target.execute(
            "SELECT setval(pg_get_serial_sequence(current_schema() || '.' || ?, 'id')::regclass, "
            "?::bigint, ? > 0)",
            (table, max(maximum, 1), maximum),
        )


def migrate(source_path, target):
    """Migrate one consistent SQLite snapshot; target tables must be empty."""
    source_path = Path(source_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source_path}")

    with tempfile.TemporaryDirectory(prefix="phantomcardz-db-migration-") as directory:
        snapshot_path = Path(directory) / "market-snapshot.sqlite"
        _snapshot_sqlite(source_path, snapshot_path)
        source = sqlite3.connect(snapshot_path)
        source.row_factory = sqlite3.Row
        try:
            main.ensure_schema(target)
            target.commit()

            table_names = _table_names()
            target_counts = {
                table: target.execute(f"SELECT COUNT(*) FROM {_quoted_identifier(table)}").fetchone()[0]
                for table in table_names
            }
            occupied = {table: count for table, count in target_counts.items() if count}
            if occupied:
                raise RuntimeError(
                    "Refusing to migrate into a non-empty database. "
                    f"Tables with existing rows: {', '.join(sorted(occupied))}"
                )

            source_tables = {
                row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            unknown_tables = source_tables - set(table_names) - {"sqlite_sequence"}
            if unknown_tables:
                raise RuntimeError(
                    "Source contains tables outside the app schema; refusing a partial copy: "
                    f"{', '.join(sorted(unknown_tables))}"
                )
            counts = {}
            copied_usernames = set()
            for table in table_names:
                if table not in source_tables:
                    counts[table] = 0
                    continue

                source_columns = [row["name"] for row in source.execute(f'PRAGMA table_info("{table}")')]
                source_canonical = [_canonical_column(name) for name in source_columns]
                target_columns = target.table_columns(table)
                unexpected = set(source_canonical) - target_columns
                if unexpected:
                    raise RuntimeError(
                        f"Source table {table} has columns not in the app schema: "
                        f"{', '.join(sorted(unexpected))}"
                    )
                additional = target_columns - set(source_canonical)
                if additional - ({"username"} if table == "users" else set()):
                    raise RuntimeError(
                        f"Source table {table} is missing columns required by the app: "
                        f"{', '.join(sorted(additional))}"
                    )

                columns = list(source_canonical)
                if table == "users" and "username" not in columns:
                    columns.append("username")
                insert_sql = (
                    f"INSERT INTO {_quoted_identifier(table)} "
                    f"({', '.join(_quoted_identifier(column) for column in columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)})"
                )
                source_cursor = source.execute(f'SELECT * FROM "{table}"')
                copied = 0
                while True:
                    rows = source_cursor.fetchmany(BATCH_SIZE)
                    if not rows:
                        break
                    batch = []
                    for row in rows:
                        values = {
                            _canonical_column(name): row[name]
                            for name in source_columns
                        }
                        if table == "users":
                            username = values.get("username")
                            if not username:
                                username = main.new_username(target)
                                while username in copied_usernames:
                                    username = main.new_username(target)
                                values["username"] = username
                            copied_usernames.add(username)
                        batch.append(tuple(values.get(column) for column in columns))
                    target.executemany(insert_sql, batch)
                    copied += len(batch)
                counts[table] = copied

            _reset_sequences(target, table_names)
            for table in table_names:
                actual = target.execute(
                    f"SELECT COUNT(*) FROM {_quoted_identifier(table)}"
                ).fetchone()[0]
                if actual != counts[table]:
                    raise RuntimeError(
                        f"Row count mismatch for {table}: copied {counts[table]}, found {actual}"
                    )
            target.commit()
            return counts
        except Exception:
            target.rollback()
            raise
        finally:
            source.close()


def main_cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=main.DB_PATH, help="SQLite file to copy (default: market.db)")
    args = parser.parse_args()
    if not os.getenv("DATABASE_URL"):
        parser.error("Set DATABASE_URL to the Supabase PostgreSQL connection string in your shell")

    target = main.db()
    if not target.is_postgres:
        target.close()
        parser.error("DATABASE_URL must point to PostgreSQL")
    try:
        counts = migrate(args.source, target)
    except Exception as error:
        print(f"Migration failed: {error}", file=sys.stderr)
        return 1
    finally:
        target.close()

    print("Migration complete. Rows copied per table:")
    for table, count in counts.items():
        print(f"  {table}: {count}")
    print(f"Total rows copied: {sum(counts.values())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())