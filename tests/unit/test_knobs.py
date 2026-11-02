import json

import pytest

from pipe.capture import config as rec
from pipe.capture.knobs import (
    KnobEvent,
    analyze,
    evaluate_bitrate_event,
    evaluate_caps_event,
    parse_schedule,
)

NS = 1_000_000_000


def test_parse_schedule_sorted():
    ev = parse_schedule("40:bitrate=2000, 20:bitrate=500,30:caps=320x240@15")
    assert ev == [
        KnobEvent(20.0, "bitrate", 500),
        KnobEvent(30.0, "caps", (320, 240, 15)),
        KnobEvent(40.0, "bitrate", 2000),
    ]
    assert parse_schedule("") == []


@pytest.mark.parametrize("bad", ["20bitrate=1", "20:fps=10", "20:caps=320x240", "x:bitrate=1"])
def test_parse_schedule_rejects(bad):
    with pytest.raises(ValueError):
        parse_schedule(bad)


def frames(t0_s: float, t1_s: float, kbps: float, fps: int = 30, start_idx: int = 0) -> list[dict]:
    """일정 비트레이트의 합성 송신 기록"""
    n = int((t1_s - t0_s) * fps)
    per = int(kbps * 1000 / 8 / fps)
    return [
        {
            "frame_idx": start_idx + i,
            "ssrc": 1,
            "bytes": per,
            "send_mono_ns": int((t0_s + i / fps) * NS),
        }
        for i in range(n)
    ]


def test_bitrate_applied_after_one_second():
    sent = frames(0, 11, 2000) + frames(11, 20, 500, start_idx=330)  # 이벤트 10s, 적용 11s
    r = evaluate_bitrate_event(sent, 10 * NS, 500, 20 * NS)
    assert r["apply_delay_s"] == 1.0
    assert r["pass"] is True
    assert abs(r["steady_err"]) < 0.02


def test_bitrate_not_applied_fails():
    sent = frames(0, 20, 2000)
    r = evaluate_bitrate_event(sent, 10 * NS, 500, 20 * NS)
    assert r["apply_delay_s"] is None and r["pass"] is False


def test_bitrate_applied_too_late_fails():
    sent = frames(0, 14, 2000) + frames(14, 20, 500, start_idx=420)
    r = evaluate_bitrate_event(sent, 10 * NS, 500, 20 * NS, max_delay_s=2.0)
    assert r["apply_delay_s"] == 4.0 and r["pass"] is False


def test_caps_event_measures_switch():
    recv = [
        {"decoded": True, "recv_mono_ns": int(i / 30 * NS), "width": 640, "height": 480}
        for i in range(300)
    ] + [
        {"decoded": True, "recv_mono_ns": int((10.2 + i / 15) * NS), "width": 320, "height": 240}
        for i in range(150)
    ]
    r = evaluate_caps_event(recv, 10 * NS, (320, 240, 15), 20 * NS)
    assert r["switched"] is True
    assert r["new_res_delay_ms"] == pytest.approx(200, abs=1)
    assert r["measured_fps_after"] == pytest.approx(15, abs=0.5)
    assert r["max_decode_gap_ms"] == pytest.approx(233, abs=2)


def write_run(path, sent: list[dict], events: list[dict]) -> None:
    rec.write_json(
        path / rec.SENDER_META,
        {
            "run_id": "r",
            "start_mono_ns": 0,
            "end_mono_ns": 20 * NS,
            "knob_events": events,
            "errors": [],
        },
    )
    (path / rec.SENT_FILE).write_text("\n".join(json.dumps(r) for r in sent))
    (path / rec.RECV_FILE).write_text("")


BITRATE_EVENT = {"t_s": 10, "kind": "bitrate", "value": 500, "mono_ns": 10 * NS}


def test_analyze_run_dir(tmp_path):
    write_run(tmp_path, frames(0, 11, 2000) + frames(11, 20, 500, start_idx=330), [BITRATE_EVENT])
    out = analyze(tmp_path)
    assert out["initial"]["kbps"] == pytest.approx(2000, rel=0.02)
    assert out["integrity"]["sender_frame_idx_gaps"] == 0
    assert out["bitrate_runtime_change"] == "pass"


def test_analyze_bitrate_not_applied_is_fail(tmp_path):
    write_run(tmp_path, frames(0, 20, 2000), [BITRATE_EVENT])
    assert analyze(tmp_path)["bitrate_runtime_change"] == "fail"


def test_analyze_without_bitrate_event_is_not_tested(tmp_path):
    caps = {"t_s": 10, "kind": "caps", "value": [320, 240, 15], "mono_ns": 10 * NS}
    write_run(tmp_path, frames(0, 20, 2000), [caps])
    assert analyze(tmp_path)["bitrate_runtime_change"] == "not_tested"
    write_run(tmp_path, frames(0, 20, 2000), [])
    assert analyze(tmp_path)["bitrate_runtime_change"] == "not_tested"
