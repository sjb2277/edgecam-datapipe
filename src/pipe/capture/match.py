"""송수신 매칭: (ssrc, rtp_ts) 키로 sent.jsonl과 recv.jsonl을 조인해 지연을 낸다.

- 조인은 32비트 원값 그대로 한다. wrap 해제는 정렬·단조성 점검에만 쓴다.
- 매칭률 = 디코드 출력이 있는 송신 프레임 비율
- 송신 프레임 분류(수신 패킷 수는 디코드 시점까지 udpsrc에 도착한 수):
  완전   디코드 출력 있음, 수신 패킷 수 = 송신 패킷 수
  불완전 디코드 출력 있음, 수신 패킷 수 < 송신 패킷 수 (비율은 incomplete_rate로 따로 기록)
  손실   디코드 출력 없음
  한계: 패킷이 모두 도착해도 앞 프레임 손실로 화면이 깨진 프레임은 구분하지 못한다.
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

LIMITATION = "한계: 패킷이 모두 도착해도 앞 프레임 손실로 화면이 깨진 프레임은 '완전'으로 분류된다."

DEFINITIONS = {
    "match_rate": "디코드 출력이 있는 송신 프레임 비율",
    "frames_complete": "디코드 출력 있음, 수신 패킷 수 = 송신 패킷 수",
    "frames_incomplete": "디코드 출력 있음, 수신 패킷 수 < 송신 패킷 수",
    "frames_lost": "디코드 출력 없음",
    "incomplete_rate": "frames_incomplete / frames_sent",
    "decoded_packets_unknown": "디코드 출력은 있으나 수신 패킷 집계가 없는 프레임(분류 제외)",
    "decoded_packets_over": "수신 패킷 수 > 송신 패킷 수(중복 수신 등, 분류 제외)",
    "packets_recv": "송신 키의 수신 패킷 합(디코드 여부 무관, 디코드 후 늦게 온 패킷 제외)",
    "match_rate_note": (
        "match_rate는 수신기 PTS 매핑 실패(decoded_unmapped)를 손실로 센다. decoder_outputs와"
        " 비교해 해석한다(예: d03-normal의 96.83%는 매핑 실패 58프레임이 포함된 값)."
    ),
    "send_fps": "(송신 프레임 수 - 1) / (마지막 - 첫 capture_mono_ns)",
    "recv_fps": "(매핑된 디코드 프레임 수 - 1) / (마지막 - 첫 recv_mono_ns)",
    "decoder_outputs": "recv_meta.json의 frames_decoded + decoded_unmapped(디코더 출력 전체)",
    "limitation": LIMITATION,
}

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


def recv_packets(r: dict) -> int | None:
    """D03 이전 run(recv.jsonl의 packets)도 읽는다."""
    return r.get("packets_recv", r.get("packets"))


def classify(sent_packets: int, r: dict | None) -> str:
    """송신 프레임 1개의 수신 상태: complete | incomplete | lost | unknown | over"""
    if r is None:
        return "lost"
    got = recv_packets(r)
    if got is None:
        return "unknown"
    if got == sent_packets:
        return "complete"
    return "incomplete" if got < sent_packets else "over"


def span_fps(times_ns: list[int]) -> float | None:
    """구간 fps = (개수 - 1) / (마지막 - 첫 시각). 2개 미만이거나 시간 범위가 0이면 None."""
    if len(times_ns) < 2:
        return None
    span = max(times_ns) - min(times_ns)
    return round((len(times_ns) - 1) / (span / 1e9), 2) if span > 0 else None


def decoder_stats(recv_meta: dict | None) -> tuple[int | None, int | None]:
    """(decoder_outputs, decoded_unmapped). recv_meta가 없으면 (None, None)."""
    if not recv_meta or recv_meta.get("frames_decoded") is None:
        return None, None
    unmapped = int(recv_meta.get("decoded_unmapped") or 0)
    return recv_meta["frames_decoded"] + unmapped, unmapped


def summarize(sent: list[dict], recv: list[dict], recv_meta: dict | None = None) -> dict:
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

    cls = Counter(classify(s["packets"], recv_by_key.get(key(s))) for s in sent)
    pkts_sent = sum(s["packets"] for s in sent)
    pkts_recv = sum(recv_packets(r) or 0 for r in recv if key(r) in sent_keys)
    # 단조성 점검(ssrc별, 송신 순서 기준)
    monotonic = True
    for ssrc in {r["ssrc"] for r in sent}:
        seq = [r["rtp_ts"] for r in sorted(sent, key=lambda r: r["frame_idx"]) if r["ssrc"] == ssrc]
        u = unwrap(seq)
        monotonic &= all(b > a for a, b in zip(u, u[1:], strict=False))

    n_sent = len(sent)
    dec_outputs, dec_unmapped = decoder_stats(recv_meta)
    return {
        "frames_sent": n_sent,
        "frames_decoded": len(decoded),
        "frames_matched": len(matched),
        "match_rate": round(len(matched) / n_sent, 4) if n_sent else None,
        "frames_complete": cls["complete"],
        "frames_incomplete": cls["incomplete"],
        "frames_lost": cls["lost"],
        "incomplete_rate": round(cls["incomplete"] / n_sent, 4) if n_sent else None,
        "decoded_packets_unknown": cls["unknown"],
        "decoded_packets_over": cls["over"],
        "packets_sent": pkts_sent,
        "packets_recv": pkts_recv,
        "packet_loss_rate": round(1 - pkts_recv / pkts_sent, 4) if pkts_sent else None,
        "send_fps": span_fps([r["capture_mono_ns"] for r in sent]),
        "recv_fps": span_fps([r["recv_mono_ns"] for r in decoded if r.get("recv_mono_ns")]),
        "decoder_outputs": dec_outputs,
        "decoded_unmapped": dec_unmapped,
        "recv_not_decoded": sum(1 for r in recv if not r.get("decoded")),
        "recv_only": sum(1 for k in recv_keys if k not in sent_keys),
        "dup_sent_keys": sum(1 for c in sent_keys.values() if c > 1),
        "dup_recv_keys": sum(1 for c in recv_keys.values() if c > 1),
        "ssrc_count": len({r["ssrc"] for r in sent}),
        "rtp_ts_monotonic": monotonic,
        "latency_ms": {stage: stats_ms(v) for stage, v in lat.items()},
        "definitions": DEFINITIONS,
    }


def run(run_dir: Path) -> dict:
    sent = rec.read_jsonl(run_dir / rec.SENT_FILE)
    recv = rec.read_jsonl(run_dir / rec.RECV_FILE)
    rmeta_path = run_dir / rec.RECEIVER_META
    rmeta = rec.read_json(rmeta_path) if rmeta_path.exists() else None
    out = {"run_id": run_dir.name, **summarize(sent, recv, rmeta)}
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
        f"match_rate={s['match_rate']:.2%} | complete={s['frames_complete']} "
        f"incomplete={s['frames_incomplete']} ({s['incomplete_rate']:.2%}) lost={s['frames_lost']}"
        if s["match_rate"] is not None
        else "sent=0",
        f"packets sent={s['packets_sent']} recv={s['packets_recv']}"
        + (f" loss={s['packet_loss_rate']:.2%}" if s["packet_loss_rate"] is not None else "")
        + (
            f" | unclassified unknown={s['decoded_packets_unknown']}"
            f" over={s['decoded_packets_over']}"
            if s["decoded_packets_unknown"] or s["decoded_packets_over"]
            else ""
        ),
        f"fps send={s.get('send_fps')} recv={s.get('recv_fps')}"
        f" | decoder_outputs={s.get('decoder_outputs')} (unmapped {s.get('decoded_unmapped')})",
        f"latency total: mean={t.get('mean')}ms p95={t.get('p95')}ms max={t.get('max')}ms",
    ]
    if s.get("decoded_unmapped"):
        lines.insert(
            3,
            f"주의: 디코더 출력 {s['decoded_unmapped']}개가 PTS 매핑에 실패해 lost에 포함됨"
            " (match_rate가 실제 디코드율보다 낮다)",
        )
    for stage in ("encode", "network", "jitter_depay", "decode"):
        st = s["latency_ms"][stage]
        if st:
            lines.append(f"  {stage:<12} mean={st['mean']}ms p95={st['p95']}ms")
    lines.append(LIMITATION)
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
