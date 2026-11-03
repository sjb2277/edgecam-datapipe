"""시나리오 적용 실행기: 구간 경계마다 netem을 바꾸고 1초마다 link.metrics를 기록한다.

실행(호스트 netns, 사용자 권한. tc는 `sudo -n`으로 호출하므로 sudo 자격 캐시가 필요):
    uv run python -m pipe.link.runner --scenario scenarios/delay.yaml --run-id r1 --duration 90

- 구간 전환은 1초 tick 단위로 반영한다(계단식 프로파일).
- 종료(정상·예외·SIGINT·SIGTERM) 시 root qdisc를 삭제해 열화 없음으로 되돌린다.
- 산출물: <RUNS_DIR>/<run_id>/link_metrics.jsonl, link_meta.json
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pipe.capture import config as rec
from pipe.link.metrics import link_metrics_record
from pipe.link.netem import tc_clear_cmd, tc_replace_cmd, tc_show_cmd
from pipe.link.scenario import Scenario, load_scenario

METRICS_FILE = "link_metrics.jsonl"
META_FILE = "link_meta.json"


@dataclass(frozen=True)
class LinkConfig:
    ns_ue: str = "ue"
    dev_ue: str = "veth-ue"
    runs_dir: Path = Path("data/runs")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LinkConfig:
        env = os.environ if env is None else env
        d = cls()
        return cls(
            ns_ue=env.get("LINK_NS_UE", d.ns_ue),
            dev_ue=env.get("LINK_DEV_UE", d.dev_ue),
            runs_dir=Path(env.get("RUNS_DIR", str(d.runs_dir))),
        )


Exec = Callable[[list[str], bool], str]


def make_exec(prefix: list[str], dry_run: bool) -> Exec:
    def _exec(cmd: list[str], check: bool) -> str:
        full = prefix + cmd
        if dry_run:
            print("[link] DRY", " ".join(full), flush=True)
            return ""
        r = subprocess.run(full, capture_output=True, text=True, timeout=10)
        if check and r.returncode != 0:
            raise RuntimeError(f"{' '.join(full)} → rc={r.returncode}: {r.stderr.strip()}")
        return r.stdout

    return _exec


class _Stop(Exception):
    pass


def _raise_stop(signum, frame):  # noqa: ARG001
    raise _Stop(signum)


def run(
    scn: Scenario,
    cfg: LinkConfig,
    run_id: str,
    duration_s: float,
    exec_: Exec,
    scenario_path: str = "",
) -> int:
    run_dir = cfg.runs_dir / run_id
    writer = rec.JsonlWriter(run_dir / METRICS_FILE)
    meta: dict = {
        "run_id": run_id,
        "role": "link",
        "scenario": scn.name,
        "scenario_path": scenario_path,
        "ns": cfg.ns_ue,
        "dev": cfg.dev_ue,
        "direction": "ue->edge (egress of dev in ns)",
        "applies": [],
        "errors": [],
    }
    old_int = signal.signal(signal.SIGINT, _raise_stop)
    old_term = signal.signal(signal.SIGTERM, _raise_stop)
    cur_idx = -1
    try:
        t0_mono, t0_ns = rec.now_ns()
        meta["start_mono_ns"], meta["start_ts_ns"] = t0_mono, t0_ns
        k = 0
        while True:
            mono, ts = rec.now_ns()
            t = (mono - t0_mono) / 1e9
            if t >= duration_s:
                break
            idx = scn.segment_index_at(t)
            seg = scn.segments[idx]
            if idx != cur_idx:
                cmd = tc_replace_cmd(cfg.ns_ue, cfg.dev_ue, seg)
                exec_(cmd, True)
                shown = exec_(tc_show_cmd(cfg.ns_ue, cfg.dev_ue), False)
                meta["applies"].append(
                    {"t_s": round(t, 3), "segment_index": idx, "mono_ns": mono, "ts_ns": ts,
                     "cmd": " ".join(cmd), "tc_show": shown.strip()}
                )  # fmt: skip
                print(f"[link] t={t:.1f}s seg={idx} {' '.join(cmd[4:])}", flush=True)
                cur_idx = idx
            writer.write(
                link_metrics_record(
                    run_id=run_id, scenario=scn.name, segment_index=idx, seg=seg,
                    event_ts_ns=ts, mono_ns=mono,
                )
            )  # fmt: skip
            k += 1
            sleep_s = t0_mono / 1e9 + k - time.monotonic_ns() / 1e9
            if sleep_s > 0:
                time.sleep(sleep_s)
    except _Stop as e:
        meta["stopped_by_signal"] = int(e.args[0])
    except Exception as e:  # noqa: BLE001
        meta["errors"].append(repr(e))
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
        try:
            final = exec_(tc_show_cmd(cfg.ns_ue, cfg.dev_ue), False)
            meta["tc_show_before_clear"] = final.strip()
            exec_(tc_clear_cmd(cfg.ns_ue, cfg.dev_ue), False)
            meta["cleared"] = True
        except Exception as e:  # noqa: BLE001
            meta["errors"].append(f"clear: {e!r}")
            meta["cleared"] = False
        meta["end_mono_ns"], meta["end_ts_ns"] = rec.now_ns()
        meta["records"] = writer.count
        writer.close()
        rec.write_json(run_dir / META_FILE, meta)
    print(f"[link] records={meta['records']} errors={len(meta['errors'])}", flush=True)
    return 1 if meta["errors"] else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="링크 시나리오 적용(netem) + link.metrics 기록")
    p.add_argument("--scenario", required=True, help="scenarios/*.yaml")
    p.add_argument("--run-id", required=True)
    p.add_argument("--duration", type=float, required=True, help="초")
    p.add_argument("--dry-run", action="store_true", help="tc를 실행하지 않고 명령만 출력")
    p.add_argument("--no-sudo", action="store_true", help="이미 root일 때")
    args = p.parse_args(argv)

    scn = load_scenario(args.scenario)
    cfg = LinkConfig.from_env()
    prefix = [] if args.no_sudo else ["sudo", "-n"]
    return run(scn, cfg, args.run_id, args.duration, make_exec(prefix, args.dry_run), args.scenario)


if __name__ == "__main__":
    sys.exit(main())
