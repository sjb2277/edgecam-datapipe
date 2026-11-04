"""GStreamer 파이프라인 문자열 생성(gi 미사용, 단위 테스트 대상).

송신 공통 구조:
  <소스> ! videoconvert ! videoscale ! videorate ! capsfilter name=rawcaps
         ! identity name=pace sync=true ! x264enc name=enc ! h264parse
         ! rtph264pay name=pay ! udpsink sync=false

- pace(identity sync=true)가 실시간 속도를 맞춘다. 클립 모드에서 인코더 입력 직전이
  곧 '프레임이 사용 가능해진 시각'이 되도록 udpsink가 아니라 인코더 앞에서 동기화한다.
- capture_ts는 pace src 패드(인코더 입력 직전)에서 기록한다.
"""

from __future__ import annotations

from typing import Literal

from pipe.capture.config import CaptureConfig

Mode = Literal["webcam", "clip", "test"]

RTP_CAPS = "application/x-rtp,media=video,encoding-name=H264,clock-rate=90000,payload=96"


def raw_caps(width: int, height: int, fps: int) -> str:
    return f"video/x-raw,format=I420,width={width},height={height},framerate={fps}/1"


def source_segment(cfg: CaptureConfig, mode: Mode, test_pattern: str = "ball") -> str:
    if mode == "webcam":
        return (
            f"v4l2src name=src device={cfg.device} do-timestamp=true"
            f" ! video/x-raw,format={cfg.webcam_format},width={cfg.width},"
            f"height={cfg.height},framerate={cfg.fps}/1"
        )
    if mode == "clip":
        # decodebin은 vaapi 디코더를 고를 수 있어 명시적으로 avdec_h264를 쓴다(클립은 H.264 mp4).
        return (
            f'filesrc name=src location="{cfg.clip_path}" ! qtdemux ! h264parse'
            " ! avdec_h264 thread-type=slice"
        )
    if mode == "test":
        # 장치 없는 통합 테스트용. ball=움직임 적음(약 200kbps), snow=고엔트로피(노브 검증용)
        return f"videotestsrc name=src is-live=true pattern={test_pattern}"
    raise ValueError(f"unknown mode: {mode}")


def encoder_segment(cfg: CaptureConfig) -> str:
    return (
        f"x264enc name=enc tune=zerolatency speed-preset={cfg.speed_preset}"
        f" bitrate={cfg.bitrate_kbps} pass=cbr vbv-buf-capacity={cfg.vbv_buf_ms}"
        f" key-int-max={cfg.key_int_max} bframes=0"
    )


def sender_pipeline(cfg: CaptureConfig, mode: Mode, test_pattern: str = "ball") -> str:
    return (
        f"{source_segment(cfg, mode, test_pattern)}"
        " ! videoconvert ! videoscale ! videorate"
        f" ! capsfilter name=rawcaps caps={raw_caps(cfg.width, cfg.height, cfg.fps)}"
        " ! identity name=pace sync=true"
        f" ! {encoder_segment(cfg)}"
        " ! h264parse"
        f" ! rtph264pay name=pay pt=96 config-interval=-1 mtu={cfg.mtu}"
        f" ! udpsink name=sink host={cfg.rtp_host} port={cfg.rtp_port} sync=false async=false"
    )


def receiver_pipeline(cfg: CaptureConfig) -> str:
    buf = f" buffer-size={cfg.recv_buffer_bytes}" if cfg.recv_buffer_bytes > 0 else ""
    return (
        f'udpsrc name=src port={cfg.rtp_port}{buf} caps="{RTP_CAPS}"'
        f" ! rtpjitterbuffer name=jbuf latency={cfg.jitter_latency_ms}"
        " ! rtph264depay name=depay ! h264parse"
        " ! avdec_h264 name=dec thread-type=slice"
        " ! fakesink name=sink sync=false async=false"
    )


def record_clip_pipeline(cfg: CaptureConfig, num_buffers: int, out_path: str) -> str:
    """웹캠 → H.264 mp4 녹화(임시 클립 생성용). 클립 품질을 위해 측정 비트레이트보다 높게 둔다."""
    return (
        f"v4l2src device={cfg.device} num-buffers={num_buffers}"
        f" ! video/x-raw,format={cfg.webcam_format},width={cfg.width},"
        f"height={cfg.height},framerate={cfg.fps}/1"
        " ! videoconvert ! x264enc speed-preset=veryfast bitrate=6000 key-int-max=30"
        f' ! h264parse ! mp4mux ! filesink location="{out_path}"'
    )
