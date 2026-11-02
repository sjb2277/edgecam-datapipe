"""프레임 송신: 웹캠/클립 → H.264(x264enc zerolatency) → RTP/UDP.

기록(sent.jsonl, 프레임당 1줄):
  capture_mono_ns/capture_ts_ns  인코더 입력 직전(pace src 패드).
                                 웹캠 모드에서도 센서 시각이 아니다.
  src_mono_ns                    웹캠 모드만: v4l2 버퍼 타임스탬프(base_time+PTS, MONOTONIC)
  send_mono_ns                   해당 프레임의 마지막 RTP 패킷이 payloader를 나간 시각
  ssrc, rtp_ts, rtp_seq_first/last, packets, bytes(RTP 패킷 크기 합)

이 모듈의 Gst 헬퍼(init, iter_buffers, rtp_header_of, caps_size)는 receiver도 쓴다.

실행: uv run python -m pipe.capture.sender --mode clip --run-id <id> --duration 60
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from typing import Any

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from pipe.capture import config as rec  # noqa: E402
from pipe.capture.config import CaptureConfig  # noqa: E402
from pipe.capture.knobs import KnobEvent, parse_schedule  # noqa: E402
from pipe.capture.pipelines import Mode, raw_caps, sender_pipeline  # noqa: E402
from pipe.capture.rtp import FrameAccumulator, RtpHeader, parse_rtp_header  # noqa: E402

DEFINITIONS = {
    "capture_ts": "인코더 입력 직전(identity pace src 패드) 시각. "
    "웹캠 모드에서도 센서 시각이 아니다.",
    "src_ts": "웹캠 모드만. v4l2src do-timestamp 버퍼 PTS + base_time(CLOCK_MONOTONIC). "
    "드라이버가 버퍼를 넘긴 시각 기준이며 capture_ts와 별도로 유지한다.",
    "send_ts": "프레임의 마지막 RTP 패킷이 rtph264pay를 나간 시각(udpsink sync=false 직전).",
    "bytes": "RTP 패킷 크기 합(RTP 헤더 포함, UDP/IP 헤더 제외).",
    "clock": "*_mono_ns=CLOCK_MONOTONIC, *_ts_ns=epoch",
}


# ---- 공유 Gst 헬퍼 ----
def init() -> None:
    if not Gst.is_initialized():
        Gst.init(None)


def iter_buffers(info: Gst.PadProbeInfo):
    """BUFFER / BUFFER_LIST probe 모두에서 버퍼를 순회한다."""
    if info.type & Gst.PadProbeType.BUFFER_LIST:
        lst = info.get_buffer_list()
        for i in range(lst.length()):
            yield lst.get(i)
    else:
        buf = info.get_buffer()
        if buf is not None:
            yield buf


def rtp_header_of(buf: Gst.Buffer) -> RtpHeader:
    return parse_rtp_header(buf.extract_dup(0, 12))


def caps_size(caps: Gst.Caps) -> tuple[int, int, int | None]:
    st = caps.get_structure(0)
    _, w = st.get_int("width")
    _, h = st.get_int("height")
    ok, num, den = st.get_fraction("framerate")
    fps = round(num / den) if ok and den else None
    return w, h, fps


def bus_messages_to(meta: dict[str, Any], loop: GLib.MainLoop, on_eos=None):
    def on_message(_bus: Gst.Bus, msg: Gst.Message) -> bool:
        t = msg.type
        if t == Gst.MessageType.EOS:
            if on_eos:
                on_eos()
            loop.quit()
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            meta["errors"].append({"src": msg.src.get_name(), "error": err.message, "debug": dbg})
            loop.quit()
        elif t == Gst.MessageType.WARNING:
            w, dbg = msg.parse_warning()
            if len(meta["warnings"]) < 100:
                meta["warnings"].append({"src": msg.src.get_name(), "warning": w.message})
        return True

    return on_message


# ---- 송신기 ----
class Sender:
    def __init__(
        self, cfg: CaptureConfig, mode: Mode, run_id: str, test_pattern: str = "ball"
    ) -> None:
        init()
        self.cfg = cfg
        self.mode = mode
        self.run_id = run_id
        self.run_dir = cfg.run_dir(run_id)
        self.desc = sender_pipeline(cfg, mode, test_pattern)
        self.pipeline = Gst.parse_launch(self.desc)
        self.enc = self.pipeline.get_by_name("enc")
        self.rawcaps = self.pipeline.get_by_name("rawcaps")
        self.writer = rec.JsonlWriter(self.run_dir / rec.SENT_FILE)
        self.lock = threading.Lock()
        self.frame_idx = 0
        self.pending: dict[int, dict] = {}  # pts → capture 기록
        self.ts_pts: dict[tuple[int, int], int] = {}  # (ssrc, rtp_ts) → pts
        self.acc = FrameAccumulator()
        self.last_src_mono: int | None = None
        self.cur = {"width": cfg.width, "height": cfg.height, "fps": cfg.fps}
        self.bitrate = cfg.bitrate_kbps
        self.meta: dict[str, Any] = {
            "run_id": run_id,
            "role": "sender",
            "mode": mode,
            "gst_version": Gst.version_string(),
            "pipeline": self.desc,
            "config": cfg.to_dict(),
            "definitions": DEFINITIONS,
            "knob_events": [],
            "errors": [],
            "warnings": [],
            "pts_offset_ns": None,
            "pts_offset_resyncs": 0,
            "unmatched_rtp_frames": 0,
            "encoder_dropped": 0,
        }
        self.pts_offset = 0
        self._install_probes()

    def _install_probes(self) -> None:
        if self.mode == "webcam":
            src_pad = self.pipeline.get_by_name("src").get_static_pad("src")
            src_pad.add_probe(Gst.PadProbeType.BUFFER, self._on_src)
        pace_pad = self.pipeline.get_by_name("pace").get_static_pad("src")
        pace_pad.add_probe(Gst.PadProbeType.BUFFER, self._on_pace)
        pace_pad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._on_pace_event)
        pay_pad = self.pipeline.get_by_name("pay").get_static_pad("src")
        pay_pad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST, self._on_pay)

    def _on_src(self, pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        buf = info.get_buffer()
        if buf is not None and buf.pts != Gst.CLOCK_TIME_NONE:
            self.last_src_mono = pad.get_parent_element().get_base_time() + buf.pts
        return Gst.PadProbeReturn.OK

    def _on_pace_event(self, _pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        ev = info.get_event()
        if ev.type == Gst.EventType.CAPS:
            w, h, fps = caps_size(ev.parse_caps())
            with self.lock:
                self.cur = {"width": w, "height": h, "fps": fps}
        return Gst.PadProbeReturn.OK

    def _on_pace(self, _pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        mono, epoch = rec.now_ns()
        buf = info.get_buffer()
        with self.lock:
            self.pending[buf.pts] = {
                "frame_idx": self.frame_idx,
                "pts_ns": buf.pts,
                "capture_mono_ns": mono,
                "capture_ts_ns": epoch,
                "src_mono_ns": self.last_src_mono if self.mode == "webcam" else None,
                "bitrate_kbps": self.bitrate,
                **self.cur,
            }
            self.frame_idx += 1
        return Gst.PadProbeReturn.OK

    def _on_pay(self, _pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        for buf in iter_buffers(info):
            mono, _ = rec.now_ns()
            hdr = rtp_header_of(buf)
            with self.lock:
                self.ts_pts.setdefault((hdr.ssrc, hdr.ts), buf.pts)
                done = self.acc.add(hdr, buf.get_size(), mono)
                for agg in done:
                    self._emit(agg)
        return Gst.PadProbeReturn.OK

    def _emit(self, agg) -> None:
        """RTP 프레임을 capture 기록과 PTS로 조인한다.

        x264enc(GstVideoEncoder)는 출력 PTS에 고정 오프셋(1.20에서 1000시간)을 더하므로
        첫 프레임에서 오프셋을 학습한다. 조인된 PTS보다 앞선 미조인 capture는 인코더가
        버린 프레임으로 집계한다.
        """
        pts = self.ts_pts.pop((agg.ssrc, agg.rtp_ts), None)
        if pts is None or pts == Gst.CLOCK_TIME_NONE:
            self.meta["unmatched_rtp_frames"] += 1
            return
        cap = self.pending.pop(pts - self.pts_offset, None)
        if cap is None and self.pending:
            self.pts_offset = pts - min(self.pending)
            self.meta["pts_offset_ns"] = self.pts_offset
            self.meta["pts_offset_resyncs"] += 1
            cap = self.pending.pop(pts - self.pts_offset, None)
        if cap is None:
            self.meta["unmatched_rtp_frames"] += 1
            return
        for old in [p for p in self.pending if p < cap["pts_ns"]]:
            del self.pending[old]
            self.meta["encoder_dropped"] += 1
        self.writer.write(
            {
                "run_id": self.run_id,
                **cap,
                "ssrc": agg.ssrc,
                "rtp_ts": agg.rtp_ts,
                "rtp_seq_first": agg.seq_first,
                "rtp_seq_last": agg.seq_last,
                "packets": agg.packets,
                "bytes": agg.bytes,
                "marker_seen": agg.marker_seen,
                "send_mono_ns": agg.last_mono_ns,
            }
        )

    # ---- 노브 ----
    def apply_knob(self, ev: KnobEvent) -> bool:
        mono, epoch = rec.now_ns()
        if ev.kind == "bitrate":
            self.enc.set_property("bitrate", int(ev.value))
            with self.lock:
                self.bitrate = int(ev.value)
            value: Any = int(ev.value)
        else:
            w, h, fps = ev.value
            self.rawcaps.set_property("caps", Gst.Caps.from_string(raw_caps(w, h, fps)))
            value = [w, h, fps]
        self.meta["knob_events"].append(
            {"t_s": ev.t_s, "kind": ev.kind, "value": value, "mono_ns": mono, "ts_ns": epoch}
        )
        print(f"[sender] knob t={ev.t_s}s {ev.kind}={value}", flush=True)
        return False  # GLib 일회성 타이머

    def run(self, duration_s: float, schedule: list[KnobEvent]) -> int:
        loop = GLib.MainLoop()
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", bus_messages_to(self.meta, loop))

        def stop(*_a) -> bool:
            self.pipeline.send_event(Gst.Event.new_eos())
            return False

        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, stop)
        GLib.timeout_add(int(duration_s * 1000), stop)
        for ev in schedule:
            GLib.timeout_add(int(ev.t_s * 1000), self.apply_knob, ev)

        self.meta["start_mono_ns"], self.meta["start_ts_ns"] = rec.now_ns()
        self.pipeline.set_state(Gst.State.PLAYING)
        print(f"[sender] run={self.run_id} mode={self.mode} → {self.run_dir}", flush=True)
        loop.run()
        self.meta["end_mono_ns"], self.meta["end_ts_ns"] = rec.now_ns()
        self.pipeline.set_state(Gst.State.NULL)
        with self.lock:
            for agg in self.acc.flush():
                self._emit(agg)
            self.meta["frames_captured"] = self.frame_idx
            self.meta["frames_sent"] = self.writer.count
            self.meta["frames_pending_at_end"] = len(self.pending)
        self.writer.close()
        rec.write_json(self.run_dir / rec.SENDER_META, self.meta)
        print(
            f"[sender] captured={self.frame_idx} sent={self.meta['frames_sent']}"
            f" errors={len(self.meta['errors'])}",
            flush=True,
        )
        return 1 if self.meta["errors"] else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="RTP/UDP 프레임 송신")
    p.add_argument("--mode", choices=["webcam", "clip", "test"], required=True)
    p.add_argument("--run-id", default=None)
    p.add_argument("--duration", type=float, default=60.0, help="초")
    p.add_argument("--schedule", default="", help="예: 20:bitrate=500,40:caps=320x240@15")
    p.add_argument("--bitrate", type=int, default=None, help="초기 비트레이트 kbps(덮어쓰기)")
    p.add_argument("--test-pattern", default="ball", help="test 모드 videotestsrc 패턴")
    args = p.parse_args(argv)

    cfg = CaptureConfig.from_env()
    if args.bitrate is not None:
        from dataclasses import replace

        cfg = replace(cfg, bitrate_kbps=args.bitrate)
    if args.mode == "clip" and not cfg.clip_path.exists():
        print(f"[sender] clip not found: {cfg.clip_path}", file=sys.stderr)
        return 2
    run_id = args.run_id or rec.default_run_id()
    return Sender(cfg, args.mode, run_id, args.test_pattern).run(
        args.duration, parse_schedule(args.schedule)
    )


if __name__ == "__main__":
    sys.exit(main())
