"""Smoke tests for scripts/run_polymarket.py's CLI.

The unit suite never drives main()'s argparse, so a 'referenced-but-undefined'
CLI arg (e.g. main() reading args.exposure_budget while --exposure-budget was
never added) passes 539 green and then crashes at runtime. These tests close
that gap. See the 2026-05-31 AttributeError regression.
"""

import re
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "run_polymarket.py"


def test_help_builds_parser_and_exits_zero():
    """--help must build the FULL parser and exit 0 (catches malformed
    add_argument calls). Hermetic: argparse exits before any network/keys."""
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, f"--help failed:\n{r.stderr}"
    assert "--exposure-budget" in r.stdout
    assert "--min-edge" in r.stdout


def test_every_args_attribute_is_defined():
    """Every args.<name> READ in run_polymarket.py must be defined via
    add_argument (or assigned in code). Directly catches the regression where
    main() read args.exposure_budget / args.min_edge that argparse never defined.
    """
    src = SCRIPT.read_text()
    defined = {
        m.group(1).lstrip("-").replace("-", "_")
        for m in re.finditer(r'add_argument\(\s*"(--?[\w-]+)"', src)
    }
    # Attributes set in code (e.g. `args.live = False`) exist at runtime too.
    assigned = set(re.findall(r"\bargs\.(\w+)\s*=", src))
    referenced = set(re.findall(r"\bargs\.(\w+)", src))
    missing = referenced - defined - assigned
    assert not missing, f"main() uses undefined CLI args: {sorted(missing)}"
