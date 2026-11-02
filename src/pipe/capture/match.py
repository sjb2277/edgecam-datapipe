"""송수신 매칭: (ssrc, rtp_ts) 키로 sent.jsonl과 recv.jsonl을 조인해 지연을 낸다.

- 조인은 32비트 원값 그대로 한다. wrap 해제는 정렬·단조성 점검에만 쓴다.
- 매칭률 = 디코드된 매칭 프레임 수 / 송신 프레임 수
- 지연 = recv_mono_ns - capture_mono_ns (같은 호스트 CLOCK_MONOTONIC)

실행: uv run python -m pipe.capture.match --run-id <id>
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path
from statistics import mean

from pipe.capture import config as rec
from pipe.capture.config import CaptureConfig
from pipe.capture.rtp import unwrap

STAGES = {
    "total": ("capture_mono_ns", "recv_mono_ns"),
    "encode": ("capture_mono_ns", "send_mono_ns"),
    "network": ("send_mono_ns", "last_pkt_mono_ns"),
    "jitter_depay": ("last_pkt_mono_ns", "depay_mono_ns"),
    "decode": ("depay_mono_ns", "recv_mono_ns"),
}


def percentile(values: list[float], p: float) -> float:
    """nearest-rank 백분위수"""
    if not values:
        raise ValueError("empty values")
    s = sorted(values)
    k = max(1, math.ceil(p / 100 * len(s)))
    return s[k - 1]


def stats_ms(values_ns: list[int]) -> dict | None:
    if not values_ns:
        return None
    ms = [v / 1e6 for v in values_ns]
    return {
        "mean": round(mean(ms), 2),
        "p50": round(percentile(ms, 50), 2),
        "p95": round(percentile(ms, 95), 2),
        "max": round(max(ms), 2),
    }


def summarize(sent: list[dict], recv: list[dict]) -> dict:
    def key(r: dict) -> tuple[int, int]:
        return (r["ssrc"], r["rtp_ts"])

    sent_keys = Counter(key(r) for r in sent)
    decoded = [r for r in recv if r.get("decoded")]
    recv_keys = Counter(key(r) for r in decoded)
    recv_by_key = {key(r): r for r in decoded}

    matched = [(s, recv_by_key[key(s)]) for s in sent if key(s) in recv_by_key]
    lat: dict[str, list[int]] = {k: [] for k in STAGES}
    for s, r in matched:
        merged = {
            "capture_mono_ns": s["capture_mono_ns"],
            "send_mono_ns": s["send_mono_ns"],
            "last_pkt_mono_ns": r.get("last_pkt_mono_ns"),
            "depay_mono_ns": r.get("depay_mono_ns"),
            "recv_mono_ns": r.get("recv_mono_ns"),
        }
        for stage, (a, b) in STAGES.items():
            if merged.get(a) is not None and merged.get(b) is not None:
                lat[stage].append(merged[b] - merged[a])

    partial = sum(
        1 for s, r in matched if r.get("packets") is not None and r["packets"] < s["packets"]
    )
    # 단조성 점검(ssrc별, 송신 순서 기준)
    monotonic = True
    for ssrc in {r["ssrc"] for r in sent}:
        seq = [r["rtp_ts"] for r in sorted(sent, key=lambda r: r["frame_idx"]) if r["ssrc"] == ssrc]
        u = unwrap(seq)
        monotonic &= all(b > a for a, b in zip(u, u[1:], strict=False))

    n_sent = len(sent)
    return {
        "frames_sent": n_sent,
        "frames_decoded": len(decoded),
        "frames_matched": len(matched),
        "match_rate": round(len(matched) / n_sent, 4) if n_sent else None,
        "lost": n_sent - len(matched),
        "recv_not_decoded": sum(1 for r in recv if not r.get("decoded")),
        "recv_only": sum(1 for k in recv_keys if k not in sent_keys),
        "dup_sent_keys": sum(1 for c in sent_keys.values() if c > 1),
        "dup_recv_keys": sum(1 for c in recv_keys.values() if c > 1),
        "partial_frames": partial,
        "ssrc_count": len({r["ssrc"] for r in sent}),
        "rtp_ts_monotonic": monotonic,
        "latency_ms": {stage: stats_ms(v) for stage, v in lat.items()},
    }


def run(run_dir: Path) -> dict:
    sent = rec.read_jsonl(run_dir / rec.SENT_FILE)
    recv = rec.read_jsonl(run_dir / rec.RECV_FILE)
    out = {"run_id": run_dir.name, **summarize(sent, recv)}
    meta_path = run_dir / rec.SENDER_META
    if meta_path.exists():
        meta = rec.read_json(meta_path)
        out["mode"] = meta.get("mode")
        out["duration_s"] = round((meta["end_mono_ns"] - meta["start_mono_ns"]) / 1e9, 2)
    rec.write_json(run_dir / rec.SUMMARY_FILE, out)
    return out


def format_summary(s: dict) -> str:
    t = s["latency_ms"]["total"] or {}
    lines = [
        f"run={s['run_id']} mode={s.get('mode')} duration={s.get('duration_s')}s",
        f"sent={s['frames_sent']} matched={s['frames_matched']} "
        f"match_rate={s['match_rate']:.2%} lost={s['lost']}"
        if s["match_rate"] is not None
        else "sent=0",
        f"latency total: mean={t.get('mean')}ms p95={t.get('p95')}ms max={t.get('max')}ms",
    ]
    for stage in ("encode", "network", "jitter_depay", "decode"):
        st = s["latency_ms"][stage]
        if st:
            lines.append(f"  {stage:<12} mean={st['mean']}ms p95={st['p95']}ms")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="송수신 매칭과 지연 통계")
    p.add_argument("--run-id", required=True)
    p.add_argument("--json", action="store_true", help="요약 전체를 JSON으로 출력")
    args = p.parse_args(argv)
    s = run(CaptureConfig.from_env().run_dir(args.run_id))
    print(json.dumps(s, indent=2) if args.json else format_summary(s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
