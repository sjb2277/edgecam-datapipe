"""링크 시나리오(scenarios/*.yaml) 로드·검증.

형식(D03 결정: 항상 구간 리스트, 계단식):

    name: delay
    description: 지연 100ms
    segments:
      - from_s: 0
        to_s: null        # null = run 끝까지 (마지막 구간만 허용)
        delay_ms: 100
        jitter_ms: 0
        loss_pct: 0
        rate_kbps: null   # null = 대역 제한 없음

- 구간은 0초에서 시작해 빈틈·겹침 없이 이어져야 한다(seg[i].to_s == seg[i+1].from_s).
- 생략한 열화 값은 0(rate_kbps는 제한 없음)으로 본다.
- 단위는 필드명에 붙인다: ms, pct(%), kbps(킬로비트/초).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_SEGMENT_KEYS = {"from_s", "to_s", "delay_ms", "jitter_ms", "loss_pct", "rate_kbps"}
_TOP_KEYS = {"name", "description", "segments"}


class ScenarioError(ValueError):
    pass


@dataclass(frozen=True)
class Segment:
    from_s: float
    to_s: float | None
    delay_ms: float = 0.0
    jitter_ms: float = 0.0
    loss_pct: float = 0.0
    rate_kbps: int | None = None

    def impairment(self) -> dict[str, Any]:
        """link.metrics에 기록할 적용값."""
        return {
            "delay_ms": self.delay_ms,
            "jitter_ms": self.jitter_ms,
            "loss_pct": self.loss_pct,
            "rate_kbps": self.rate_kbps,
        }


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    segments: tuple[Segment, ...]

    def segment_index_at(self, t_s: float) -> int:
        """경과 시간 t_s(초)에 적용할 구간 번호. 구간은 [from_s, to_s)."""
        if t_s < 0:
            raise ValueError(f"t_s must be >= 0: {t_s}")
        for i, seg in enumerate(self.segments):
            if seg.to_s is None or t_s < seg.to_s:
                return i
        # 마지막 구간이 to_s로 끝나는 경우: 끝난 뒤에도 마지막 값을 유지한다
        return len(self.segments) - 1

    def segment_at(self, t_s: float) -> Segment:
        return self.segments[self.segment_index_at(t_s)]


def _num(seg: dict, key: str, idx: int, default: float = 0.0) -> float:
    v = seg.get(key, default)
    if v is None:
        v = default
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise ScenarioError(f"segments[{idx}].{key}: 숫자여야 함 ({v!r})")
    if v < 0:
        raise ScenarioError(f"segments[{idx}].{key}: 음수 불가 ({v})")
    return float(v)


def _parse_segment(raw: Any, idx: int) -> Segment:
    if not isinstance(raw, dict):
        raise ScenarioError(f"segments[{idx}]: 매핑이어야 함")
    unknown = set(raw) - _SEGMENT_KEYS
    if unknown:
        raise ScenarioError(f"segments[{idx}]: 알 수 없는 키 {sorted(unknown)}")
    if "from_s" not in raw:
        raise ScenarioError(f"segments[{idx}].from_s 누락")
    from_s = _num(raw, "from_s", idx)
    to_raw = raw.get("to_s")
    to_s = None if to_raw is None else _num(raw, "to_s", idx)
    if to_s is not None and to_s <= from_s:
        raise ScenarioError(f"segments[{idx}]: to_s({to_s}) <= from_s({from_s})")
    loss = _num(raw, "loss_pct", idx)
    if loss > 100:
        raise ScenarioError(f"segments[{idx}].loss_pct: 100 초과 ({loss})")
    rate_raw = raw.get("rate_kbps")
    rate: int | None = None
    if rate_raw is not None:
        if isinstance(rate_raw, bool) or not isinstance(rate_raw, int) or rate_raw <= 0:
            raise ScenarioError(f"segments[{idx}].rate_kbps: 양의 정수여야 함 ({rate_raw!r})")
        rate = rate_raw
    return Segment(
        from_s=from_s,
        to_s=to_s,
        delay_ms=_num(raw, "delay_ms", idx),
        jitter_ms=_num(raw, "jitter_ms", idx),
        loss_pct=loss,
        rate_kbps=rate,
    )


def parse_scenario(data: Any) -> Scenario:
    if not isinstance(data, dict):
        raise ScenarioError("최상위는 매핑이어야 함")
    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise ScenarioError(f"알 수 없는 최상위 키 {sorted(unknown)}")
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ScenarioError("name 누락")
    raw_segs = data.get("segments")
    if not isinstance(raw_segs, list) or not raw_segs:
        raise ScenarioError("segments는 비어 있지 않은 리스트여야 함")
    segs = tuple(_parse_segment(r, i) for i, r in enumerate(raw_segs))
    if segs[0].from_s != 0:
        raise ScenarioError(f"segments[0].from_s는 0이어야 함 ({segs[0].from_s})")
    for i in range(len(segs) - 1):
        cur, nxt = segs[i], segs[i + 1]
        if cur.to_s is None:
            raise ScenarioError(f"segments[{i}]: 마지막 구간만 to_s를 비울 수 있음")
        if cur.to_s != nxt.from_s:
            kind = "겹침" if nxt.from_s < cur.to_s else "빈틈"
            raise ScenarioError(
                f"segments[{i}]→[{i + 1}] {kind}: to_s={cur.to_s}, from_s={nxt.from_s}"
            )
    return Scenario(name=name, description=str(data.get("description", "")), segments=segs)


def load_scenario(path: str | Path) -> Scenario:
    with Path(path).open(encoding="utf-8") as f:
        return parse_scenario(yaml.safe_load(f))
