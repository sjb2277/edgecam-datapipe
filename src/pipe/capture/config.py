"""캡처·송수신 설정과 run 디렉터리 기록 형식.

설정 값은 환경변수(.env)에서 주입하고, 없으면 .env.example과 같은 기본값을 쓴다.
시각 필드는 모두 ns 정수다.
- *_mono_ns: CLOCK_MONOTONIC. 같은 호스트의 프로세스·netns가 공유하므로 지연 계산에 쓴다.
- *_ts_ns: epoch(CLOCK_REALTIME). 이벤트 시각 표기용.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SENT_FILE = "sent.jsonl"
RECV_FILE = "recv.jsonl"
SENDER_META = "meta.json"
RECEIVER_META = "recv_meta.json"
SUMMARY_FILE = "summary.json"
KNOBS_FILE = "knobs.json"


@dataclass(frozen=True)
class CaptureConfig:
    device: str = "/dev/video0"
    webcam_format: str = "YUY2"
    width: int = 640
    height: int = 480
    fps: int = 30
    clip_path: Path = Path("data/clips/d02_tmp.mp4")
    rtp_host: str = "127.0.0.1"
    rtp_port: int = 5004
    bitrate_kbps: int = 2000
    speed_preset: str = "ultrafast"
    key_int_max: int = 60
    vbv_buf_ms: int = 300
    mtu: int = 1400
    jitter_latency_ms: int = 50
    # udpsrc buffer-size(SO_RCVBUF 요청값, bytes). 0이면 OS 기본값.
    # 커널은 2배로 보고하고 rmem_max로 제한한다.
    recv_buffer_bytes: int = 8_388_608
    runs_dir: Path = Path("data/runs")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> CaptureConfig:
        env = os.environ if env is None else env
        d = cls()

        def s(key: str, default: str) -> str:
            return env.get(key, default)

        def i(key: str, default: int) -> int:
            return int(env.get(key, default))

        return cls(
            device=s("CAPTURE_DEVICE", d.device),
            webcam_format=s("CAPTURE_WEBCAM_FORMAT", d.webcam_format),
            width=i("CAPTURE_WIDTH", d.width),
            height=i("CAPTURE_HEIGHT", d.height),
            fps=i("CAPTURE_FPS", d.fps),
            clip_path=Path(s("CLIP_PATH", str(d.clip_path))),
            rtp_host=s("RTP_HOST", d.rtp_host),
            rtp_port=i("RTP_PORT", d.rtp_port),
            bitrate_kbps=i("X264_BITRATE_KBPS", d.bitrate_kbps),
            speed_preset=s("X264_SPEED_PRESET", d.speed_preset),
            key_int_max=i("X264_KEY_INT_MAX", d.key_int_max),
            vbv_buf_ms=i("X264_VBV_BUF_MS", d.vbv_buf_ms),
            mtu=i("RTP_MTU", d.mtu),
            jitter_latency_ms=i("JITTER_LATENCY_MS", d.jitter_latency_ms),
            recv_buffer_bytes=i("RTP_RECV_BUFFER_BYTES", d.recv_buffer_bytes),
            runs_dir=Path(s("RUNS_DIR", str(d.runs_dir))),
        )

    def to_dict(self) -> dict:
        out = asdict(self)
        out["clip_path"] = str(self.clip_path)
        out["runs_dir"] = str(self.runs_dir)
        return out

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id


def now_ns() -> tuple[int, int]:
    """(monotonic_ns, epoch_ns)"""
    return time.monotonic_ns(), time.time_ns()


class JsonlWriter:
    """GStreamer 스트리밍 스레드에서 호출되므로 lock으로 보호한다."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._f = path.open("w", encoding="utf-8")
        self._lock = threading.Lock()
        self.count = 0

    def write(self, rec: dict[str, Any]) -> None:
        line = json.dumps(rec, separators=(",", ":"))
        with self._lock:
            self._f.write(line + "\n")
            self.count += 1

    def close(self) -> None:
        with self._lock:
            self._f.close()


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def default_run_id(prefix: str = "d02") -> str:
    return f"{prefix}-{time.strftime('%Y%m%dT%H%M%S')}"
