"""시나리오 구간 → tc netem 명령 변환(순수 함수, 실행하지 않음).

단위 주의: tc에서 `kbps`는 킬로**바이트**/초, `kbit`은 킬로**비트**/초다.
시나리오의 rate_kbps(킬로비트/초)는 항상 `kbit`으로 넘긴다.
"""

from __future__ import annotations

from pipe.link.scenario import Segment


def _fmt(v: float) -> str:
    return f"{v:g}"


def netem_args(seg: Segment) -> list[str]:
    """`netem` 이후 인자. 열화가 없으면 ["netem"]만(통과)."""
    args = ["netem"]
    if seg.delay_ms > 0 or seg.jitter_ms > 0:
        args += ["delay", f"{_fmt(seg.delay_ms)}ms"]
        if seg.jitter_ms > 0:
            # D03 결정: 재정렬 허용(별도 child qdisc 없음)
            args += [f"{_fmt(seg.jitter_ms)}ms", "distribution", "normal"]
    if seg.loss_pct > 0:
        args += ["loss", f"{_fmt(seg.loss_pct)}%"]
    if seg.rate_kbps is not None:
        args += ["rate", f"{int(seg.rate_kbps)}kbit"]
    return args


def tc_replace_cmd(ns: str, dev: str, seg: Segment) -> list[str]:
    """멱등 적용: qdisc가 없으면 추가, 있으면 교체."""
    return ["ip", "netns", "exec", ns, "tc", "qdisc", "replace", "dev", dev, "root"] + netem_args(
        seg
    )


def tc_clear_cmd(ns: str, dev: str) -> list[str]:
    """root qdisc 삭제(기본 qdisc로 복귀). 없을 때의 실패는 호출 측에서 무시한다."""
    return ["ip", "netns", "exec", ns, "tc", "qdisc", "del", "dev", dev, "root"]


def tc_show_cmd(ns: str, dev: str) -> list[str]:
    return ["ip", "netns", "exec", ns, "tc", "-s", "qdisc", "show", "dev", dev]
