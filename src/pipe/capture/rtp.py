"""RTP 헤더 파싱과 패킷→프레임 집계(순수 Python, 단위 테스트 대상)."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

RTP_TS_MOD = 1 << 32
RTP_SEQ_MOD = 1 << 16


def seq_before(a: int, b: int) -> bool:
    """16비트 RTP seq에서 a가 b보다 앞서는가(wrap 고려, RFC 3550 방식)."""
    d = (b - a) % RTP_SEQ_MOD
    return 0 < d < RTP_SEQ_MOD // 2


@dataclass(frozen=True)
class RtpHeader:
    version: int
    marker: bool
    payload_type: int
    seq: int
    ts: int
    ssrc: int


def parse_rtp_header(data: bytes) -> RtpHeader:
    if len(data) < 12:
        raise ValueError(f"RTP packet too short: {len(data)} bytes")
    b0, b1, seq, ts, ssrc = struct.unpack("!BBHII", data[:12])
    version = b0 >> 6
    if version != 2:
        raise ValueError(f"unsupported RTP version: {version}")
    return RtpHeader(
        version=version,
        marker=bool(b1 & 0x80),
        payload_type=b1 & 0x7F,
        seq=seq,
        ts=ts,
        ssrc=ssrc,
    )


def unwrap(values: list[int], modulus: int = RTP_TS_MOD) -> list[int]:
    """wrap된 카운터(RTP ts 32비트, seq 16비트)를 단조 증가 정수로 편다(정렬용)."""
    out: list[int] = []
    offset = 0
    prev: int | None = None
    half = modulus // 2
    for v in values:
        if prev is not None:
            diff = v - prev
            if diff < -half:
                offset += modulus
            elif diff > half:
                offset -= modulus
        out.append(v + offset)
        prev = v
    return out


@dataclass
class FrameAgg:
    ssrc: int
    rtp_ts: int
    seq_first: int
    seq_last: int
    packets: int
    bytes: int
    first_mono_ns: int
    last_mono_ns: int
    marker_seen: bool = False

    def merge(self, other: FrameAgg) -> None:
        """재정렬로 같은 프레임이 두 번 닫힌 경우(늦게 온 패킷) 하나로 합친다."""
        self.packets += other.packets
        self.bytes += other.bytes
        if seq_before(other.seq_first, self.seq_first):
            self.seq_first = other.seq_first
        if seq_before(self.seq_last, other.seq_last):
            self.seq_last = other.seq_last
        self.first_mono_ns = min(self.first_mono_ns, other.first_mono_ns)
        self.last_mono_ns = max(self.last_mono_ns, other.last_mono_ns)
        self.marker_seen = self.marker_seen or other.marker_seen


@dataclass
class FrameAccumulator:
    """같은 (ssrc, rtp_ts)의 패킷을 한 프레임으로 묶는다.

    marker 비트를 받으면 그 프레임을 완료로 돌려준다. marker가 손실되어도
    다음 프레임의 첫 패킷이 오면 이전 프레임을 완료로 돌려준다(marker_seen=False).
    seq_first/seq_last는 받은 패킷의 seq 최소·최대(wrap 고려)다.
    재정렬로 닫힌 프레임의 패킷이 늦게 오면 같은 키로 다시 돌려주므로 호출 측이 merge한다.
    """

    open: dict[tuple[int, int], FrameAgg] = field(default_factory=dict)

    def add(self, hdr: RtpHeader, size: int, mono_ns: int) -> list[FrameAgg]:
        done: list[FrameAgg] = []
        key = (hdr.ssrc, hdr.ts)
        if key not in self.open:
            # 새 프레임 시작: 같은 ssrc의 미완료 프레임은 marker 손실로 보고 닫는다.
            for k in [k for k in self.open if k[0] == hdr.ssrc]:
                done.append(self.open.pop(k))
            self.open[key] = FrameAgg(
                ssrc=hdr.ssrc,
                rtp_ts=hdr.ts,
                seq_first=hdr.seq,
                seq_last=hdr.seq,
                packets=0,
                bytes=0,
                first_mono_ns=mono_ns,
                last_mono_ns=mono_ns,
            )
        agg = self.open[key]
        agg.packets += 1
        agg.bytes += size
        # 도착 순서가 아니라 seq 범위(최소~최대)를 기록한다(재정렬 대비)
        if seq_before(hdr.seq, agg.seq_first):
            agg.seq_first = hdr.seq
        if seq_before(agg.seq_last, hdr.seq):
            agg.seq_last = hdr.seq
        agg.last_mono_ns = mono_ns
        if hdr.marker:
            agg.marker_seen = True
            done.append(self.open.pop(key))
        return done

    def flush(self) -> list[FrameAgg]:
        done = list(self.open.values())
        self.open.clear()
        return done
