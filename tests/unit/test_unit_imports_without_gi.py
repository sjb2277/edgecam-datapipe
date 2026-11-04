"""단위 테스트가 gi(PyGObject) 없이 수집되는지 확인한다.

CI는 gst 의존성 그룹을 설치하지 않는다. 로컬 venv에는 gi가 있어 단위 테스트가 실수로 gi를
import해도 드러나지 않으므로, gi import를 막은 하위 프로세스에서 단위 테스트를 수집해 본다.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SELF = Path(__file__).resolve().relative_to(ROOT)

BLOCK_GI_AND_COLLECT = (
    "import sys; sys.modules['gi'] = None; import pytest; sys.exit(pytest.main(sys.argv[1:]))"
)


def test_unit_tests_collect_without_gi():
    r = subprocess.run(
        [sys.executable, "-c", BLOCK_GI_AND_COLLECT,
         "--collect-only", "-q", "-m", "not integration",
         "-p", "no:cacheprovider", f"--ignore={SELF}"],
        cwd=ROOT, capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
