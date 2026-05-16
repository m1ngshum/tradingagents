"""Shared I/O helpers for Polymarket + stocks scripts.

Three CLI scripts (run_polymarket.py, score_fills.py, backtest.py) used to
duplicate this constant and the JSONL-append helper. Centralised here so
custom `TRADINGAGENTS_RESULTS_DIR` overrides apply consistently if we ever
honor that env var, and so the output path doesn't drift across scripts.

Concurrency: `append_jsonl` takes an exclusive POSIX file lock around the
write. JSON payloads can exceed PIPE_BUF (4 KiB on macOS/Linux) once they
include rationale strings, so POSIX's atomic-append-under-PIPE_BUF guarantee
doesn't cover us. Two concurrent fires can otherwise interleave a single
line and corrupt the JSONL. flock is process-portable across macOS / Linux
and adds no measurable cost for the polling workloads we run.
"""

from __future__ import annotations

import fcntl
import json
from pathlib import Path
from typing import Any

POLYMARKET_OUTPUT_DIR = Path.home() / ".tradingagents" / "polymarket"
STOCKS_OUTPUT_DIR = Path.home() / ".tradingagents" / "stocks"


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """Append one JSONL line under an exclusive file lock.

    Safe against concurrent fires (e.g. cron-overlapping routines + a manual
    debug run on the same date file). Lock is released on close.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
