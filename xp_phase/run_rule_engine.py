#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_rule_engine.py — 수집 CSV → (STEP 2) FlightState → (STEP 3) phase → (STEP 4) 규칙 평가

  py run_rule_engine.py ..\\logs\\flight_20260924_002110.csv
  py run_rule_engine.py ..\\logs\\*.csv --out situation

출력
  <out>/<이름>_situation.csv : FlightState + phase + active_rules (샘플별 status)
  <out>/<이름>_events.csv    : ONSET/CLEAR 이벤트 목록
  <out>/<이름>_events.txt    : 사람이 읽는 요약 (규칙별 발생 횟수, 시간순 이벤트)
"""
import argparse, csv, glob, os, sys
from collections import Counter
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flight_state import iter_states, COLUMNS
from phase_detector import PhaseDetector, GROUND_PHASES
from rule_engine import RuleEngine, RULES

EV_COLS = ["flight_id", "t_sim", "seq", "phase", "rule_id", "name", "kind", "severity", "value", "source"]


def process(path: str, out_dir: str, quiet: bool = False):
    stem = os.path.splitext(os.path.basename(path))[0]
    os.makedirs(out_dir, exist_ok=True)
    det, eng = PhaseDetector(), RuleEngine()
    rows, n_tr = [], 0
    for s in iter_states(path):
        ph = det.update(s)
        liftoff = touchdown = None
        if len(det.transitions) > n_tr:                       # 이번 샘플에서 전이 발생
            tr = det.transitions[-1]; n_tr = len(det.transitions)
            if tr.to_phase == "CLIMB" and (tr.from_phase in GROUND_PHASES or tr.from_phase == "LANDING_ROLL"):
                liftoff = det.edge_state or s
            if tr.to_phase in ("LANDING_ROLL", "IMPACT"):
                touchdown = det.edge_state or s
        eng.update(s, ph, liftoff, touchdown)
        d = s.as_dict(); d["vs_smooth"] = round(det.vs_smooth, 1); d["phase"] = ph
        d["active_rules"] = ";".join(eng.status())
        rows.append(d)

    with open(os.path.join(out_dir, stem + "_situation.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS + ["vs_smooth", "phase", "active_rules"]); w.writeheader(); w.writerows(rows)
    with open(os.path.join(out_dir, stem + "_events.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=EV_COLS); w.writeheader()
        for e in eng.events:
            w.writerow({k: getattr(e, k) for k in EV_COLS})

    onsets = [e for e in eng.events if e.kind == "ONSET"]
    cnt = Counter((e.rule_id, e.name, e.severity) for e in onsets)
    lines = [f"# {os.path.basename(path)}  rows={len(rows)}  events(ONSET)={len(onsets)}", "", "## 규칙별 ONSET 횟수"]
    for r in RULES:
        n = cnt.get((r.id, r.name, r.severity), 0)
        lines.append(f"  {r.id} {r.name:<24} {r.severity:<8} {n:3d}   [{r.source}]")
    lines += ["", "## 이벤트 (시간순)", f"{'flight':>6} {'t_sim':>8} {'seq':>6} {'phase':<13} {'rule':<5} {'kind':<6} {'sev':<8} value"]
    for e in eng.events:
        lines.append(f"{e.flight_id:>6} {e.t_sim:8.1f} {e.seq:6d} {e.phase:<13} {e.rule_id:<5} {e.kind:<6} {e.severity:<8} {e.value}")
    txt = "\n".join(lines)
    with open(os.path.join(out_dir, stem + "_events.txt"), "w", encoding="utf-8") as fh:
        fh.write(txt + "\n")
    if not quiet:
        print(txt, "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+"); ap.add_argument("--out", default="situation"); ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    files = []
    for p in a.paths:
        files += sorted(glob.glob(p)) or [p]
    for f in files:
        process(f, a.out, a.quiet)


if __name__ == "__main__":
    main()
