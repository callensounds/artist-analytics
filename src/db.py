"""D1 on Workers, sqlite everywhere else. Same SQL, no placeholder database id required to boot."""

from __future__ import annotations

import json
import re
import sqlite3
import weakref
from datetime import datetime, timezone
from pathlib import Path

# Assignment-style secrets sometimes land in a driver message. The value is not logged.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|secret|authorization|cookie|api[_-]?key|bearer|token)\s*[=:]\s*\S+"
)

# SQL lives next to this module. The Worker bundle is src/, not the repo root.
MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def utc_now(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def split_sql(script: str) -> list[str]:
    """Split a migration on semicolons. BEGIN/END blocks stay one statement.

    Trigger bodies contain semicolons. A line-ending semicolon is a boundary
    only when it is outside those blocks. ``END`` from ``CASE`` is ignored
    unless the line is the ``END`` that closes a ``BEGIN``.
    """
    statements = []
    buffer: list[str] = []
    depth = 0
    for line in script.splitlines():
        stripped = line.strip()
        if depth == 0 and stripped.startswith("--"):
            continue
        buffer.append(line)
        upper = stripped.upper()
        if upper == "BEGIN":
            depth += 1
        elif upper == "END;" or upper == "END":
            depth = max(0, depth - 1)
        if depth == 0 and stripped.endswith(";"):
            text = "\n".join(buffer).strip().rstrip(";").strip()
            buffer = []
            if text:
                statements.append(text)
    tail = "\n".join(buffer).strip().rstrip(";").strip()
    if tail:
        statements.append(tail)
    return statements


def _row_dict(row) -> dict:
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    keys = getattr(row, "keys", None)
    if callable(keys):
        try:
            return {key: row[key] for key in keys()}
        except Exception:
            pass
    try:
        return dict(row)
    except Exception:
        return {}


def _result_rows(result) -> list:
    if result is None:
        return []
    rows = getattr(result, "results", None)
    if rows is None and isinstance(result, dict):
        rows = result.get("results")
    return list(rows or [])


def _last_id(result) -> int | None:
    meta = getattr(result, "meta", None)
    if meta is None and isinstance(result, dict):
        meta = result.get("meta")
    if isinstance(meta, dict):
        value = meta.get("last_row_id")
    else:
        value = getattr(meta, "last_row_id", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def _await(value):
    if hasattr(value, "__await__"):
        return await value
    return value


class SqliteDatabase:
    """Local file or memory database. Calls run on the event-loop thread.

    There is no lock and no ``asyncio.to_thread``. Each statement finishes
    before the next await, so concurrent tasks on one loop do not overlap
    inside sqlite. This class is not used on the Workers runtime.
    """

    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row

    async def execute(self, sql: str, params: tuple = ()) -> int | None:
        cursor = self.conn.execute(sql, params)
        self.conn.commit()
        return cursor.lastrowid

    async def query(self, sql: str, params: tuple = ()) -> list[dict]:
        cursor = self.conn.execute(sql, params)
        return [_row_dict(row) for row in cursor.fetchall()]

    async def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = await self.query(sql, params)
        return rows[0] if rows else None

    async def batch(self, statements: list[tuple[str, tuple]]) -> None:
        """One commit for many writes. Callers chunk under D1's parameter limit."""
        if not statements:
            return
        try:
            for sql, params in statements:
                self.conn.execute(sql, params)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise


class D1Database:
    """Await the D1 binding. ``prepare`` and ``bind`` are sync; ``run`` and ``all`` are promises."""

    def __init__(self, binding):
        self.binding = binding

    async def _statement(self, sql: str, params: tuple):
        statement = await _await(self.binding.prepare(sql))
        if params:
            statement = await _await(statement.bind(*params))
        return statement

    async def execute(self, sql: str, params: tuple = ()) -> int | None:
        statement = await self._statement(sql, params)
        result = await _await(statement.run())
        return _last_id(result)

    async def query(self, sql: str, params: tuple = ()) -> list[dict]:
        statement = await self._statement(sql, params)
        result = await _await(statement.all())
        return [_row_dict(row) for row in _result_rows(result)]

    async def query_one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = await self.query(sql, params)
        return rows[0] if rows else None

    async def batch(self, statements: list[tuple[str, tuple]]) -> None:
        """One D1 round trip. A binding without ``batch`` falls back to sequential ``run`` calls."""
        if not statements:
            return
        batch_fn = getattr(self.binding, "batch", None)
        if batch_fn is None:
            for sql, params in statements:
                await self.execute(sql, params)
            return
        prepared = []
        for sql, params in statements:
            prepared.append(await self._statement(sql, params))
        await _await(batch_fn(prepared))


# Bump this when a new file is added under src/migrations. A database that
# already has this version skips the per-file probes.
LATEST_MIGRATION = "0011_read_budget.sql"

# One successful migrate per database object (or its D1 binding). The next
# request on that isolate does not read schema_migrations again.
_MIGRATED: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def missing_table(exc: BaseException) -> bool:
    """True when a summary table is not created yet. A D1 limit error is not this."""
    return "no such table" in str(exc).lower()


def log_read_failure(scope: str, exc: BaseException) -> None:
    """One line when a stored read is swallowed. No SQL parameters, headers, or secrets."""
    detail = " ".join(f"{type(exc).__name__}: {exc}".split())
    detail = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[redacted]", detail)
    if len(detail) > 300:
        detail = detail[:300]
    print(
        json.dumps(
            {
                "message": "stored read failed",
                "scope": " ".join(str(scope).split())[:80],
                "error": type(exc).__name__,
                "detail": detail,
            },
            default=str,
        ),
        flush=True,
    )


def _migrate_key(db):
    binding = getattr(db, "binding", None)
    return binding if binding is not None else db


def _already_migrated(db) -> bool:
    try:
        return bool(_MIGRATED.get(_migrate_key(db)))
    except TypeError:
        return False


def _remember_migrated(db) -> None:
    try:
        _MIGRATED[_migrate_key(db)] = True
    except TypeError:
        return


async def _latest_recorded(db) -> bool:
    try:
        row = await db.query_one(
            "SELECT version FROM schema_migrations WHERE version = ?",
            (LATEST_MIGRATION,),
        )
    except Exception:
        return False
    return row is not None


async def migrate(db) -> None:
    if _already_migrated(db):
        return
    if await _latest_recorded(db):
        _remember_migrated(db)
        return
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for path in sorted(MIGRATIONS.glob("*.sql")):
        applied = await db.query_one("SELECT version FROM schema_migrations WHERE version = ?", (path.name,))
        if applied:
            continue
        for statement in split_sql(path.read_text()):
            await db.execute(statement)
        await db.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (path.name, utc_now()),
        )
    _remember_migrated(db)
