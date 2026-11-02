"""GStreamer 루프백 통합 테스트(브로커 불필요, GStreamer·PyGObject 필요).

실행: uv run pytest -m integration tests/integration/test_capture_loopback.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("gi")

from pipe.capture import config as rec  # noqa: E402
from pipe.capture.config import CaptureConfig  # noqa: E402
from pipe.capture.knobs import analyze  # noqa: E402
from pipe.capture.match import run as match_run  # noqa: E402
from pipe.capture.pipelines import record_clip_pipeline  # noqa: E402

pytestmark = pytest.mark.integration

PORT = "5094"  # 측정 기본 포트(5004)와 겹치지 않게


def _env(runs_dir: Path, **extra: str) -> dict[str, str]:
    env = {**os.environ, "RUNS_DIR": str(runs_dir), "RTP_PORT": PORT}
    env.update(extra)
    return env


def _run_pair(env: dict, run_id: str, sender_args: list[str], recv_timeout: float) -> None:
    recv = subprocess.Popen(
        [sys.executable, "-m", "pipe.capture.receiver", "--run-id", run_id,
         "--duration", str(recv_timeout), "--idle-timeout", "2"],
        env=env, stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        line = recv.stdout.readline()
        assert "READY" in line, line
        send = subprocess.run(
            [sys.executable, "-m", "pipe.capture.sender", "--run-id", run_id, *sender_args],
            env=env, capture_output=True, text=True, timeout=recv_timeout,
        )  # fmt: skip
        assert send.returncode == 0, send.stderr + send.stdout
        assert recv.wait(timeout=recv_timeout) == 0
    finally:
        if recv.poll() is None:
            recv.kill()


def test_loopback_test_source_matches(tmp_path):
    env = _env(tmp_path)
    _run_pair(env, "it-test", ["--mode", "test", "--duration", "5"], 30)
    s = match_run(tmp_path / "it-test")
    assert s["frames_sent"] >= 140
    assert s["match_rate"] >= 0.99
    assert s["latency_ms"]["total"]["p95"] < 100
    meta = rec.read_json(tmp_path / "it-test" / rec.SENDER_META)
    assert "센서 시각이 아니다" in meta["definitions"]["capture_ts"]


def test_loopback_clip_mode(tmp_path):
    clip = tmp_path / "clip.mp4"
    # 장치 없이 짧은 H.264 mp4 생성(videotestsrc)
    desc = record_clip_pipeline(CaptureConfig(), 90, str(clip)).replace(
        f"v4l2src device={CaptureConfig().device} num-buffers=90 ! video/x-raw,format=YUY2",
        "videotestsrc num-buffers=90 pattern=ball ! video/x-raw,format=I420",
    )
    subprocess.run(["gst-launch-1.0", "-e", "-q", *desc.split()], check=True, timeout=60)
    env = _env(tmp_path, CLIP_PATH=str(clip))
    _run_pair(env, "it-clip", ["--mode", "clip", "--duration", "10"], 30)
    s = match_run(tmp_path / "it-clip")
    assert 85 <= s["frames_sent"] <= 91  # 3초 클립, EOS로 종료
    assert s["match_rate"] >= 0.99


def test_runtime_bitrate_change(tmp_path):
    env = _env(tmp_path)
    _run_pair(
        env,
        "it-knob",
        ["--mode", "test", "--test-pattern", "snow", "--duration", "10",
         "--schedule", "4:bitrate=500,7:caps=320x240@15"],
        40,
    )  # fmt: skip
    out = analyze(tmp_path / "it-knob")
    assert out["bitrate_runtime_change"] == "pass", out
    caps = [e for e in out["events"] if e["kind"] == "caps"][0]
    assert caps["switched"] is True


@pytest.mark.skipif(not Path("/dev/video0").exists(), reason="/dev/video0 없음")
def test_loopback_webcam(tmp_path):
    env = _env(tmp_path)
    _run_pair(env, "it-cam", ["--mode", "webcam", "--duration", "3"], 30)
    s = match_run(tmp_path / "it-cam")
    assert s["match_rate"] >= 0.99
    sent = rec.read_jsonl(tmp_path / "it-cam" / rec.SENT_FILE)
    assert all(r["src_mono_ns"] is not None for r in sent)
