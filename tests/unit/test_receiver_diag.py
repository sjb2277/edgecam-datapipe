import pytest

from pipe.capture.receiver import (
    CLOCK_TIME_NONE,
    GST_DEBUG_MAX_BYTES,
    CappedLog,
    format_gst_line,
    gst_debug_settings,
    leftover_summary,
    unmapped_sample,
)


def test_unmapped_sample_records_pending_range_and_none():
    s = unmapped_sample(300, CLOCK_TIME_NONE, 33, 1_000, 200, [500, 400, 600])
    assert (s["pts_ns"], s["dts_ns"], s["duration_ns"], s["mono_ns"]) == (300, None, 33, 1_000)
    assert s["last_mapped_pts_ns"] == 200
    assert (s["pending_count"], s["pending_pts_min_ns"], s["pending_pts_max_ns"]) == (3, 400, 600)
    e = unmapped_sample(1, 1, 1, 0, None, {})
    assert (e["pending_count"], e["pending_pts_min_ns"], e["pending_pts_max_ns"]) == (0, None, None)


def test_leftover_summary_sorted_and_limited():
    out = leftover_summary({30: "c", 10: "a", 20: "b"}, 2)
    assert out == {"pts_key_leftover": 3, "pts_key_leftover_sample_ns": [10, 20]}
    assert leftover_summary([], 5) == {"pts_key_leftover": 0, "pts_key_leftover_sample_ns": []}


def test_gst_debug_settings_cli_over_env_and_defaults():
    env = {"RECV_GST_DEBUG": "h264parse:4", "RECV_GST_DEBUG_MAX_BYTES": "1000"}
    assert gst_debug_settings(None, None, env) == ("h264parse:4", 1000)
    assert gst_debug_settings("libav:4", 50, env) == ("libav:4", 50)
    assert gst_debug_settings(None, None, {}) == (None, GST_DEBUG_MAX_BYTES)
    assert gst_debug_settings("  ", None, env)[0] is None  # 빈 스펙 = 비활성
    with pytest.raises(ValueError):
        gst_debug_settings("x:4", 0, {})


def test_format_gst_line():
    line = format_gst_line(5, "WARN", "videodecoder", "dec", "fn", "decreasing timestamp")
    assert line.startswith("5 WARN    videodecoder") and line.endswith("fn: decreasing timestamp\n")


def test_capped_log_truncates_once_and_counts(tmp_path):
    log = CappedLog(tmp_path / "g.log", max_bytes=10)
    log.write("abcd\n")  # 5
    log.write("efgh\n")  # 10 = 상한
    log.write("ij\n")  # 초과 → 잘림 표시 1줄
    log.write("kl\n")
    log.close()
    log.write("after close\n")  # 무시, 카운트 안 함
    text = (tmp_path / "g.log").read_text()
    assert text == "abcd\nefgh\n# truncated at 10 bytes\n"
    s = log.summary()
    assert (s["bytes_written"], s["lines_written"], s["lines_dropped"]) == (10, 2, 2)
    assert s["truncated"] is True and s["max_bytes"] == 10


def test_capped_log_under_limit_not_truncated(tmp_path):
    log = CappedLog(tmp_path / "sub" / "g.log", max_bytes=100)
    log.write("한글\n")
    log.close()
    s = log.summary()
    assert s["truncated"] is False and s["bytes_written"] == len("한글\n".encode())
