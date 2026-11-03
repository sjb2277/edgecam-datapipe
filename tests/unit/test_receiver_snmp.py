from pipe.capture.receiver import read_udp_snmp, snmp_delta

SNMP = """Ip: Forwarding DefaultTTL
Ip: 1 64
Udp: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors SndbufErrors
Udp: 100 2 9 50 9 0
UdpLite: InDatagrams NoPorts
UdpLite: 0 0
"""


def test_read_udp_snmp_parses_udp_only(tmp_path):
    f = tmp_path / "snmp"
    f.write_text(SNMP)
    d = read_udp_snmp(str(f))
    assert d["RcvbufErrors"] == 9 and d["InDatagrams"] == 100 and "Forwarding" not in d


def test_read_udp_snmp_missing_returns_empty(tmp_path):
    assert read_udp_snmp(str(tmp_path / "none")) == {}


def test_snmp_delta():
    assert snmp_delta({"RcvbufErrors": 9, "X": 1}, {"RcvbufErrors": 12}) == {"RcvbufErrors": 3}
