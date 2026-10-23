#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_phase_detection.py — 수집 CSV 에 flight_id/phase 를 붙여 저장하고 구간 요약을 출력한다.

  py run_phase_detection.py logs\\flight_20260924_000626.csv
  py run_phase_detection.py logs\\*.csv --out phased

출력
  <out>/<파일명>_phased.csv   : FlightState 열 + vs_smooth + phase
  <out>/<파일명>_phases.txt   : 비행별 단계 구간(시작/종료/지속) 및 전이 기록
"""
import argparse, csv, glob, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flight_state import iter_states, COLUMNS
from phase_detector import PhaseDetector


def process(path: str, out_dir: str, quiet: bool = False) -> None:
    stem = os.path.splitext(os.path.basename(path))[0]
    os.makedirs(out_dir, exist_ok=True)
    det = PhaseDetector()
    rows, segs = [], []          # segs: [flight_id, phase, t0, t1, n]
    for s in iter_states(path):
        ph = det.update(s)
        d = s.as_dict(); d["vs_smooth"] = round(det.vs_smooth, 1); d["phase"] = ph
        rows.append(d)
        if s.paused:
            continue
        if segs and segs[-1][0] == s.flight_id and segs[-1][1] == ph:
            segs[-1][3] = s.t_sim; segs[-1][4] += 1
        else:
            segs.append([s.flight_id, ph, s.t_sim, s.t_sim, 1])

    out_csv = os.path.join(out_dir, stem + "_phased.csv")
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS + ["vs_smooth", "phase"])
        w.writeheader(); w.writerows(rows)

    lines = [f"# {os.path.basename(path)}  rows={len(rows)}", "",
             "## 단계 구간 (paused 제외)", f"{'flight':>6} {'phase':<13} {'t_start':>8} {'t_end':>8} {'dur_s':>7} {'n':>6}"]
    for fid, ph, t0, t1, n in segs:
        lines.append(f"{fid:>6} {ph:<13} {t0:8.1f} {t1:8.1f} {t1 - t0:7.1f} {n:6d}")
    lines += ["", "## 전이 기록", f"{'flight':>6} {'t_sim':>8} {'seq':>6}  {'from':<13}-> {'to':<13} note"]
    for tr in det.transitions:
        lines.append(f"{tr.flight_id:>6} {tr.t_sim:8.1f} {tr.seq:6d}  {tr.from_phase:<13}-> {tr.to_phase:<13} {tr.note}")
    txt = "\n".join(lines)
    with open(os.path.join(out_dir, stem + "_phases.txt"), "w", encoding="utf-8") as fh:
        fh.write(txt + "\n")
    if not quiet:
        print(txt); print(f"\n-> {out_csv}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", help="CSV 파일 또는 glob 패턴")
    ap.add_argument("--out", default="phased")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    files = []
    for p in a.paths:
        files += sorted(glob.glob(p)) or [p]
    for f in files:
        process(f, a.out, a.quiet)


if __name__ == "__main__":
    main()
