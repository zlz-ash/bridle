from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PLUGIN = "backend.tests.no_xfail_plugin"


def _run_isolated_pytest(test_file: Path, *, load_guard: bool) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "pytest"]
    if load_guard:
        command.extend(["-p", PLUGIN])
    command.extend([str(test_file), "-q"])
    return subprocess.run(
        command,
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


@pytest.mark.parametrize(
    "source",
    [
        "import pytest\n@pytest.mark.xfail\ndef test_non_strict_xpass():\n    assert True\n",
        "import pytest\n@pytest.mark.xfail(strict=True)\ndef test_strict_xpass():\n    assert True\n",
        "import pytest\n@pytest.mark.xfail\ndef test_expected_failure():\n    assert False\n",
        "import pytest\ndef test_dynamic_xfail():\n    pytest.xfail('dynamic')\n",
    ],
    ids=["non-strict-xpass", "strict-xpass", "xfail", "dynamic-xfail"],
)
def test_no_xfail_guard_rejects_all_xfail_outcomes(tmp_path: Path, source: str) -> None:
    test_file = tmp_path / "test_guard_violation.py"
    test_file.write_text(source, encoding="utf-8")

    result = _run_isolated_pytest(test_file, load_guard=True)

    assert result.returncode != 0, result.stdout + result.stderr


def test_no_xfail_guard_preserves_clean_pytest_behavior(tmp_path: Path) -> None:
    test_file = tmp_path / "test_clean_control.py"
    test_file.write_text("def test_clean():\n    assert True\n", encoding="utf-8")

    guarded = _run_isolated_pytest(test_file, load_guard=True)
    unguarded = _run_isolated_pytest(test_file, load_guard=False)

    assert guarded.returncode == 0, guarded.stdout + guarded.stderr
    assert unguarded.returncode == 0, unguarded.stdout + unguarded.stderr
