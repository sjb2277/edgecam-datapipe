from pathlib import Path

import pytest

from pipe.link.scenario import ScenarioError, load_scenario, parse_scenario

SCENARIOS = Path(__file__).resolve().parents[2] / "scenarios"


def _scn(*segs, name="t"):
    return {"name": name, "segments": list(segs)}


@pytest.mark.parametrize("name", ["normal", "delay", "loss", "cell_edge"])
def test_repo_scenarios_load(name):
    scn = load_scenario(SCENARIOS / f"{name}.yaml")
    assert scn.name == name
    assert scn.segments[0].from_s == 0
    assert scn.segments[-1].to_s is None


def test_d03_scenario_values():
    normal = load_scenario(SCENARIOS / "normal.yaml").segments[0]
    assert (normal.delay_ms, normal.jitter_ms, normal.loss_pct, normal.rate_kbps) == (0, 0, 0, None)
    delay = load_scenario(SCENARIOS / "delay.yaml").segments[0]
    assert (delay.delay_ms, delay.jitter_ms, delay.loss_pct) == (100, 0, 0)
    loss = load_scenario(SCENARIOS / "loss.yaml").segments[0]
    assert (loss.delay_ms, loss.loss_pct) == (0, 2)


def test_defaults_when_omitted():
    seg = parse_scenario(_scn({"from_s": 0, "to_s": None})).segments[0]
    assert seg.impairment() == {"delay_ms": 0, "jitter_ms": 0, "loss_pct": 0, "rate_kbps": None}


def test_segment_lookup_stepwise():
    scn = parse_scenario(
        _scn(
            {"from_s": 0, "to_s": 10, "delay_ms": 10},
            {"from_s": 10, "to_s": 20, "delay_ms": 50},
            {"from_s": 20, "to_s": None, "delay_ms": 5},
        )
    )
    assert scn.segment_index_at(0) == 0
    assert scn.segment_index_at(9.99) == 0
    assert scn.segment_index_at(10) == 1  # [from, to)
    assert scn.segment_index_at(25) == 2
    assert scn.segment_at(1e6).delay_ms == 5


def test_last_segment_with_end_holds_value():
    scn = parse_scenario(_scn({"from_s": 0, "to_s": 5, "loss_pct": 1}))
    assert scn.segment_index_at(100) == 0


@pytest.mark.parametrize(
    "data, msg",
    [
        ({"segments": [{"from_s": 0}]}, "name"),
        (_scn(), "비어 있지 않은"),
        (_scn({"from_s": 1, "to_s": None}), "from_s는 0"),
        (_scn({"to_s": None}), "from_s 누락"),
        (_scn({"from_s": 0, "to_s": 10}, {"from_s": 12, "to_s": None}), "빈틈"),
        (_scn({"from_s": 0, "to_s": 10}, {"from_s": 8, "to_s": None}), "겹침"),
        (_scn({"from_s": 0, "to_s": None}, {"from_s": 10, "to_s": None}), "마지막 구간만"),
        (_scn({"from_s": 0, "to_s": 0}), "to_s"),
        (_scn({"from_s": 0, "delay_ms": -1}), "음수"),
        (_scn({"from_s": 0, "loss_pct": 101}), "100 초과"),
        (_scn({"from_s": 0, "rate_kbps": 0}), "양의 정수"),
        (_scn({"from_s": 0, "rate_kbps": 1.5}), "양의 정수"),
        (_scn({"from_s": 0, "delay_ms": "100ms"}), "숫자"),
        (_scn({"from_s": 0, "rate": 100}), "알 수 없는 키"),
        ({"name": "x", "segments": [{"from_s": 0}], "extra": 1}, "최상위 키"),
    ],
)
def test_invalid_scenarios(data, msg):
    with pytest.raises(ScenarioError, match=msg):
        parse_scenario(data)
