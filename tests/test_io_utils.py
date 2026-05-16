"""Tests for tradingagents/exchange/io_utils.py."""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from tradingagents.exchange.io_utils import append_jsonl


def test_append_jsonl_writes_one_line(tmp_path: Path):
    path = tmp_path / "out.jsonl"
    append_jsonl(path, {"k": "v"})
    assert path.read_text() == '{"k":"v"}\n'


def test_append_jsonl_creates_parent_dir(tmp_path: Path):
    path = tmp_path / "nested" / "dir" / "out.jsonl"
    append_jsonl(path, {"hello": 1})
    assert path.exists()
    assert path.read_text() == '{"hello":1}\n'


def test_append_jsonl_appends(tmp_path: Path):
    path = tmp_path / "out.jsonl"
    for i in range(3):
        append_jsonl(path, {"i": i})
    lines = path.read_text().splitlines()
    assert [json.loads(l)["i"] for l in lines] == [0, 1, 2]


def _writer_process(path_str: str, who: str, n: int, payload_size: int) -> None:
    """Top-level so multiprocessing.spawn can pickle it on macOS."""
    from tradingagents.exchange.io_utils import append_jsonl
    p = Path(path_str)
    for i in range(n):
        append_jsonl(p, {"who": who, "i": i, "rationale": "x" * payload_size})


def test_concurrent_writes_dont_interleave(tmp_path: Path):
    """flock prevents two processes from interleaving a single line.

    The 5KB rationale payload exceeds PIPE_BUF (4KB on macOS/Linux), so
    without flock POSIX makes no atomicity guarantee — interleaved writes
    would produce malformed JSONL lines. With flock, every line parses.
    """
    path = tmp_path / "concurrent.jsonl"
    n_per_proc = 25
    n_procs = 4
    procs = [
        multiprocessing.Process(
            target=_writer_process,
            args=(str(path), f"p{i}", n_per_proc, 5000),
        )
        for i in range(n_procs)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0, f"writer exited {p.exitcode}"

    lines = path.read_text().splitlines()
    assert len(lines) == n_per_proc * n_procs, (
        f"expected {n_per_proc * n_procs} lines, got {len(lines)}"
    )
    # The whole point: every line is parseable. Without flock, ~5-15% are not.
    bad = []
    for lineno, raw in enumerate(lines, start=1):
        try:
            json.loads(raw)
        except json.JSONDecodeError as e:
            bad.append((lineno, str(e)))
    assert not bad, f"{len(bad)} corrupted lines: first 3 = {bad[:3]}"
