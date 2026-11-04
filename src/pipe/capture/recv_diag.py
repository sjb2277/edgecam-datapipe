"""수신기 진단 보조(순수 Python, gi 없음, 단위 테스트 대상).

receiver.py에서 옮겼다: PTS 매핑 진단 기록, GStreamer 디버그 로그 설정·크기 상한 파일,
/proc 기반 UDP 카운터·소켓 버퍼 값 읽기. GStreamer 객체에 의존하지 않는다.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

CLOCK_TIME_NONE = (1 << 64) - 1  # Gst.CLOCK_TIME_NONE
GST_DEBUG_FILE = "gst_debug.log"
GST_DEBUG_MAX_BYTES = 20_000_000


def _pts(v: int) -> int | None:
    return None if v is None or v == CLOCK_TIME_NONE else int(v)


def unmapped_sample(
    pts: int, dts: int, duration: int, mono_ns: int, last_mapped: int | None, pending: Iterable[int]
) -> dict[str, Any]:
    """매핑 실패한 출력 버퍼 1개의 진단 기록(매핑 표 상태 포함)."""
    pend = list(pending)
    return {
        "pts_ns": _pts(pts),
        "dts_ns": _pts(dts),
        "duration_ns": _pts(duration),
        "mono_ns": mono_ns,
        "last_mapped_pts_ns": last_mapped,
        "pending_count": len(pend),
        "pending_pts_min_ns": min(pend) if pend else None,
        "pending_pts_max_ns": max(pend) if pend else None,
    }


def leftover_summary(pending: Iterable[int], limit: int) -> dict[str, Any]:
    """종료 시 매핑 표에 남은 PTS: 개수와 오름차순 앞 limit개."""
    left = sorted(pending)
    return {"pts_key_leftover": len(left), "pts_key_leftover_sample_ns": left[:limit]}


def gst_debug_settings(
    spec: str | None, max_bytes: int | None, env: Mapping[str, str]
) -> tuple[str | None, int]:
    """명령줄 인자가 우선, 없으면 RECV_GST_DEBUG(_MAX_BYTES). 빈 문자열이면 비활성(None)."""
    spec = spec if spec is not None else env.get("RECV_GST_DEBUG", "")
    if max_bytes is None:
        max_bytes = int(env.get("RECV_GST_DEBUG_MAX_BYTES", GST_DEBUG_MAX_BYTES))
    if max_bytes <= 0:
        raise ValueError(f"gst debug max bytes must be > 0: {max_bytes}")
    return (spec.strip() or None), max_bytes


def format_gst_line(mono_ns: int, level: str, category: str, obj: str, func: str, msg: str) -> str:
    return f"{mono_ns} {level:<7} {category:<20} <{obj}> {func}: {msg}\n"


class CappedLog:
    """바이트 상한이 있는 로그 파일. 상한을 넘는 줄부터 쓰지 않고 표시 한 줄만 남긴다."""

    def __init__(self, path: Path, max_bytes: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.max_bytes = max_bytes
        self._f = path.open("wb")
        self._lock = threading.Lock()
        self.bytes_written = 0
        self.lines_written = 0
        self.lines_dropped = 0
        self.truncated = False
        self.closed = False

    def write(self, line: str) -> None:
        data = line.encode("utf-8", errors="replace")
        with self._lock:
            if self.closed or self.truncated:
                self.lines_dropped += 0 if self.closed else 1
                return
            if self.bytes_written + len(data) > self.max_bytes:
                self.truncated = True
                self.lines_dropped += 1
                self._f.write(f"# truncated at {self.max_bytes} bytes\n".encode())
                return
            self._f.write(data)
            self.bytes_written += len(data)
            self.lines_written += 1

    def close(self) -> None:
        with self._lock:
            if not self.closed:
                self._f.close()
                self.closed = True

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "path": str(self.path),
                "max_bytes": self.max_bytes,
                "bytes_written": self.bytes_written,
                "lines_written": self.lines_written,
                "lines_dropped": self.lines_dropped,
                "truncated": self.truncated,
            }


def read_udp_snmp(path: str = "/proc/net/snmp") -> dict[str, int]:
    """/proc/net/snmp의 Udp 헤더·값 줄을 dict로 읽는다. 읽을 수 없으면 빈 dict."""
    try:
        with open(path, encoding="ascii") as f:
            rows = [line.split() for line in f if line.startswith("Udp:")]
    except OSError:
        return {}
    if len(rows) < 2:
        return {}
    return {k: int(v) for k, v in zip(rows[0][1:], rows[1][1:], strict=False)}


def snmp_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {k: after[k] - before[k] for k in after if k in before}


def _read_int(path: str) -> int | None:
    try:
        with open(path, encoding="ascii") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None
