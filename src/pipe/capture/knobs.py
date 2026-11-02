"""실행 중 노브(비트레이트·해상도·fps) 변경의 스케줄 파싱과 판정(순수 Python).

판정 기준(비트레이트):
  - 변경 후 1초 창 평균 비트레이트가 목표의 ±tol(기본 20%)에 연속 2창 들어오는
    첫 시점까지의 지연(apply_delay_s)이 max_delay_s(기본 2초) 이하
  - 파이프라인 재시작 없음(ssrc 1개), 송신 frame_idx 연속, 수신 오류 0
  비트레이트는 RTP 패킷 크기(헤더 포함) 기준이다.

해상도·fps는 통과 기준 대신 측정값을 보고한다: 새 해상도 첫 디코드까지 지연,
전환 구간 최대 디코드 공백, 전환 후 실측 fps.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any

from pipe.capture import config as records
from pipe.capture.config import CaptureConfig

NS = 1_000_000_000


@dataclass(frozen=True)
class KnobEvent:
    t_s: float
    kind: str  # "bitrate" | "caps"
    value: Any  # int kbps | (w, h, fps)


def parse_caps_value(text: str) -> tuple[int, int, int]:
    """'320x240@15' → (320, 240, 15)"""
    size, _, fps = text.partition("@")
    w, _, h = size.partition("x")
    if not (w and h and fps):
        raise ValueError(f"caps value must be WxH@FPS: {text!r}")
    return int(w), int(h), int(fps)


def parse_schedule(text: str) -> list[KnobEvent]:
    """'20:bitrate=500,40:caps=320x240@15' → 시각순 KnobEvent 목록"""
    events: list[KnobEvent] = []
    for item in filter(None, (p.strip() for p in text.split(","))):
        t, _, kv = item.partition(":")
        kind, _, val = kv.partition("=")
        if not (t and kind and val):
            raise ValueError(f"schedule item must be T:kind=value: {item!r}")
        if kind == "bitrate":
            value: Any = int(val)
        elif kind == "caps":
            value = parse_caps_value(val)
        else:
            raise ValueError(f"unknown knob: {kind!r}")
        events.append(KnobEvent(float(t), kind, value))
    return sorted(events, key=lambda e: e.t_s)


def kbps_in_window(sent: list[dict], t0_ns: int, t1_ns: int) -> float:
    total = sum(r["bytes"] for r in sent if t0_ns <= r["send_mono_ns"] < t1_ns)
    return total * 8 / ((t1_ns - t0_ns) / NS) / 1000


def evaluate_bitrate_event(
    sent: list[dict],
    event_ns: int,
    target_kbps: int,
    end_ns: int,
    tol: float = 0.2,
    max_delay_s: float = 2.0,
) -> dict:
    windows: list[tuple[float, float]] = []
    t = event_ns
    while t + NS <= end_ns:
        windows.append(((t - event_ns) / NS, kbps_in_window(sent, t, t + NS)))
        t += NS

    def ok(kbps: float) -> bool:
        return abs(kbps - target_kbps) <= tol * target_kbps

    apply_idx = next(
        (i for i in range(len(windows) - 1) if ok(windows[i][1]) and ok(windows[i + 1][1])),
        None,
    )
    apply_delay = windows[apply_idx][0] if apply_idx is not None else None
    steady = [k for _, k in windows[apply_idx:]] if apply_idx is not None else []
    steady_mean = mean(steady) if steady else None
    return {
        "target_kbps": target_kbps,
        "windows_kbps": [round(k, 1) for _, k in windows],
        "apply_delay_s": apply_delay,
        "steady_mean_kbps": round(steady_mean, 1) if steady_mean is not None else None,
        "steady_err": round(steady_mean / target_kbps - 1, 3) if steady_mean else None,
        "pass": apply_delay is not None and apply_delay <= max_delay_s,
    }


def evaluate_caps_event(
    recv: list[dict], event_ns: int, new: tuple[int, int, int], end_ns: int
) -> dict:
    w, h, fps = new
    decoded = sorted(
        (r for r in recv if r.get("decoded") and r.get("recv_mono_ns")),
        key=lambda r: r["recv_mono_ns"],
    )
    first_new = next(
        (
            r
            for r in decoded
            if r["recv_mono_ns"] >= event_ns and r.get("width") == w and r.get("height") == h
        ),
        None,
    )
    around = [r["recv_mono_ns"] for r in decoded if event_ns - NS <= r["recv_mono_ns"] <= end_ns]
    gaps = [b - a for a, b in zip(around, around[1:], strict=False)]
    measured_fps = None
    if first_new is not None:
        t0 = first_new["recv_mono_ns"] + NS  # 전환 직후 1초는 제외
        n = sum(1 for r in decoded if t0 <= r["recv_mono_ns"] < end_ns)
        if end_ns - t0 >= NS:
            measured_fps = round(n / ((end_ns - t0) / NS), 1)
    return {
        "target": {"width": w, "height": h, "fps": fps},
        "new_res_delay_ms": (
            round((first_new["recv_mono_ns"] - event_ns) / 1e6, 1) if first_new else None
        ),
        "max_decode_gap_ms": round(max(gaps) / 1e6, 1) if gaps else None,
        "measured_fps_after": measured_fps,
        "switched": first_new is not None,
    }


def analyze(run_dir: Path, tol: float = 0.2, max_delay_s: float = 2.0) -> dict:
    meta = records.read_json(run_dir / records.SENDER_META)
    sent = records.read_jsonl(run_dir / records.SENT_FILE)
    recv = records.read_jsonl(run_dir / records.RECV_FILE)
    rmeta_path = run_dir / records.RECEIVER_META
    rmeta = records.read_json(rmeta_path) if rmeta_path.exists() else {}

    events = meta.get("knob_events", [])
    end_all = meta["end_mono_ns"]
    idx = sorted(r["frame_idx"] for r in sent)
    frame_idx_gaps = sum(1 for a, b in zip(idx, idx[1:], strict=False) if b - a != 1)
    ssrcs = sorted({r["ssrc"] for r in sent})

    results = []
    initial = {
        "window": "start→first event",
        "kbps": None,
    }
    if events and sent:
        initial["kbps"] = round(
            kbps_in_window(sent, meta["start_mono_ns"], events[0]["mono_ns"]), 1
        )
    for i, ev in enumerate(events):
        end_ns = events[i + 1]["mono_ns"] if i + 1 < len(events) else end_all
        if ev["kind"] == "bitrate":
            res = evaluate_bitrate_event(sent, ev["mono_ns"], ev["value"], end_ns, tol, max_delay_s)
        else:
            res = evaluate_caps_event(recv, ev["mono_ns"], tuple(ev["value"]), end_ns)
        results.append({"t_s": ev["t_s"], "kind": ev["kind"], **res})

    integrity = {
        "ssrc_count": len(ssrcs),
        "sender_frame_idx_gaps": frame_idx_gaps,
        "sender_errors": len(meta.get("errors", [])),
        "receiver_errors": len(rmeta.get("errors", [])),
        "receiver_warnings": len(rmeta.get("warnings", [])),
    }
    integrity_ok = (
        integrity["ssrc_count"] == 1
        and integrity["sender_frame_idx_gaps"] == 0
        and integrity["sender_errors"] == 0
        and integrity["receiver_errors"] == 0
    )
    bitrate_events = [r for r in results if r["kind"] == "bitrate"]
    if not bitrate_events:
        verdict = "not_tested"
    elif integrity_ok and all(r["pass"] for r in bitrate_events):
        verdict = "pass"
    else:
        verdict = "fail"
    out = {
        "run_id": meta["run_id"],
        "criteria": {"tol": tol, "max_delay_s": max_delay_s},
        "initial": initial,
        "events": results,
        "integrity": integrity,
        "bitrate_runtime_change": verdict,  # pass | fail | not_tested
    }
    return out


def main(argv: list[str] | None = None) -> int:

    p = argparse.ArgumentParser(description="노브 변경 run 판정")
    p.add_argument("--run-id", required=True)
    p.add_argument("--tol", type=float, default=0.2)
    p.add_argument("--max-delay-s", type=float, default=2.0)
    args = p.parse_args(argv)
    run_dir = CaptureConfig.from_env().runs_dir / args.run_id
    out = analyze(run_dir, args.tol, args.max_delay_s)
    records.write_json(run_dir / records.KNOBS_FILE, out)

    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out["integrity"]["receiver_errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
