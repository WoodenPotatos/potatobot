"""Synthetic write load against a throwaway database, to find where SQLite queues.

The live guild is too quiet for a week of journal to say anything about
contention, so the "stay on SQLite" recommendation in `todo.md` rested on
structure alone. This drives the bot's own accessors through the bot's own
executors — `database.run_write` on the one serialized writer, `run_read` on
the read pool — at rising concurrency, and reports latency as the caller sees
it, queue time included.

It never touches an existing database. The file is created fresh in a
temporary directory, migrated with `initialize_database()`, seeded with
synthetic users, and deleted afterwards unless `--keep` is given. There is no
option to point it anywhere else, by design: a load test against a real
wallet table is a way to lose one.

The workload mix, per operation:
  chat XP     apply_user_delta            (every message in a busy guild)
  daily       claim_timed_reward          (cooldown check and grant)
  transfer    transfer_balance            (/pay)
  slots       resolve_slots_wager         (an instant casino settlement)
  read        get_user_balance via run_read
plus, at every level, one voice tick per simulated guild per second through
apply_batch_user_deltas, which is how voice rewards actually arrive.

Usage:
    python scripts/load_test.py
    python scripts/load_test.py --levels 1 16 64 256 --ops 4000 --guilds 20
    python scripts/load_test.py --json
"""

import argparse
import asyncio
import json
import logging
import os
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import database  # noqa: E402
from core.clock import utc_now  # noqa: E402

GUILD_ID = 1420070400000000001  # synthetic, see publish_public.SYNTHETIC_IDS
USER_BASE = 1_000_000          # deliberately not snowflake-shaped
VOICE_BATCH = 50                 # members in voice per simulated guild

WEIGHTS = {"chat_xp": 40, "daily": 10, "transfer": 10, "slots": 15, "read": 25}


class _Counter(logging.Handler):
    """Counts the two warnings the database layer already emits under load, so
    the report says when the existing alarms would start firing."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.slow = 0
        self.queued = 0

    def emit(self, record):
        message = record.getMessage()
        if message.startswith("Slow database operation"):
            self.slow += 1
        elif message.startswith("Database executor queue delay"):
            self.queued += 1


def _percentile(samples, fraction):
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def seed(users: int) -> list[int]:
    ids = [USER_BASE + index for index in range(users)]
    database.apply_batch_user_deltas([(user_id, 10_000, 0) for user_id in ids])
    return ids


async def one_operation(kind: str, users: list[int], rng: random.Random):
    user = rng.choice(users)
    if kind == "chat_xp":
        await database.run_write(database.apply_user_delta, user, 0, rng.randint(5, 15))
    elif kind == "daily":
        await database.run_write(
            database.claim_timed_reward, user, "last_daily",
            utc_now().isoformat(), 100, 10, None, True)
    elif kind == "transfer":
        other = rng.choice(users)
        if other != user:
            await database.run_write(database.transfer_balance, user, other, 1)
    elif kind == "slots":
        await database.run_write(database.resolve_slots_wager, GUILD_ID, user, 10)
    else:
        await database.run_read(database.get_user_balance, user)


async def voice_ticks(guilds: int, users: list[int], stop: asyncio.Event,
                      latencies: list[float]):
    rng = random.Random(7)
    while not stop.is_set():
        for _ in range(guilds):
            batch = [(user, 2, 3) for user in rng.sample(users, min(VOICE_BATCH, len(users)))]
            started = time.perf_counter()
            await database.run_write(database.apply_batch_user_deltas, batch)
            latencies.append(time.perf_counter() - started)
        try:
            await asyncio.wait_for(stop.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass


async def run_level(concurrency: int, total_ops: int, guilds: int,
                    users: list[int]) -> dict:
    kinds = list(WEIGHTS)
    weights = [WEIGHTS[kind] for kind in kinds]
    latencies: dict[str, list[float]] = {kind: [] for kind in kinds}
    voice_latencies: list[float] = []
    remaining = total_ops
    stop = asyncio.Event()

    async def worker(seed_value):
        nonlocal remaining
        rng = random.Random(seed_value)
        while remaining > 0:
            remaining -= 1
            kind = rng.choices(kinds, weights)[0]
            started = time.perf_counter()
            await one_operation(kind, users, rng)
            latencies[kind].append(time.perf_counter() - started)

    ticker = asyncio.create_task(voice_ticks(guilds, users, stop, voice_latencies))
    started = time.perf_counter()
    await asyncio.gather(*(worker(index) for index in range(concurrency)))
    elapsed = time.perf_counter() - started
    stop.set()
    await ticker

    every = [value for values in latencies.values() for value in values]
    writes = [value for kind, values in latencies.items() if kind != "read"
              for value in values]

    def summary(samples):
        if not samples:
            return None
        return {
            "n": len(samples),
            "p50_ms": round(statistics.median(samples) * 1000, 2),
            "p95_ms": round(_percentile(samples, 0.95) * 1000, 2),
            "p99_ms": round(_percentile(samples, 0.99) * 1000, 2),
            "max_ms": round(max(samples) * 1000, 2),
        }

    return {
        "concurrency": concurrency,
        "ops": len(every),
        "seconds": round(elapsed, 2),
        "ops_per_second": round(len(every) / elapsed, 1),
        "all": summary(every),
        "writes": summary(writes),
        "voice_tick": summary(voice_latencies),
        "by_kind": {kind: summary(values) for kind, values in latencies.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--levels", type=int, nargs="+", default=[1, 8, 32, 128, 512],
                        help="concurrent callers per level")
    parser.add_argument("--ops", type=int, default=3000, help="operations per level")
    parser.add_argument("--users", type=int, default=5000, help="synthetic wallets")
    parser.add_argument("--guilds", type=int, default=10,
                        help="voice ticks per second, one per simulated guild")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--keep", action="store_true", help="keep the database afterwards")
    arguments = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="potatobot-load-"))
    database.DB_PATH = str(workdir / "load.db")
    counter = _Counter()
    database.db_logger.addHandler(counter)

    try:
        database.initialize_database()
        users = seed(arguments.users)
        results = []
        for level in arguments.levels:
            counter.slow = counter.queued = 0
            result = asyncio.run(run_level(level, arguments.ops, arguments.guilds, users))
            result["slow_operation_warnings"] = counter.slow
            result["queue_delay_warnings"] = counter.queued
            results.append(result)
            if not arguments.json:
                w = result["writes"]
                v = result["voice_tick"] or {}
                print(f"  c={level:<4} {result['ops_per_second']:>7} ops/s  "
                      f"write p50 {w['p50_ms']:>7}ms  p95 {w['p95_ms']:>8}ms  "
                      f"p99 {w['p99_ms']:>8}ms  max {w['max_ms']:>8}ms  "
                      f"voice p95 {v.get('p95_ms', '-'):>7}ms  "
                      f"queue-warn {counter.queued}  slow-warn {counter.slow}")
        size = os.path.getsize(database.DB_PATH)
        report = {"python": sys.version.split()[0],
                  "sqlite": database.sqlite3.sqlite_version,
                  "users": arguments.users, "guilds": arguments.guilds,
                  "database_bytes": size, "levels": results}
        if arguments.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"\n  python {report['python']}, sqlite {report['sqlite']}, "
                  f"{arguments.users} users, database {size // 1024} KiB")
    finally:
        if arguments.keep:
            print(f"  kept {database.DB_PATH}", file=sys.stderr)
        else:
            for entry in sorted(workdir.rglob("*"), reverse=True):
                entry.unlink() if entry.is_file() else entry.rmdir()
            workdir.rmdir()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
