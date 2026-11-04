import struct

import pytest

from pipe.capture.rtp import FrameAccumulator, parse_rtp_header, seq_before, unwrap


def pkt(seq: int, ts: int, marker: bool = False, ssrc: int = 0x1234, pt: int = 96) -> bytes:
    return struct.pack("!BBHII", 0x80, (0x80 if marker else 0) | pt, seq, ts, ssrc) + b"\x00" * 4


def test_parse_header_fields():
    h = parse_rtp_header(pkt(65535, 0xFFFFFFFF, marker=True, ssrc=7))
    assert (h.version, h.marker, h.payload_type, h.seq, h.ts, h.ssrc) == (
        2,
        True,
        96,
        65535,
        0xFFFFFFFF,
        7,
    )


def test_parse_rejects_short_and_bad_version():
    with pytest.raises(ValueError):
        parse_rtp_header(b"\x80" * 5)
    with pytest.raises(ValueError):
        parse_rtp_header(b"\x40" + pkt(1, 1)[1:])


def test_unwrap_ts_and_seq():
    assert unwrap([0xFFFFFF00, 0xFFFFFFF0, 0x10, 0x100]) == [
        0xFFFFFF00,
        0xFFFFFFF0,
        (1 << 32) + 0x10,
        (1 << 32) + 0x100,
    ]
    assert unwrap([65534, 65535, 0, 1], modulus=1 << 16) == [65534, 65535, 65536, 65537]


def test_accumulator_marker_closes_frame():
    acc = FrameAccumulator()
    assert acc.add(parse_rtp_header(pkt(1, 100)), 1000, 10) == []
    done = acc.add(parse_rtp_header(pkt(2, 100, marker=True)), 500, 20)
    assert len(done) == 1
    f = done[0]
    assert (f.rtp_ts, f.seq_first, f.seq_last, f.packets, f.bytes) == (100, 1, 2, 2, 1500)
    assert (f.first_mono_ns, f.last_mono_ns, f.marker_seen) == (10, 20, True)


def test_accumulator_marker_lost_closed_by_next_frame():
    acc = FrameAccumulator()
    acc.add(parse_rtp_header(pkt(1, 100)), 10, 1)
    done = acc.add(parse_rtp_header(pkt(3, 3100)), 10, 2)  # seq 2(marker) 손실
    assert [(f.rtp_ts, f.marker_seen) for f in done] == [(100, False)]
    assert [f.rtp_ts for f in acc.flush()] == [3100]


def test_accumulator_separates_ssrc():
    acc = FrameAccumulator()
    acc.add(parse_rtp_header(pkt(1, 100, ssrc=1)), 10, 1)
    assert acc.add(parse_rtp_header(pkt(1, 100, ssrc=2)), 10, 2) == []
    assert len(acc.flush()) == 2


def test_seq_before_wraps():
    assert seq_before(1, 2) and not seq_before(2, 1) and not seq_before(5, 5)
    assert seq_before(65535, 0) and not seq_before(0, 65535)


def test_accumulator_reordered_packets_keep_seq_range():
    acc = FrameAccumulator()
    acc.add(parse_rtp_header(pkt(65535, 100)), 10, 1)
    acc.add(parse_rtp_header(pkt(1, 100, marker=False)), 10, 2)
    (f,) = acc.add(parse_rtp_header(pkt(0, 100, marker=True)), 10, 3)
    assert (f.seq_first, f.seq_last, f.packets) == (65535, 1, 3)


def test_late_packet_reopens_frame_and_merges():
    acc = FrameAccumulator()
    acc.add(parse_rtp_header(pkt(10, 100)), 10, 1)
    (first,) = acc.add(parse_rtp_header(pkt(12, 3100)), 10, 2)  # 다음 프레임이 먼저 옴
    # 늦은 패킷: 열려 있던 3100을 닫고 100을 다시 돌려준다(둘 다 호출 측이 키로 merge)
    done = acc.add(parse_rtp_header(pkt(11, 100, marker=True)), 10, 3)
    assert [f.rtp_ts for f in done] == [3100, 100]
    late = done[1]
    first.merge(late)
    assert (first.packets, first.seq_first, first.seq_last) == (2, 10, 11)
    assert (first.first_mono_ns, first.last_mono_ns, first.marker_seen) == (1, 3, True)
