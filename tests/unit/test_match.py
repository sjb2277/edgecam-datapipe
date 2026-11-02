import pytest

from pipe.capture.match import format_summary, percentile, summarize

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
        "packets": packets,
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
    assert "match_rate=100.00%" in format_summary({"run_id": "x", **out})


def test_loss_partial_not_decoded_and_recv_only():
    s = [sent(i, i * 3000) for i in range(10)]
    r = [recv(i, i * 3000) for i in range(8)]  # 8, 9 손실
    r[3]["packets"] = 1  # 일부 패킷 손실 후 디코드
    r.append(recv(8, 8 * 3000, decoded=False))  # 도착했지만 디코드 안 됨
    r.append(recv(20, 999_999))  # 송신 기록 없음
    out = summarize(s, r)
    assert out["frames_matched"] == 8
    assert out["match_rate"] == 0.8
    assert out["lost"] == 2
    assert out["partial_frames"] == 1
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
