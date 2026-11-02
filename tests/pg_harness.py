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


def fresh_database() -> str:
    """Point `database.DSN` at this process's working database, emptied.

    The suite's per-test fresh database, as a temporary SQLite file was: one
    database per process, built from the baseline once, every table truncated
    (identities restarted) on each call, which measured 35 ms against 76 for a
    template clone. `schema_migrations` is kept, so the test's own
    `initialize_database()` finds the schema and reseeds the defaults. A test
    that changes the schema itself uses `create_database` instead.
    """
    from core import database

    if not _worker:
        dsn = create_database("worker")
        admin = psycopg.connect(dsn, autocommit=True)
        tables = [row[0] for row in admin.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename <> 'schema_migrations' ORDER BY tablename")]
        _worker.update(dsn=dsn, admin=admin, truncate=sql.SQL(
            "TRUNCATE {} RESTART IDENTITY CASCADE").format(
                sql.SQL(", ").join(sql.Identifier(t) for t in tables)))
    _worker["admin"].execute(_worker["truncate"])
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
