import pytest

from pipe.capture.match import LIMITATION, classify, format_summary, percentile, summarize

MS = 1_000_000


def sent(i: int, ts: int, ssrc: int = 1, packets: int = 2) -> dict:
    base = i * 33 * MS
    return {
        "frame_idx": i,
        "ssrc": ssrc,
        "rtp_ts": ts,
        "packets": packets,
        "bytes": 1000,
        "capture_mono_ns": base,
        "send_mono_ns": base + 2 * MS,
    }


def recv(i: int, ts: int, lat_ms: int = 10, ssrc: int = 1, packets: int = 2, decoded=True) -> dict:
    base = i * 33 * MS
    return {
        "ssrc": ssrc,
        "rtp_ts": ts,
        "packets_recv": packets,
        "last_pkt_mono_ns": base + 3 * MS,
        "depay_mono_ns": base + 4 * MS,
        "recv_mono_ns": base + lat_ms * MS,
        "decoded": decoded,
    }


def test_percentile_nearest_rank():
    v = list(range(1, 101))
    assert percentile(v, 95) == 95
    assert percentile(v, 50) == 50
    assert percentile([7], 95) == 7
    with pytest.raises(ValueError):
        percentile([], 50)


def test_all_matched_latency():
    s = [sent(i, 1000 + i * 3000) for i in range(10)]
    r = [recv(i, 1000 + i * 3000, lat_ms=10) for i in range(10)]
    out = summarize(s, r)
    assert out["match_rate"] == 1.0
    assert out["latency_ms"]["total"]["mean"] == 10.0
    assert out["latency_ms"]["encode"]["mean"] == 2.0
    assert out["latency_ms"]["decode"]["mean"] == 6.0
    assert out["rtp_ts_monotonic"] is True
    assert out["frames_complete"] == 10 and out["frames_incomplete"] == 0
    text = format_summary({"run_id": "x", **out})
    assert "match_rate=100.00%" in text and "complete=10" in text
    assert text.splitlines()[-1] == LIMITATION


def test_loss_partial_not_decoded_and_recv_only():
    s = [sent(i, i * 3000) for i in range(10)]
    r = [recv(i, i * 3000) for i in range(8)]  # 8, 9 손실
    r[3]["packets_recv"] = 1  # 일부 패킷 손실 후 디코드
    r.append(recv(8, 8 * 3000, decoded=False))  # 도착했지만 디코드 안 됨
    r.append(recv(20, 999_999))  # 송신 기록 없음
    out = summarize(s, r)
    assert out["frames_matched"] == 8
    assert out["match_rate"] == 0.8
    assert (out["frames_complete"], out["frames_incomplete"], out["frames_lost"]) == (7, 1, 2)
    assert out["incomplete_rate"] == 0.1
    # 송신 20, 수신 = 디코드 8프레임(15) + 미디코드 프레임 8(2), 송신 기록 없는 프레임 제외
    assert (out["packets_sent"], out["packets_recv"]) == (20, 17)
    assert out["packet_loss_rate"] == 0.15
    assert out["recv_not_decoded"] == 1
    assert out["recv_only"] == 1


def test_rtp_ts_wraparound_joins_and_stays_monotonic():
    start = (1 << 32) - 3 * 3000
    ts = [(start + i * 3000) % (1 << 32) for i in range(6)]
    s = [sent(i, t) for i, t in enumerate(ts)]
    r = [recv(i, t) for i, t in enumerate(ts)]
    out = summarize(s, r)
    assert out["match_rate"] == 1.0
    assert out["rtp_ts_monotonic"] is True


def test_same_rtp_ts_different_ssrc_not_confused():
    s = [sent(0, 3000, ssrc=1), sent(1, 3000, ssrc=2)]
    r = [recv(0, 3000, ssrc=2)]
    out = summarize(s, r)
    assert out["frames_matched"] == 1
    assert out["ssrc_count"] == 2


def test_classify_three_categories_and_unclassified():
    assert classify(3, None) == "lost"
    assert classify(3, {"packets_recv": 3}) == "complete"
    assert classify(3, {"packets_recv": 2}) == "incomplete"
    assert classify(3, {"packets_recv": None}) == "unknown"
    assert classify(3, {"packets_recv": 4}) == "over"


def test_legacy_recv_packets_field_is_read():
    s = [sent(0, 3000, packets=3)]
    r = [recv(0, 3000)]
    del r[0]["packets_recv"]
    r[0]["packets"] = 2  # D03 이전 recv.jsonl 형식
    out = summarize(s, r)
    assert out["frames_incomplete"] == 1


def test_categories_sum_to_sent():
    s = [sent(i, i * 3000, packets=4) for i in range(6)]
    r = [recv(0, 0, packets=4), recv(1, 3000, packets=3), recv(2, 6000, packets=5)]
    r.append({**recv(3, 9000), "packets_recv": None})
    out = summarize(s, r)
    parts = ("frames_complete", "frames_incomplete", "frames_lost")
    extra = ("decoded_packets_unknown", "decoded_packets_over")
    assert [out[k] for k in parts + extra] == [1, 1, 2, 1, 1]
    assert sum(out[k] for k in parts + extra) == out["frames_sent"]
    assert out["match_rate"] == round(4 / 6, 4)
    assert "unclassified unknown=1 over=1" in format_summary({"run_id": "x", **out})


def test_fps_and_decoder_outputs_from_recv_meta():
    s = [sent(i, i * 3000) for i in range(31)]  # capture 33ms 간격
    r = [recv(i, i * 3000) for i in range(29)]
    meta = {"frames_decoded": 29, "decoded_unmapped": 2}
    out = summarize(s, r, meta)
    assert out["send_fps"] == round(30 / 0.99, 2)
    assert out["recv_fps"] == round(28 / (28 * 0.033), 2)
    assert (out["decoder_outputs"], out["decoded_unmapped"]) == (31, 2)
    assert out["frames_lost"] == 2  # 매핑 실패는 여전히 lost로 센다(정의 유지)
    text = format_summary({"run_id": "x", **out})
    assert "decoder_outputs=31 (unmapped 2)" in text
    assert "주의: 디코더 출력 2개가 PTS 매핑에 실패" in text


def test_decoder_outputs_none_without_meta_and_fps_edge_cases():
    out = summarize([sent(0, 0)], [recv(0, 0)])
    assert out["decoder_outputs"] is None and out["decoded_unmapped"] is None
    assert out["send_fps"] is None and out["recv_fps"] is None
    assert "주의" not in format_summary({"run_id": "x", **out})
    # D03 이전 recv_meta(decoded_unmapped 키 없음)는 0으로 본다
    assert summarize([sent(0, 0)], [recv(0, 0)], {"frames_decoded": 1})["decoded_unmapped"] == 0
