"""Runs the node tests of the viewer's pure helpers (web/js/viewer-style.js) as part of the suite."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_viewer_style_js():
    r = subprocess.run(["node", "tests/data/viewer/style.test.mjs"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
