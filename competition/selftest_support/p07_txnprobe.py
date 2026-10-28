"""Timing probe for the count-trigger acceptance run.

This module is loaded as a pytest plugin (``-p selftest_support.p07_txnprobe``).
It does not change behaviour: it wraps ``AsyncDatabase.transaction`` so every
transaction's wall-clock hold time is recorded, then prints a distribution at the
end of the session.

Why this matters: the acceptance test creates a SQLite trigger with a blocking
``sqlite3`` call from inside the event loop thread. If that call lands while the
supervisor still owns an open transaction, the DDL waits for the lock while the
event loop is blocked, so the supervisor can never reach its commit. The
transaction's measured hold time then jumps to the sqlite busy timeout instead of
the milliseconds its own statements need.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from contextlib import asynccontextmanager

import powercontext.builtin.persistence.database as _database_module

_RECORDS: list[tuple[float, str]] = []


def _install() -> None:
    original = _database_module.AsyncDatabase.transaction

    @asynccontextmanager
    async def timed(self):  # type: ignore[no-untyped-def]
        started = time.monotonic()
        task = asyncio.current_task()
        name = task.get_name() if task is not None else "-"
        entered = False
        try:
            async with original(self) as connection:
                entered = True
                try:
                    yield connection
                finally:
                    _RECORDS.append((time.monotonic() - started, name))
        finally:
            if not entered:
                _RECORDS.append((time.monotonic() - started, f"{name}:enter-failed"))

    _database_module.AsyncDatabase.transaction = timed


_install()


def _report() -> None:
    if not _RECORDS:
        print("txn-probe: no transactions recorded", flush=True)
        return
    durations = sorted(record[0] for record in _RECORDS)
    total = sum(durations)
    count = len(durations)
    longest = sorted(_RECORDS, reverse=True)[:8]
    print("txn-probe begin", flush=True)
    print(f"txn-probe transactions: {count}", flush=True)
    print(f"txn-probe total_hold_seconds: {total:.3f}", flush=True)
    print(f"txn-probe mean_ms: {statistics.fmean(durations) * 1000:.2f}", flush=True)
    print(f"txn-probe p50_ms: {statistics.median(durations) * 1000:.2f}", flush=True)
    print(f"txn-probe p95_ms: {durations[int(count * 0.95) - 1] * 1000:.2f}", flush=True)
    print(f"txn-probe max_ms: {durations[-1] * 1000:.2f}", flush=True)
    print(f"txn-probe over_1000ms: {sum(1 for d in durations if d >= 1.0)}", flush=True)
    print(f"txn-probe over_4000ms: {sum(1 for d in durations if d >= 4.0)}", flush=True)
    for seconds, name in longest:
        print(f"txn-probe longest: {seconds * 1000:9.2f} ms  {name}", flush=True)
    print("txn-probe end", flush=True)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 - pytest hook signature
    _report()
