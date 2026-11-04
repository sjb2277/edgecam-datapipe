import argparse
import uuid

from pipe.capture import config as rec
from pipe.capture.config import CaptureConfig, apply_overrides
from pipe.link.metrics import link_metrics_record
from pipe.link.runner import META_FILE, METRICS_FILE, LinkConfig, run
from pipe.link.scenario import Segment, parse_scenario

ENVELOPE = {"event_id", "run_id", "source", "schema_version", "event_ts", "ingest_ts"}


def test_record_fields_and_units():
    seg = Segment(from_s=0, to_s=None, delay_ms=100, loss_pct=2, rate_kbps=1500)
    r = link_metrics_record(
        run_id="r1", scenario="s", segment_index=0, seg=seg, event_ts_ns=123, mono_ns=456
    )
    assert ENVELOPE <= r.keys()
    uuid.UUID(r["event_id"])
    assert r["mode"] == "netem"
    assert (r["delay_ms"], r["jitter_ms"], r["loss_pct"], r["rate_kbps"]) == (100, 0, 2, 1500)
    assert not any(k in r for k in ("delay", "loss", "rate", "envelope"))  # 단위 접미사, 중첩 없음
    assert r["event_ts"] == 123 and r["mono_ns"] == 456 and r["ingest_ts"] is None


class FakeExec:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, check):
        self.calls.append(cmd)
        return "qdisc netem 8001: root" if "show" in cmd else ""


def test_runner_applies_on_boundaries_and_clears(tmp_path, monkeypatch):
    # 시간을 가짜로 진행시켜 tick마다 1초씩 흐르게 한다
    clock = {"ns": 1_000_000_000}

    def fake_now():
        return clock["ns"], clock["ns"] + 10**18

    def fake_sleep(s):
        clock["ns"] += int(s * 1e9)

    monkeypatch.setattr(rec, "now_ns", fake_now)
    monkeypatch.setattr("pipe.link.runner.time.monotonic_ns", lambda: clock["ns"])
    monkeypatch.setattr("pipe.link.runner.time.sleep", fake_sleep)

    scn = parse_scenario(
        {"name": "two", "segments": [
            {"from_s": 0, "to_s": 2, "delay_ms": 10},
            {"from_s": 2, "to_s": None, "loss_pct": 1},
        ]}
    )  # fmt: skip
    fx = FakeExec()
    rc = run(scn, LinkConfig(runs_dir=tmp_path), "r1", 4, fx)
    assert rc == 0

    replaces = [c for c in fx.calls if "replace" in c]
    assert [c[-2:] for c in replaces] == [["delay", "10ms"], ["loss", "1%"]]
    assert fx.calls[-1][-4:] == ["del", "dev", "veth-ue", "root"]  # 종료 시 원복

    recs = rec.read_jsonl(tmp_path / "r1" / METRICS_FILE)
    assert [r["segment_index"] for r in recs] == [0, 0, 1, 1]
    assert [r["delay_ms"] for r in recs] == [10, 10, 0, 0]
    meta = rec.read_json(tmp_path / "r1" / META_FILE)
    assert meta["cleared"] is True and meta["records"] == 4 and len(meta["applies"]) == 2


def test_runner_clears_even_on_apply_error(tmp_path):
    calls = []

    def failing(cmd, check):
        calls.append(cmd)
        if "replace" in cmd:
            raise RuntimeError("tc failed")
        return ""

    scn = parse_scenario({"name": "x", "segments": [{"from_s": 0, "delay_ms": 5}]})
    rc = run(scn, LinkConfig(runs_dir=tmp_path), "r2", 1, failing)
    assert rc == 1
    assert "del" in calls[-1]
    meta = rec.read_json(tmp_path / "r2" / META_FILE)
    assert meta["cleared"] is True and "tc failed" in meta["errors"][0]


def test_link_config_from_env():
    cfg = LinkConfig.from_env({"LINK_NS_UE": "a", "LINK_DEV_UE": "b", "RUNS_DIR": "/x"})
    assert (cfg.ns_ue, cfg.dev_ue, str(cfg.runs_dir)) == ("a", "b", "/x")
    assert LinkConfig.from_env({}).ns_ue == "ue"


def test_sender_host_override():
    base = CaptureConfig()
    ns = argparse.Namespace(bitrate=None, host="10.250.0.2")
    assert apply_overrides(base, ns).rtp_host == "10.250.0.2"
    ns = argparse.Namespace(bitrate=None, host=None)
    assert apply_overrides(base, ns).rtp_host == base.rtp_host
