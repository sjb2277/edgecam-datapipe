"""netns + netem 통합 테스트(sudo 필요, CI 제외).

실행: sudo -v && uv run pytest -m "integration and netns" tests/integration/test_netns_link.py
- `make link-up`이 되어 있어야 한다(테스트는 네임스페이스를 만들거나 지우지 않는다).
- sudo 자격 캐시가 없거나 ue/edge가 없으면 skip한다.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

from pipe.link.netem import tc_clear_cmd, tc_replace_cmd, tc_show_cmd
from pipe.link.scenario import Segment

pytestmark = [pytest.mark.integration, pytest.mark.netns]

UE = os.environ.get("LINK_NS_UE", "ue")
EDGE = os.environ.get("LINK_NS_EDGE", "edge")
DEV = os.environ.get("LINK_DEV_UE", "veth-ue")
EDGE_IP = os.environ.get("LINK_EDGE_IP", "10.250.0.2")
PORT = "5095"  # 측정 기본 포트(5004)·루프백 테스트(5094)와 겹치지 않게


def _sudo(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["sudo", "-n", *cmd], capture_output=True, text=True, **kw)


def _ready() -> str | None:
    if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode != 0:
        return "sudo 자격 캐시 없음(sudo -v 필요)"
    names = subprocess.run(["ip", "netns", "list"], capture_output=True, text=True).stdout.split()
    if UE not in names or EDGE not in names:
        return f"netns {UE}/{EDGE} 없음(make link-up 필요)"
    return None


_reason = _ready()
if _reason:
    pytestmark.append(pytest.mark.skip(reason=_reason))


@pytest.fixture
def netem():
    def apply(seg: Segment) -> None:
        r = _sudo(tc_replace_cmd(UE, DEV, seg))
        assert r.returncode == 0, r.stderr

    yield apply
    _sudo(tc_clear_cmd(UE, DEV))


def _ping_avg_ms(count: int = 5) -> float:
    r = _sudo(["ip", "netns", "exec", UE, "ping", "-c", str(count), "-i", "0.2", EDGE_IP])
    assert r.returncode == 0, r.stdout + r.stderr
    m = re.search(r"= [\d.]+/([\d.]+)/", r.stdout)
    assert m, r.stdout
    return float(m.group(1))


def test_netem_delay_visible_in_rtt(netem):
    base = _ping_avg_ms()
    netem(Segment(from_s=0, to_s=None, delay_ms=50))
    shown = _sudo(tc_show_cmd(UE, DEV)).stdout
    assert "delay 50ms" in shown
    delayed = _ping_avg_ms()
    # ue→edge 단방향에만 걸었으므로 RTT는 약 +50ms
    assert delayed - base >= 45, (base, delayed)


def test_send_recv_across_netns(tmp_path, netem):
    netem(Segment(from_s=0, to_s=None, delay_ms=20))
    user = os.environ.get("USER") or subprocess.run(["id", "-un"], capture_output=True,
                                                    text=True).stdout.strip()  # fmt: skip
    env_args = [f"HOME={os.environ['HOME']}", f"PATH={os.environ['PATH']}",
                f"RUNS_DIR={tmp_path}", f"RTP_PORT={PORT}"]  # fmt: skip

    def in_ns(ns: str, mod: str, *args: str) -> list[str]:
        return ["sudo", "-n", "ip", "netns", "exec", ns, "sudo", "-u", user, "env", *env_args,
                sys.executable, "-m", mod, *args]  # fmt: skip

    recv = subprocess.Popen(
        in_ns(EDGE, "pipe.capture.receiver", "--run-id", "it-ns", "--duration", "30",
              "--idle-timeout", "2"),
        stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        assert "READY" in recv.stdout.readline()
        send = subprocess.run(
            in_ns(UE, "pipe.capture.sender", "--run-id", "it-ns", "--mode", "test",
                  "--duration", "5", "--host", EDGE_IP),
            capture_output=True, text=True, timeout=30,
        )  # fmt: skip
        assert send.returncode == 0, send.stderr + send.stdout
        assert recv.wait(timeout=30) == 0
    finally:
        if recv.poll() is None:
            recv.kill()

    from pipe.capture.match import run as match_run

    s = match_run(tmp_path / "it-ns")
    assert s["match_rate"] >= 0.99
    assert s["latency_ms"]["total"]["mean"] >= 20  # netem 20ms가 지연에 반영
