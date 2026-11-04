"""시나리오 적용값 → link.metrics 레코드(JSONL) 어댑터.

D03 결정(Q4-b): pydantic 모델(D05) 전까지 overview 필드를 단위 접미사로 기록한다.
envelope 필드명은 overview 4절을 따르되 값의 타입은 D05에서 확정한다(잠정: epoch ns).
"""

from __future__ import annotations

import uuid
from typing import Any

from pipe.link.scenario import Segment

SOURCE = "link.netem"
SCHEMA_VERSION = 1
MODE = "netem"


def link_metrics_record(
    *,
    run_id: str,
    scenario: str,
    segment_index: int,
    seg: Segment,
    event_ts_ns: int,
    mono_ns: int,
    event_id: str | None = None,
) -> dict[str, Any]:
    return {
        # envelope
        "event_id": event_id or str(uuid.uuid4()),
        "run_id": run_id,
        "source": SOURCE,
        "schema_version": SCHEMA_VERSION,
        "event_ts": event_ts_ns,
        "ingest_ts": None,  # ingest 단계에서 채운다
        # 링크 지표
        "mode": MODE,
        **seg.impairment(),
        # 조인·추적용(overview 표에 없는 추가 필드)
        "scenario": scenario,
        "segment_index": segment_index,
        "mono_ns": mono_ns,
    }
