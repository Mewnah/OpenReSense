import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_globe_helpers():
    r = subprocess.run(["node", "--test", str(ROOT / "tests" / "globe_helpers.test.mjs")],
                       capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr
