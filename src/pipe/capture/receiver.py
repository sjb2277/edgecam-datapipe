"""프레임 수신: RTP/UDP → jitterbuffer → depay → avdec_h264(디코드).

기록(recv.jsonl, 프레임당 1줄):
  first_pkt_mono_ns/last_pkt_mono_ns  udpsrc 출력(jitterbuffer 이전) 기준 패킷 도착 시각
  depay_mono_ns                       depayloader가 access unit을 내보낸 시각
  recv_mono_ns/recv_ts_ns             디코드 완료(avdec_h264 src 패드) 시각 = 추론 입력 가능 시점
  decoded=false                       패킷은 왔지만 디코드 출력이 없던 프레임(종료 시 기록)
  packets_recv/rtp_seq_first/rtp_seq_last/marker_seen
      udpsrc 출력 기준 프레임별 수신 패킷 수, seq 최소·최대, marker 수신 여부.
      디코드 시점까지 도착한 패킷 기준이고, 디코드 후 늦게 온 패킷은
      recv_meta.json의 late_packets_after_decode로만 센다.

  pts_ns
      depay 출력 PTS(진단용). 미디코드 프레임도 depay를 거쳤으면 기록한다
      (이 경우 depay_mono_ns도 채워진다).

recv_meta.json:
  gst_debug       수신기 전용 GStreamer 디버그 로그 설정·결과(--gst-debug 또는 RECV_GST_DEBUG).
                  로그는 <run>/gst_debug.log. 크기 상한(기본 20MB)을 넘으면 쓰기를 멈추고
                  truncated=true
  pts_diag        PTS 매핑 진단: h264parse 출력·디코더 출력 중 매핑 표에 없는 PTS의 수와
                  처음 diag_max개 샘플, 종료 시 매핑 표에 남은 PTS
  udp_rcvbuf      요청값(buffer-size), 실제 SO_RCVBUF(커널 보고값=2배), rmem_max/rmem_default
  udp_snmp_*      /proc/net/snmp Udp 카운터 시작·종료·증감(netns 단위, 이 소켓만의 값이 아님)

rtp_ts → 디코드 프레임 매핑: depay는 marker 패킷(또는 marker 손실 시 다음 프레임 첫 패킷)을
받은 직후 동기적으로 AU를 내보내므로, depay src에서 직전 입력의 rtp_ts를 출력 PTS에 묶는다.
zerolatency(B프레임 없음)라 디코드 순서와 출력 순서가 같고 avdec는 PTS를 보존한다.

실행: uv run python -m pipe.capture.receiver --run-id <id> --duration 75
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import threading
from typing import Any

import gi

from pipe.capture import config as rec
from pipe.capture.config import CaptureConfig
from pipe.capture.pipelines import receiver_pipeline
from pipe.capture.recv_diag import (
    GST_DEBUG_FILE,
    GST_DEBUG_MAX_BYTES,
    CappedLog,
    _pts,
    _read_int,
    format_gst_line,
    gst_debug_settings,
    leftover_summary,
    read_udp_snmp,
    snmp_delta,
    unmapped_sample,
)
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

gi.require_version("Gio", "2.0")  # udpsrc used-socket(GSocket) 메서드용
from gi.repository import Gio  # noqa: E402, F401

DEFINITIONS = {
    "first_pkt_ts/last_pkt_ts": "udpsrc src 패드(jitterbuffer 이전) 패킷 도착 시각",
    "depay_ts": "rtph264depay가 access unit을 내보낸 시각(jitterbuffer 대기 포함)",
    "recv_ts": "avdec_h264 src 패드 디코드 완료 시각(추론 입력 가능 시점)",
    "clock": "*_mono_ns=CLOCK_MONOTONIC, *_ts_ns=epoch",
    "packets_recv": "udpsrc 출력 기준 프레임별 수신 RTP 패킷 수(디코드 시점까지)",
    "udp_snmp": "/proc/net/snmp Udp 카운터(netns 단위)",
    "pts_ns": "rtph264depay src 패드 출력 PTS(ns). 매핑 키로 쓰는 값",
}


class Receiver:
    def __init__(
        self,
        cfg: CaptureConfig,
        run_id: str,
        diag_max: int = 200,
        gst_debug: str | None = None,
        gst_debug_max_bytes: int = GST_DEBUG_MAX_BYTES,
    ) -> None:
        init()
        self.cfg = cfg
        self.run_id = run_id
        self.run_dir = cfg.run_dir(run_id)
        self.gst_log: CappedLog | None = None
        self.gst_debug_info: dict[str, Any] = {"spec": gst_debug}
        if gst_debug:
            self._start_gst_debug(gst_debug, gst_debug_max_bytes)
        self.desc = receiver_pipeline(cfg)
        self.pipeline = Gst.parse_launch(self.desc)
        self.writer = rec.JsonlWriter(self.run_dir / rec.RECV_FILE)
        self.lock = threading.Lock()
        self.acc = FrameAccumulator()
        self.arrivals: dict[tuple[int, int], FrameAgg] = {}
        self.decoded_keys: set[tuple[int, int]] = set()
        self.late_packets = 0
        self.depay_in_cur: tuple[int, int] | None = None
        self.depay_in_prev: tuple[int, int] | None = None
        self.depay_in_marker = False
        self.pts_key: dict[int, tuple[tuple[int, int], int]] = {}  # pts → (key, depay_mono)
        self.depay_by_key: dict[tuple[int, int], tuple[int, int]] = {}  # key → (pts, depay_mono)
        self.diag_max = diag_max
        self.diag: dict[str, Any] = {
            "diag_max": diag_max,
            "parse_unmapped": 0,
            "parse_unmapped_samples": [],
            "dec_unmapped_samples": [],
            "last_mapped_pts_ns": None,
        }
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
            "gst_debug": self.gst_debug_info,
        }
        self._install_probes()

    def _start_gst_debug(self, spec: str, max_bytes: int) -> None:
        """GStreamer 기본 로그 함수(stderr, 상한 없음)를 빼고 상한 있는 파일로 보낸다.

        기본 로그 함수를 제거하지 못하면 stderr로 무제한 출력될 수 있으므로 활성화하지 않는다.
        """
        self.gst_log = CappedLog(self.run_dir / GST_DEBUG_FILE, max_bytes)
        try:
            removed = Gst.debug_remove_log_function(None)
            if not removed:
                raise RuntimeError("기본 로그 함수 제거 실패: 활성화하지 않음")
            Gst.debug_add_log_function(self._on_gst_log, None)
            Gst.debug_set_active(True)
            Gst.debug_set_threshold_from_string(spec, True)
            self.gst_debug_info["active"] = True
        except Exception as e:  # noqa: BLE001  진단 로그 실패로 수신을 멈추지 않는다
            self.gst_debug_info.update(active=False, error=repr(e))
            self.gst_log.close()

    def _on_gst_log(self, category, level, _file, function, _line, obj, message, *_user) -> None:
        log = self.gst_log
        if log is None or log.closed:
            return
        try:
            name = obj.get_name() if hasattr(obj, "get_name") else ("" if obj is None else str(obj))
            log.write(
                format_gst_line(
                    rec.now_ns()[0], Gst.debug_level_get_name(level), category.get_name(),
                    name or "", function or "", message.get() or "",
                )
            )  # fmt: skip
        except Exception:  # noqa: BLE001, S110  로그 콜백에서 예외를 올리지 않는다
            pass

    def _pad(self, name: str, pad: str) -> Gst.Pad:
        return self.pipeline.get_by_name(name).get_static_pad(pad)

    def _install_probes(self) -> None:
        both = Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST
        self._pad("src", "src").add_probe(both, self._on_udp)
        self._pad("depay", "sink").add_probe(both, self._on_depay_in)
        self._pad("depay", "src").add_probe(Gst.PadProbeType.BUFFER, self._on_depay_out)
        # h264parse는 이름이 없으므로 depay src의 peer로 찾는다(파이프라인 문자열은 그대로)
        parse = self._pad("depay", "src").get_peer().get_parent_element()
        parse.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._on_parsed)
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
                    self._store_arrival(agg)
        return Gst.PadProbeReturn.OK

    def _store_arrival(self, agg: FrameAgg) -> None:
        """lock 안에서 호출. 재정렬로 같은 프레임이 다시 닫히면 합친다."""
        key = (agg.ssrc, agg.rtp_ts)
        if key in self.decoded_keys:
            self.late_packets += agg.packets
        elif key in self.arrivals:
            self.arrivals[key].merge(agg)
        else:
            self.arrivals[key] = agg

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
                self.depay_by_key[key] = (buf.pts, mono)
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

    def _unmapped_sample(self, buf: Gst.Buffer, mono: int) -> dict[str, Any]:
        """lock 안에서 호출(상한 개수까지만 호출된다)."""
        return unmapped_sample(
            buf.pts, buf.dts, buf.duration, mono, self.diag["last_mapped_pts_ns"], self.pts_key
        )

    def _on_parsed(self, _pad, info) -> Gst.PadProbeReturn:
        mono, _ = rec.now_ns()
        buf = info.get_buffer()
        with self.lock:
            if buf.pts not in self.pts_key:
                self.diag["parse_unmapped"] += 1
                if len(self.diag["parse_unmapped_samples"]) < self.diag_max:
                    self.diag["parse_unmapped_samples"].append(self._unmapped_sample(buf, mono))
        return Gst.PadProbeReturn.OK

    def _on_decoded(self, _pad, info) -> Gst.PadProbeReturn:
        mono, epoch = rec.now_ns()
        buf = info.get_buffer()
        with self.lock:
            item = self.pts_key.pop(buf.pts, None)
            if item is None:
                self.meta.setdefault("decoded_unmapped", 0)
                self.meta["decoded_unmapped"] += 1
                if len(self.diag["dec_unmapped_samples"]) < self.diag_max:
                    self.diag["dec_unmapped_samples"].append(self._unmapped_sample(buf, mono))
                return Gst.PadProbeReturn.OK
            key, depay_mono = item
            agg = self.arrivals.pop(key, None)
            self.depay_by_key.pop(key, None)
            self.diag["last_mapped_pts_ns"] = _pts(buf.pts)
            self.decoded_keys.add(key)
            self.decoded += 1
            self.writer.write(
                self._record(key, agg, depay_mono, mono, epoch, decoded=True, pts=buf.pts)
            )
        return Gst.PadProbeReturn.OK

    def _record(self, key, agg, depay_mono, mono, epoch, decoded: bool, pts=None) -> dict:
        w, h = self.cur_size if (decoded and self.cur_size) else (None, None)
        return {
            "run_id": self.run_id,
            "ssrc": key[0],
            "rtp_ts": key[1],
            "packets_recv": agg.packets if agg else None,
            "bytes": agg.bytes if agg else None,
            "rtp_seq_first": agg.seq_first if agg else None,
            "rtp_seq_last": agg.seq_last if agg else None,
            "marker_seen": agg.marker_seen if agg else None,
            "first_pkt_mono_ns": agg.first_mono_ns if agg else None,
            "last_pkt_mono_ns": agg.last_mono_ns if agg else None,
            "depay_mono_ns": depay_mono,
            "pts_ns": None if pts is None else _pts(pts),
            "recv_mono_ns": mono,
            "recv_ts_ns": epoch,
            "decoded": decoded,
            "width": w,
            "height": h,
        }

    def _udp_rcvbuf(self) -> dict[str, Any]:
        """udpsrc 소켓에 실제 적용된 SO_RCVBUF를 읽는다(PLAYING 이후 호출)."""
        out: dict[str, Any] = {
            "requested_bytes": self.cfg.recv_buffer_bytes,
            "rmem_max": _read_int("/proc/sys/net/core/rmem_max"),
            "rmem_default": _read_int("/proc/sys/net/core/rmem_default"),
            "note": "actual_bytes는 커널 보고값(요청값의 2배, rmem_max*2로 제한)",
        }
        out["actual_bytes"] = None
        try:
            gsock = self.pipeline.get_by_name("src").get_property("used-socket")
            if gsock is not None:
                with socket.socket(fileno=os.dup(gsock.get_fd())) as s:
                    out["actual_bytes"] = s.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        except Exception as e:  # noqa: BLE001  측정 실패로 수신을 멈추지 않는다
            out["error"] = repr(e)
        return out

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

        self.meta["udp_snmp_start"] = read_udp_snmp()
        self.meta["start_mono_ns"], self.meta["start_ts_ns"] = rec.now_ns()
        self.pipeline.set_state(Gst.State.PLAYING)
        self.pipeline.get_state(Gst.CLOCK_TIME_NONE)
        self.meta["udp_rcvbuf"] = self._udp_rcvbuf()
        print(
            f"[receiver] READY run={self.run_id} port={self.cfg.rtp_port}"
            f" rcvbuf={self.meta['udp_rcvbuf'].get('actual_bytes')}",
            flush=True,
        )
        loop.run()
        self.meta["end_mono_ns"], self.meta["end_ts_ns"] = rec.now_ns()
        self.meta["udp_snmp_end"] = read_udp_snmp()
        self.meta["udp_snmp_delta"] = snmp_delta(
            self.meta["udp_snmp_start"], self.meta["udp_snmp_end"]
        )
        self.pipeline.set_state(Gst.State.NULL)
        with self.lock:
            for agg in self.acc.flush():
                self._store_arrival(agg)
            for key, agg in self.arrivals.items():
                # depay를 거쳤으나 디코더 출력과 매핑되지 않은 프레임은 depay 시각·PTS를 남긴다
                pts, depay_mono = self.depay_by_key.get(key, (None, None))
                self.writer.write(
                    self._record(key, agg, depay_mono, None, None, decoded=False, pts=pts)
                )
            self.diag.update(leftover_summary(self.pts_key, self.diag_max))
            self.meta["pts_diag"] = self.diag
            self.meta["packets"] = self.packets
            self.meta["frames_decoded"] = self.decoded
            self.meta["frames_not_decoded"] = len(self.arrivals)
            self.meta["late_packets_after_decode"] = self.late_packets
        self.writer.close()
        if self.gst_log is not None:
            if self.gst_debug_info.get("active"):
                Gst.debug_set_threshold_from_string("*:0", True)
            self.gst_log.close()
            self.gst_debug_info.update(self.gst_log.summary())
        rec.write_json(self.run_dir / rec.RECEIVER_META, self.meta)
        print(
            f"[receiver] packets={self.packets} decoded={self.decoded}"
            f" not_decoded={self.meta['frames_not_decoded']}"
            f" RcvbufErrors+={self.meta['udp_snmp_delta'].get('RcvbufErrors')}"
            f" errors={len(self.meta['errors'])}",
            flush=True,
        )
        return 1 if self.meta["errors"] else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="RTP/UDP 프레임 수신·디코드")
    p.add_argument("--run-id", required=True)
    p.add_argument("--duration", type=float, default=90.0, help="최대 실행 초")
    p.add_argument("--idle-timeout", type=float, default=3.0, help="첫 패킷 이후 무수신 종료 초")
    p.add_argument("--diag-max", type=int, default=200, help="PTS 매핑 진단 샘플 상한(종류별)")
    p.add_argument("--gst-debug", default=None, help="GST 디버그 스펙(RECV_GST_DEBUG보다 우선)")
    p.add_argument("--gst-debug-max-bytes", type=int, default=None, help="gst_debug.log 상한")
    args = p.parse_args(argv)
    spec, max_bytes = gst_debug_settings(args.gst_debug, args.gst_debug_max_bytes, os.environ)
    rx = Receiver(
        CaptureConfig.from_env(), args.run_id, diag_max=args.diag_max,
        gst_debug=spec, gst_debug_max_bytes=max_bytes,
    )  # fmt: skip
    return rx.run(args.duration, args.idle_timeout)


if __name__ == "__main__":
    sys.exit(main())
