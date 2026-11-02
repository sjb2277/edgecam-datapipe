from pathlib import Path

import pytest

from pipe.capture.config import CaptureConfig
from pipe.capture.pipelines import receiver_pipeline, record_clip_pipeline, sender_pipeline


def test_config_from_env_overrides_and_defaults():
    cfg = CaptureConfig.from_env({"CAPTURE_FPS": "15", "RTP_PORT": "6000", "RUNS_DIR": "x/runs"})
    assert (cfg.fps, cfg.rtp_port, cfg.width, cfg.height) == (15, 6000, 640, 480)
    assert cfg.run_dir("r1") == Path("x/runs/r1")
    assert CaptureConfig.from_env({}).to_dict()["clip_path"] == "data/clips/d02_tmp.mp4"


def test_webcam_sender_pipeline():
    p = sender_pipeline(CaptureConfig(), "webcam")
    assert p.startswith("v4l2src name=src device=/dev/video0 do-timestamp=true")
    assert "format=YUY2,width=640,height=480,framerate=30/1" in p
    # 공통 뒷부분: 실시간 동기화는 인코더 앞(pace), udpsink는 sync=false
    assert "capsfilter name=rawcaps" in p
    assert p.index("identity name=pace sync=true") < p.index("x264enc name=enc")
    assert "tune=zerolatency" in p and "bframes=0" in p and "pass=cbr" in p
    assert "rtph264pay name=pay pt=96 config-interval=-1" in p
    assert p.endswith("sync=false async=false")


def test_clip_sender_pipeline_uses_same_tail():
    cfg = CaptureConfig(clip_path=Path("data/clips/a.mp4"), fps=15)
    clip = sender_pipeline(cfg, "clip")
    cam = sender_pipeline(cfg, "webcam")
    assert clip.startswith('filesrc name=src location="data/clips/a.mp4" ! qtdemux')
    assert "decodebin" not in clip
    tail = " ! videoconvert ! videoscale ! videorate"
    assert clip[clip.index(tail) :] == cam[cam.index(tail) :]
    assert "framerate=15/1" in clip


def test_test_mode_pattern_and_unknown_mode():
    assert "pattern=snow" in sender_pipeline(CaptureConfig(), "test", "snow")
    with pytest.raises(ValueError):
        sender_pipeline(CaptureConfig(), "rtsp")  # type: ignore[arg-type]


def test_receiver_pipeline():
    p = receiver_pipeline(CaptureConfig(rtp_port=5006, jitter_latency_ms=50))
    assert p.startswith("udpsrc name=src port=5006")
    assert "rtpjitterbuffer name=jbuf latency=50" in p
    assert "avdec_h264 name=dec thread-type=slice" in p


def test_record_clip_pipeline():
    p = record_clip_pipeline(CaptureConfig(), 2700, "data/clips/x.mp4")
    assert "num-buffers=2700" in p and p.endswith('filesink location="data/clips/x.mp4"')
