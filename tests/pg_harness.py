"""The PostgreSQL server the suite runs against, and a database per process.

`POTATOBOT_TEST_DSN` names the server; unset, it is the local throwaway one
`docs/development.md` starts with podman (trust authentication on 127.0.0.1,
so no password appears anywhere). **No server is a failure, never a skip**: a
test that skips itself is a test that does not exist, and the failure mode of a
guard is silence (`JavaScriptIsActuallyRunTests`).

Import it as `import pg_harness` after putting this directory on `sys.path`,
which gives it one module name whether the suite runs by discovery or one
module runs alone.
"""

import atexit
import os
import threading
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "core" / "schema" / "0026_baseline.sql"
DEFAULT_SERVER_DSN = "postgresql://potatobot@127.0.0.1:55432/potatobot"

_lock = threading.Lock()
_created: list[str] = []


def server_dsn() -> str:
    """The server's administrative connection string."""
    return os.getenv("POTATOBOT_TEST_DSN", "").strip() or DEFAULT_SERVER_DSN


def _drop_created():
    if _worker.get("admin") is not None:
        _worker["admin"].close()
    if not _created:
        return
    try:
        with psycopg.connect(server_dsn(), autocommit=True) as admin:
            for name in _created:
                admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                              .format(sql.Identifier(name)))
    except psycopg.Error:
        pass  # the server is gone; nothing is left to clean


def create_database(label: str, baseline: bool = True) -> str:
    """A new database of this process's own, with the baseline applied unless
    asked otherwise; dropped when the process exits. Returns its DSN."""
    with _lock:
        name = f"potatobot_test_{os.getpid()}_{label}_{len(_created)}"
        with psycopg.connect(server_dsn(), autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                          .format(sql.Identifier(name)))
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        if not _created:
            atexit.register(_drop_created)
        _created.append(name)
    dsn = make_conninfo(server_dsn(), dbname=name)
    if baseline:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(BASELINE.read_text(encoding="utf-8"))
    return dsn


_worker: dict = {}


def _children_first(admin, tables: list[str]) -> list[str]:
    """`tables` ordered so every referencing table precedes the one it
    references, which is the order a plain DELETE empties them in. A cycle
    (or a self-reference) has no such order and raises rather than being
    emptied half-way: the reset would then fail on some later test instead."""
    parents: dict[str, set[str]] = {table: set() for table in tables}
    for child, parent in admin.execute(
            "SELECT conrelid::regclass::text, confrelid::regclass::text "
            "FROM pg_constraint WHERE contype = 'f' "
            "AND connamespace = 'public'::regnamespace"):
        if child in parents and parent in parents:
            parents[child].add(parent)
    referenced_by = {table: {child for child, ps in parents.items() if table in ps}
                     for table in tables}
    ordered: list[str] = []
    remaining = set(tables)
    while remaining:
        ready = sorted(t for t in remaining if not (referenced_by[t] & remaining))
        if not ready:
            raise RuntimeError(
                f"foreign keys form a cycle among {sorted(remaining)}; "
                "fresh_database cannot order its DELETEs")
        ordered.extend(ready)
        remaining.difference_update(ready)
    return ordered


def _reset_statement(admin):
    """One round trip that empties every table but `schema_migrations` and
    restarts the identities they own: the end state of `TRUNCATE … RESTART
    IDENTITY CASCADE`. DELETE measured 3.4 ms against TRUNCATE's 21 here,
    because TRUNCATE swaps in a new file per table and the tables a test leaves
    behind hold a handful of rows."""
    tables = [row[0] for row in admin.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
        "AND tablename <> 'schema_migrations'")]
    sequences = [row[0] for row in admin.execute(
        "SELECT s.oid::regclass::text FROM pg_class s "
        "JOIN pg_depend d ON d.classid = 'pg_class'::regclass AND d.objid = s.oid "
        "AND d.deptype IN ('a', 'i') "
        "JOIN pg_class t ON t.oid = d.refobjid "
        "WHERE s.relkind = 'S' AND t.relnamespace = 'public'::regnamespace "
        "AND t.relname <> 'schema_migrations' ORDER BY 1")]
    return sql.SQL(" ").join(
        [sql.SQL("DELETE FROM {};").format(sql.Identifier(t))
         for t in _children_first(admin, tables)]
        + [sql.SQL("ALTER SEQUENCE {} RESTART;").format(sql.SQL(s))
           for s in sequences])


def fresh_database() -> str:
    """Point `database.DSN` at this process's working database, emptied.

    The suite's per-test fresh database, as a temporary SQLite file was: one
    database per process, built from the baseline once, every table emptied
    and every identity restarted on each call (`_reset_statement`; a template
    clone measured 76 ms). `schema_migrations` is kept, so the test's own
    `initialize_database()` finds the schema and reseeds the defaults. A test
    that changes the schema itself uses `create_database` instead.
    """
    from core import database

    if not _worker:
        dsn = create_database("worker")
        admin = psycopg.connect(dsn, autocommit=True)
        _worker.update(dsn=dsn, admin=admin, reset=_reset_statement(admin))
    _worker["admin"].execute(_worker["reset"])
    database.DSN = _worker["dsn"]
    return _worker["dsn"]


class _UnreachablePool:
    def connection(self, *args, **kwargs):
        raise psycopg.OperationalError("connection refused (pg_harness.unreachable_database)")


def unreachable_database():
    """A context in which the pool cannot lend a connection, as when the server
    is down: the failure path of every accessor, without waiting out the pool's
    timeout against a real dead address."""
    from unittest.mock import patch

    from core import database

    return patch.object(database, "_connection_pool", lambda: _UnreachablePool())
