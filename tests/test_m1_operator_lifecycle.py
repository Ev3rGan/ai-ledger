from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _bash_executable() -> Path:
    candidates = (
        Path(os.environ.get("PROGRAMFILES", "")) / "Git" / "bin" / "bash.exe",
        Path("/bin/bash"),
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        probe = subprocess.run(
            (str(candidate), "-lc", "exit 0"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if probe.returncode == 0:
            return candidate
    pytest.skip("an executable POSIX bash runtime is required for lifecycle tests")


@pytest.mark.parametrize(
    "case_name",
    (
        "validate_no_side_effect",
        "qualification_failure_no_side_effect",
        "preflight_failure_no_side_effect",
        "upgrade_success",
        "upgrade_failure_recovery",
        "rollback_and_rerun",
        "rollback_failure_recovery",
        "ipam_failure_retry",
        "initial_start_failure_cleanup",
        "initial_start_preserves_preexisting_edge",
    ),
)
def test_m1_operator_lifecycle(case_name: str) -> None:
    project_root = Path(__file__).parents[1]
    harness = project_root / "tests" / "fixtures" / "m1_operator_lifecycle_harness.sh"
    operator = project_root / "deploy" / "m1" / "operate.sh"

    result = subprocess.run(
        (str(_bash_executable()), str(harness), str(operator), case_name),
        cwd=project_root,
        env={
            **os.environ,
            "QUALIFICATION_TEST_PYTHON": Path(sys.executable).as_posix(),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
