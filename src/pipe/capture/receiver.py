"""프레임 수신: RTP/UDP → jitterbuffer → depay → avdec_h264(디코드).

기록(recv.jsonl, 프레임당 1줄):
  first_pkt_mono_ns/last_pkt_mono_ns  udpsrc 출력(jitterbuffer 이전) 기준 패킷 도착 시각
  depay_mono_ns                       depayloader가 access unit을 내보낸 시각
  recv_mono_ns/recv_ts_ns             디코드 완료(avdec_h264 src 패드) 시각 = 추론 입력 가능 시점
  decoded=false                       패킷은 왔지만 디코드 출력이 없던 프레임(종료 시 기록)

rtp_ts → 디코드 프레임 매핑: depay는 marker 패킷(또는 marker 손실 시 다음 프레임 첫 패킷)을
받은 직후 동기적으로 AU를 내보내므로, depay src에서 직전 입력의 rtp_ts를 출력 PTS에 묶는다.
zerolatency(B프레임 없음)라 디코드 순서와 출력 순서가 같고 avdec는 PTS를 보존한다.

실행: uv run python -m pipe.capture.receiver --run-id <id> --duration 75
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from typing import Any

from pipe.capture import config as rec
from pipe.capture.config import CaptureConfig
from pipe.capture.pipelines import receiver_pipeline
from pipe.capture.rtp import FrameAccumulator, FrameAgg
from pipe.capture.sender import (
    GLib,
    Gst,
    bus_messages_to,
    caps_size,
    init,
    iter_buffers,
    rtp_header_of,
)

DEFINITIONS = {
    "first_pkt_ts/last_pkt_ts": "udpsrc src 패드(jitterbuffer 이전) 패킷 도착 시각",
    "depay_ts": "rtph264depay가 access unit을 내보낸 시각(jitterbuffer 대기 포함)",
    "recv_ts": "avdec_h264 src 패드 디코드 완료 시각(추론 입력 가능 시점)",
    "clock": "*_mono_ns=CLOCK_MONOTONIC, *_ts_ns=epoch",
}


class Receiver:
    def __init__(self, cfg: CaptureConfig, run_id: str) -> None:
        init()
        self.cfg = cfg
        self.run_id = run_id
        self.run_dir = cfg.run_dir(run_id)
        self.desc = receiver_pipeline(cfg)
        self.pipeline = Gst.parse_launch(self.desc)
        self.writer = rec.JsonlWriter(self.run_dir / rec.RECV_FILE)
        self.lock = threading.Lock()
        self.acc = FrameAccumulator()
        self.arrivals: dict[tuple[int, int], FrameAgg] = {}
        self.depay_in_cur: tuple[int, int] | None = None
        self.depay_in_prev: tuple[int, int] | None = None
        self.depay_in_marker = False
        self.pts_key: dict[int, tuple[tuple[int, int], int]] = {}  # pts → (key, depay_mono)
        self.cur_size: tuple[int, int] | None = None
        self.last_pkt_mono: int | None = None
        self.packets = 0
        self.decoded = 0
        self.meta: dict[str, Any] = {
            "run_id": run_id,
            "role": "receiver",
            "gst_version": Gst.version_string(),
            "pipeline": self.desc,
            "config": cfg.to_dict(),
            "definitions": DEFINITIONS,
            "caps_changes": [],
            "errors": [],
            "warnings": [],
        }
        self._install_probes()

    def _pad(self, name: str, pad: str) -> Gst.Pad:
        return self.pipeline.get_by_name(name).get_static_pad(pad)

    def _install_probes(self) -> None:
        both = Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST
        self._pad("src", "src").add_probe(both, self._on_udp)
        self._pad("depay", "sink").add_probe(both, self._on_depay_in)
        self._pad("depay", "src").add_probe(Gst.PadProbeType.BUFFER, self._on_depay_out)
        self._pad("dec", "src").add_probe(Gst.PadProbeType.BUFFER, self._on_decoded)
        self._pad("dec", "src").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_dec_event)

    def _on_udp(self, _pad, info) -> Gst.PadProbeReturn:
        for buf in iter_buffers(info):
            mono, _ = rec.now_ns()
            try:
                hdr = rtp_header_of(buf)
            except ValueError:
                continue
            with self.lock:
                self.packets += 1
                self.last_pkt_mono = mono
                for agg in self.acc.add(hdr, buf.get_size(), mono):
                    self.arrivals[(agg.ssrc, agg.rtp_ts)] = agg
        return Gst.PadProbeReturn.OK

    def _on_depay_in(self, _pad, info) -> Gst.PadProbeReturn:
        for buf in iter_buffers(info):
            hdr = rtp_header_of(buf)
            key = (hdr.ssrc, hdr.ts)
            with self.lock:
                if key != self.depay_in_cur:
                    self.depay_in_prev, self.depay_in_cur = self.depay_in_cur, key
                self.depay_in_marker = hdr.marker
        return Gst.PadProbeReturn.OK

    def _on_depay_out(self, _pad, info) -> Gst.PadProbeReturn:
        mono, _ = rec.now_ns()
        buf = info.get_buffer()
        with self.lock:
            key = self.depay_in_cur if self.depay_in_marker else self.depay_in_prev
            if key is not None:
                self.pts_key[buf.pts] = (key, mono)
        return Gst.PadProbeReturn.OK

    def _on_dec_event(self, _pad, info) -> Gst.PadProbeReturn:
        ev = info.get_event()
        if ev.type == Gst.EventType.CAPS:
            w, h, fps = caps_size(ev.parse_caps())
            mono, _ = rec.now_ns()
            with self.lock:
                self.cur_size = (w, h)
                self.meta["caps_changes"].append(
                    {"mono_ns": mono, "width": w, "height": h, "fps": fps}
                )
        return Gst.PadProbeReturn.OK

    def _on_decoded(self, _pad, info) -> Gst.PadProbeReturn:
        mono, epoch = rec.now_ns()
        buf = info.get_buffer()
        with self.lock:
            item = self.pts_key.pop(buf.pts, None)
            if item is None:
                self.meta.setdefault("decoded_unmapped", 0)
                self.meta["decoded_unmapped"] += 1
                return Gst.PadProbeReturn.OK
            key, depay_mono = item
            agg = self.arrivals.pop(key, None)
            self.decoded += 1
            self.writer.write(self._record(key, agg, depay_mono, mono, epoch, decoded=True))
        return Gst.PadProbeReturn.OK

    def _record(self, key, agg, depay_mono, mono, epoch, decoded: bool) -> dict:
        w, h = self.cur_size if (decoded and self.cur_size) else (None, None)
        return {
            "run_id": self.run_id,
            "ssrc": key[0],
            "rtp_ts": key[1],
            "packets": agg.packets if agg else None,
            "bytes": agg.bytes if agg else None,
            "rtp_seq_first": agg.seq_first if agg else None,
            "rtp_seq_last": agg.seq_last if agg else None,
            "marker_seen": agg.marker_seen if agg else None,
            "first_pkt_mono_ns": agg.first_mono_ns if agg else None,
            "last_pkt_mono_ns": agg.last_mono_ns if agg else None,
            "depay_mono_ns": depay_mono,
            "recv_mono_ns": mono,
            "recv_ts_ns": epoch,
            "decoded": decoded,
            "width": w,
            "height": h,
        }

    def run(self, duration_s: float, idle_timeout_s: float) -> int:
        loop = GLib.MainLoop()
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", bus_messages_to(self.meta, loop))

        def stop(*_a) -> bool:
            loop.quit()
            return False

        def idle_check() -> bool:
            mono, _ = rec.now_ns()
            with self.lock:
                last = self.last_pkt_mono
            if last is not None and (mono - last) / 1e9 > idle_timeout_s:
                print(f"[receiver] idle {idle_timeout_s}s → stop", flush=True)
                loop.quit()
                return False
            return True

        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, stop)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, stop)
        GLib.timeout_add(int(duration_s * 1000), stop)
        GLib.timeout_add(250, idle_check)

        self.meta["start_mono_ns"], self.meta["start_ts_ns"] = rec.now_ns()
        self.pipeline.set_state(Gst.State.PLAYING)
        self.pipeline.get_state(Gst.CLOCK_TIME_NONE)
        print(f"[receiver] READY run={self.run_id} port={self.cfg.rtp_port}", flush=True)
        loop.run()
        self.meta["end_mono_ns"], self.meta["end_ts_ns"] = rec.now_ns()
        self.pipeline.set_state(Gst.State.NULL)
        with self.lock:
            for agg in self.acc.flush():
                self.arrivals[(agg.ssrc, agg.rtp_ts)] = agg
            for key, agg in self.arrivals.items():
                self.writer.write(self._record(key, agg, None, None, None, decoded=False))
            self.meta["packets"] = self.packets
            self.meta["frames_decoded"] = self.decoded
            self.meta["frames_not_decoded"] = len(self.arrivals)
        self.writer.close()
        rec.write_json(self.run_dir / rec.RECEIVER_META, self.meta)
        print(
            f"[receiver] packets={self.packets} decoded={self.decoded}"
            f" not_decoded={self.meta['frames_not_decoded']} errors={len(self.meta['errors'])}",
            flush=True,
        )
        return 1 if self.meta["errors"] else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="RTP/UDP 프레임 수신·디코드")
    p.add_argument("--run-id", required=True)
    p.add_argument("--duration", type=float, default=90.0, help="최대 실행 초")
    p.add_argument("--idle-timeout", type=float, default=3.0, help="첫 패킷 이후 무수신 종료 초")
    args = p.parse_args(argv)
    return Receiver(CaptureConfig.from_env(), args.run_id).run(args.duration, args.idle_timeout)


if __name__ == "__main__":
    sys.exit(main())
