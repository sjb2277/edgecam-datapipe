import pytest

from pipe.link.netem import netem_args, tc_clear_cmd, tc_replace_cmd
from pipe.link.scenario import Segment


def _seg(**kw):
    return Segment(from_s=0, to_s=None, **kw)


def test_no_impairment_is_plain_netem():
    assert netem_args(_seg()) == ["netem"]


def test_delay_only():
    assert netem_args(_seg(delay_ms=100)) == ["netem", "delay", "100ms"]


def test_delay_with_jitter_uses_normal_distribution():
    assert netem_args(_seg(delay_ms=60, jitter_ms=15)) == [
        "netem", "delay", "60ms", "15ms", "distribution", "normal",
    ]  # fmt: skip


def test_fractional_values():
    assert netem_args(_seg(delay_ms=12.5, loss_pct=0.5)) == [
        "netem", "delay", "12.5ms", "loss", "0.5%",
    ]  # fmt: skip


def test_loss_only():
    assert netem_args(_seg(loss_pct=2)) == ["netem", "loss", "2%"]


@pytest.mark.parametrize("rate", [1, 500, 1500, 20000])
def test_rate_is_always_kbit_never_kbps(rate):
    args = netem_args(_seg(rate_kbps=rate))
    i = args.index("rate")
    assert args[i + 1] == f"{rate}kbit"
    # tc의 kbps는 킬로바이트/초라 8배 차이가 난다. 어떤 인자에도 들어가면 안 된다.
    assert not any("kbps" in a for a in args)
    assert not any(a.endswith(("bps", "Bps", "mbit", "Mbit")) for a in args)


@pytest.mark.parametrize(
    "seg",
    [_seg(), _seg(delay_ms=100), _seg(loss_pct=2), _seg(delay_ms=60, jitter_ms=15, loss_pct=1)],
)
def test_rate_omitted_when_not_set(seg):
    assert "rate" not in netem_args(seg)
    assert not any(a.endswith("kbit") for a in netem_args(seg))


def test_full_segment_order():
    seg = _seg(delay_ms=120, jitter_ms=30, loss_pct=5, rate_kbps=1500)
    assert netem_args(seg) == [
        "netem", "delay", "120ms", "30ms", "distribution", "normal",
        "loss", "5%", "rate", "1500kbit",
    ]  # fmt: skip


def test_replace_cmd_targets_ns_and_dev():
    cmd = tc_replace_cmd("ue", "veth-ue", _seg(delay_ms=100))
    assert cmd == [
        "ip", "netns", "exec", "ue", "tc", "qdisc", "replace", "dev", "veth-ue", "root",
        "netem", "delay", "100ms",
    ]  # fmt: skip


def test_clear_cmd():
    assert tc_clear_cmd("ue", "veth-ue") == [
        "ip", "netns", "exec", "ue", "tc", "qdisc", "del", "dev", "veth-ue", "root",
    ]  # fmt: skip
