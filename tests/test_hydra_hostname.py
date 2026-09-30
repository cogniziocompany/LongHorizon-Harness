"""Device-relay D2 (2026-09-29): Hydra's only address is now hydra.easybutt0n.ai.

The old hostname must not survive anywhere in tracked repo content outside
docs/handoffs/ (historical records are intentionally left unchanged).
Runs as part of the existing pytest suite / pr-gate CI step.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# The needle is assembled from fragments on purpose: the contiguous literal
# must never appear in tracked files outside docs/handoffs/, and this guard
# file is part of that set — a hardcoded needle would make the test fail on
# its own source.
OLD_HOST = "hydra." + "cognizioware" + ".com"
NEW_HOST = "hydra.easybutt0n.ai"


def test_old_hydra_hostname_absent_outside_handoffs():
    result = subprocess.run(
        [
            "git",
            "grep",
            "-F",
            "-l",
            "--",
            OLD_HOST,
            ".",
            ":(exclude)docs/handoffs",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    matches = result.stdout.strip()
    assert not matches, (
        f"{OLD_HOST} renamed to {NEW_HOST} on 2026-09-29 and must not appear "
        f"outside docs/handoffs/. Offending tracked files:\n{matches}"
    )
