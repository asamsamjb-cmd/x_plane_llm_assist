#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
live_assist.py — STEP 7 (라이브 모드): X-Plane 실시간 수신 → 단계·규칙 판정 → 화면 표시 → 버튼으로 LLM 응답

  python live_assist.py --model qwen2.5:7b-instruct                  # X-Plane 과 같은 PC (localhost)
  python live_assist.py --model qwen2.5:7b-instruct --xp-host 192.168.0.10
  python live_assist.py --replay ..\\logs\\flight_20260924_002110.csv --speed 20 --model qwen2.5:7b-instruct   # X-Plane 없이 CSV 재생
  python live_assist.py --replay ..\\logs\\flight_20260924_002110.csv --speed 0 --no-ui                        # 화면 없이 판정만 점검

구성
  · 수신 스레드: xp_collector.LiveUDPSource(또는 CSVReplaySource) → FlightState → PhaseDetector → RuleEngine, 동시에 CSV 저장
  · 화면(Tkinter, 추가 설치 없음): 현재 값 · 비행단계 · 활성 경고 · 이벤트 로그 · 질문 입력 + "AI 응답 생성"
  · LLM 호출은 별도 스레드 → 화면이 멈추지 않음. 선택: WARNING 발생 시 자동 응답
"""
import argparse, csv, datetime as dt, os, queue, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)


def _find_dir(filename):
    """ROOT 아래(3단계)에서 filename 이 있는 폴더를 찾아 sys.path 에 추가한다. 못 찾으면 어디를 봤는지 알려준다."""
    for base in (HERE, ROOT):
        for dirpath, dirnames, filenames in os.walk(base):
            if dirpath[len(base):].count(os.sep) > 3 or any(x in dirpath for x in (".venv", ".idea", "__pycache__")):
                dirnames[:] = []; continue
            if filename in filenames:
                if dirpath not in sys.path:
                    sys.path.insert(0, dirpath)
                return dirpath
    sys.exit(f"[live_assist] {filename} 을(를) 찾지 못했습니다. 프로젝트 폴더({ROOT}) 안의 xp_phase / xp_kb 폴더와 xp_collector.py 를 확인하십시오.")


for fname in ("xp_collector.py", "flight_state.py", "kb_retriever.py", "run_assist_replay.py"):
    print(f"[live_assist] {fname} ← {_find_dir(fname)}")
from xp_collector import LiveUDPSource, CSVReplaySource, COLUMNS, _fmt_csv
from flight_state import FlightState
from phase_detector import PhaseDetector, GROUND_PHASES
from rule_engine import RuleEngine, RULES
from kb_retriever import KB
from run_assist_replay import SYSTEM_PROMPT, build_context, ask_ollama, fmt_state, pick_k, classify_intent, rule_answer, verify_answer, stream_ollama, has_chinese, KO_ONLY_SUFFIX

PHASE_KO = {"GROUND_STOP": "지상 정지", "TAXI": "택시", "TAKEOFF_ROLL": "이륙 활주", "CLIMB": "상승", "CRUISE": "순항",
            "DESCENT": "강하", "APPROACH": "접근", "GO_AROUND": "복행", "LANDING_ROLL": "착륙 활주", "IMPACT": "충돌"}
PHASE_COL = {"GROUND_STOP": "#d9d9d9", "TAXI": "#bdbdbd", "TAKEOFF_ROLL": "#fdd0a2", "CLIMB": "#c7e9c0", "CRUISE": "#9ecae1",
             "DESCENT": "#fdae6b", "APPROACH": "#e7ba52", "GO_AROUND": "#f7b6d2", "LANDING_ROLL": "#dadaeb", "IMPACT": "#e41a1c"}
SEV_COL = {"WARNING": "#b2182b", "CAUTION": "#e08214", "INFO": "#4575b4"}


class Engine:
    """수신 → 판정 공유 상태. UI 와 LLM 스레드가 lock 으로 읽는다."""

    def __init__(self, source, csv_path=None):
        self.source, self.csv_path = source, csv_path
        self.det, self.eng = PhaseDetector(), RuleEngine()
        self.lock = threading.Lock()
        self.state = None; self.phase = None; self.status = []; self.rows = 0; self.flight_id = 1; self.prev_t = None
        self.events_q = queue.Queue()          # UI 로그용 (Event)
        self.transitions_q = queue.Queue()     # 단계 전이 로그용
        self.n_tr = 0; self.n_ev = 0
        self.stop = threading.Event(); self.error = None

    def run(self):
        fh = w = None
        if self.csv_path:
            fh = open(self.csv_path, "w", newline="", encoding="utf-8"); w = csv.DictWriter(fh, fieldnames=COLUMNS); w.writeheader()
        try:
            for row in self.source.samples():
                if self.stop.is_set():
                    break
                if row is None:          # 수신 없음
                    with self.lock:
                        self.error = "X-Plane 수신 없음 — 실행 여부 / 비행 화면 / 방화벽 확인"
                    continue
                self.rows += 1
                now = time.time()
                row.setdefault("seq", self.rows); row.setdefault("wall_epoch", f"{now:.3f}")
                row.setdefault("wall_time", dt.datetime.fromtimestamp(now).isoformat(timespec="milliseconds"))
                if w is not None:
                    w.writerow({k: _fmt_csv(row.get(k)) for k in COLUMNS}); fh.flush()
                t = float(row["sim_time_s"])
                if self.prev_t is not None and t < self.prev_t - 1.0:
                    self.flight_id += 1
                self.prev_t = t
                s = FlightState.from_row(row, self.flight_id)
                ph = self.det.update(s)
                liftoff = touchdown = None
                if len(self.det.transitions) > self.n_tr:
                    tr = self.det.transitions[-1]; self.n_tr = len(self.det.transitions); self.transitions_q.put(tr)
                    if tr.to_phase == "CLIMB" and (tr.from_phase in GROUND_PHASES or tr.from_phase == "LANDING_ROLL"):
                        liftoff = self.det.edge_state or s
                    if tr.to_phase in ("LANDING_ROLL", "IMPACT"):
                        touchdown = self.det.edge_state or s
                new_events = self.eng.update(s, ph, liftoff, touchdown)
                for e in new_events:
                    self.events_q.put(e)
                with self.lock:
                    self.state, self.phase, self.status, self.error = s, ph, self.eng.status(), None
        except Exception as e:
            with self.lock:
                self.error = f"수신 스레드 오류: {e}"
        finally:
            if fh: fh.close()

    def snapshot(self):
        with self.lock:
            if self.state is None:
                return None
            s, ph, st = self.state, self.phase, list(self.status)
        last_onset = {}
        recent = [e for e in self.eng.events if e.flight_id == s.flight_id and e.kind == "ONSET" and s.t_sim - 60.0 <= e.t_sim <= s.t_sim]
        for e in self.eng.events:
            if e.kind == "ONSET":
                last_onset[e.rule_id] = e
        return s, ph, st, last_onset, recent[-5:]


def make_source(a):
    if a.replay:
        return CSVReplaySource(a.replay, a.speed)
    return LiveUDPSource(a.xp_host, a.xp_port, a.local_port, a.hz, a.timeout)


# ----------------------------------------------------------------------------- UI
def run_ui(engine, kb, a):
    import tkinter as tk
    from tkinter import ttk, scrolledtext
    root = tk.Tk(); root.title("X-Plane AI Assist — Live"); root.geometry("900x1040+0+0")
    root.attributes("-topmost", True)            # 항상 위 (X-Plane 창 위에 표시)
    F, FB, FBIG = ("Malgun Gothic", 11), ("Malgun Gothic", 11, "bold"), ("Malgun Gothic", 26, "bold")
    rule_by_id = {r.id: r for r in RULES}
    llm_busy = threading.Event()

    # --- 현재 값
    f1 = ttk.LabelFrame(root, text=" 비행 상태 (실시간) "); f1.pack(fill="x", padx=10, pady=(10, 4))
    fields = [("IAS", "kt"), ("GS", "kt"), ("고도 MSL", "ft"), ("고도 AGL", "ft"), ("승강률", "fpm"), ("방위", "°"),
              ("피치 / 롤", "°"), ("스로틀 / RPM", ""), ("플랩", "°"), ("접지", ""), ("시뮬 시각", "s"), ("비행", ""),
              ("목적지 · 거리", ""), ("남은 시간", ""), ("연료", ""), ("바람 (측풍)", "")]
    vals = {}
    for i, (name, unit) in enumerate(fields):
        r_, c_ = divmod(i, 4)
        ttk.Label(f1, text=name, font=F).grid(row=r_*2, column=c_, sticky="w", padx=12, pady=(6, 0))
        v = tk.StringVar(value="—"); vals[name] = v
        ttk.Label(f1, textvariable=v, font=("Consolas", 15, "bold")).grid(row=r_*2+1, column=c_, sticky="w", padx=12, pady=(0, 6))

    # --- 단계 / 경고
    f2 = ttk.Frame(root); f2.pack(fill="x", padx=10, pady=4)
    phase_var = tk.StringVar(value="대기 중")
    phase_lbl = tk.Label(f2, textvariable=phase_var, font=FBIG, bg="#eeeeee", width=22, height=2); phase_lbl.pack(side="left", padx=(0, 8))
    warn_frame = ttk.LabelFrame(f2, text=" 활성 경고 (status) "); warn_frame.pack(side="left", fill="both", expand=True)
    warn_txt = tk.Text(warn_frame, height=4, font=F, wrap="word"); warn_txt.pack(fill="both", expand=True, padx=4, pady=4)
    for sev, col in SEV_COL.items():
        warn_txt.tag_config(sev, foreground=col, font=FB)

    # --- 이벤트 로그
    f3 = ttk.LabelFrame(root, text=" 이벤트 · 단계 전이 로그 "); f3.pack(fill="both", expand=True, padx=10, pady=4)
    log = scrolledtext.ScrolledText(f3, height=9, font=("Consolas", 10), wrap="word"); log.pack(fill="both", expand=True, padx=4, pady=4)
    for sev, col in SEV_COL.items():
        log.tag_config(sev, foreground=col)
    log.tag_config("PHASE", foreground="#1f4e79", font=("Consolas", 10, "bold"))

    # --- 질의 / 응답
    f4 = ttk.LabelFrame(root, text=" AI Assist (로컬 LLM: %s) " % a.model); f4.pack(fill="both", expand=True, padx=10, pady=4)
    qrow = ttk.Frame(f4); qrow.pack(fill="x", padx=4, pady=4)
    q_var = tk.StringVar(value="지금 뭘 확인해야 해?")
    ttk.Entry(qrow, textvariable=q_var, font=F).pack(side="left", fill="x", expand=True)
    auto_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(qrow, text="WARNING 발생 시 자동 응답", variable=auto_var).pack(side="left", padx=8)
    top_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(qrow, text="항상 위", variable=top_var, command=lambda: root.attributes("-topmost", top_var.get())).pack(side="left", padx=4)
    btn = ttk.Button(qrow, text="AI 응답 생성"); btn.pack(side="left")
    src_var = tk.StringVar(value=""); ttk.Label(f4, textvariable=src_var, font=("Malgun Gothic", 9), foreground="#555").pack(fill="x", padx=6)
    resp = scrolledtext.ScrolledText(f4, height=9, font=F, wrap="word"); resp.pack(fill="both", expand=True, padx=4, pady=4)
    resp.tag_config("H", font=FB, foreground="#1f4e79"); resp.tag_config("RULE", font=FB); resp.tag_config("OK", foreground="#2a7d2a"); resp.tag_config("BAD", foreground="#b2182b")
    status_var = tk.StringVar(value="연결 대기 …"); ttk.Label(root, textvariable=status_var, font=("Malgun Gothic", 9), foreground="#555").pack(fill="x", padx=10, pady=(0, 8))

    def ask(question, auto=False):
        snap = engine.snapshot()
        if snap is None or llm_busy.is_set():
            return
        llm_busy.set(); btn.state(["disabled"])
        s, ph, st, last_onset, recent = snap
        hits = kb.retrieve(question, phase=ph, k=pick_k(question, a.k))
        ctx = build_context(s, ph, st, last_onset, recent, hits, question, RULES, KB)
        ra = rule_answer(classify_intent(question), s, ph, st, last_onset, hits, RULES)
        src_var.set("근거: " + "; ".join(f"POH §{h['section']} {h['path'][:38]} p.{h['page']}" for h in hits) + f"   | 단계 {ph}, 경고 {st or '없음'}")
        resp.delete("1.0", "end")
        resp.insert("end", f"[{dt.datetime.now():%H:%M:%S} · t={s.t_sim:.0f}s · {PHASE_KO.get(ph, ph)}] {'[자동] ' if auto else ''}{question}\n\n")
        resp.insert("end", "■ 규칙 답변 (즉시)\n", "H"); resp.insert("end", ra + "\n\n", "RULE")
        resp.insert("end", "■ AI 보충 설명 (%s)\n" % a.model, "H")
        def work():
            t0 = time.time(); buf = []
            def on_tok(tok):
                buf.append(tok); root.after(0, lambda: (resp.insert("end", tok), resp.see("end")))
            try:
                text = stream_ollama(a.host, a.model, SYSTEM_PROMPT, ctx, on_tok)
                if has_chinese(text):                       # 중국어로 새면 한국어로 한 번 재생성
                    root.after(0, lambda: resp.insert("end", "\n(중국어 감지 → 한국어로 다시 생성 중…)\n"))
                    text = stream_ollama(a.host, a.model, SYSTEM_PROMPT, ctx + KO_ONLY_SUFFIX, on_tok)
            except Exception as e:
                text = ""; root.after(0, lambda: resp.insert("end", f"(오류: {e}) Ollama 실행 여부와 --host, 모델 이름을 확인하십시오.\n"))
            ok, issues = verify_answer(text, ctx, hits, s) if text else (False, ["응답 없음"])
            def done():
                resp.insert("end", f"\n\n[검증] " + ("통과" if ok else "주의 — " + "; ".join(issues)) + f"   ({time.time()-t0:.1f} s)\n", "OK" if ok else "BAD")
                resp.see("end"); btn.state(["!disabled"]); llm_busy.clear()
            root.after(0, done)
        threading.Thread(target=work, daemon=True).start()
    btn.configure(command=lambda: ask(q_var.get().strip() or "지금 뭘 확인해야 해?"))

    def refresh():
        snap = engine.snapshot()
        if snap is not None:
            s, ph, st, last_onset, _ = snap
            vals["IAS"].set(f"{s.ias_kt:5.1f} kt"); vals["GS"].set(f"{s.gs_kt:5.1f} kt"); vals["고도 MSL"].set(f"{s.alt_ft:6.0f} ft"); vals["고도 AGL"].set(f"{s.agl_ft:6.0f} ft")
            vals["승강률"].set(f"{s.vs_fpm:+6.0f} fpm"); vals["방위"].set(f"{s.hdg_deg:03.0f}°"); vals["피치 / 롤"].set(f"{s.pitch_deg:+.1f} / {s.roll_deg:+.1f}")
            vals["스로틀 / RPM"].set(f"{s.throttle:.2f} / {s.rpm:4.0f}"); vals["플랩"].set(f"{s.flap*30:.0f}°"); vals["접지"].set("예" if s.on_ground else "아니오")
            vals["시뮬 시각"].set(f"{s.t_sim:7.1f} s" + ("  ⏸" if s.paused else "")); vals["비행"].set(f"#{s.flight_id}")
            vals["목적지 · 거리"].set("—" if s.dest_dist_nm is None else f"{s.dest_id or '?'}  {s.dest_dist_nm:.1f} NM / {s.dest_brg_deg:03.0f}°")
            vals["남은 시간"].set("—" if not s.dest_ete_min else f"{s.dest_ete_min:.0f} min")
            vals["연료"].set("—" if s.fuel_gal is None else f"{s.fuel_gal:.1f} gal" + (f" ({s.fuel_gal / s.fuel_flow_gph * 60:.0f} min)" if s.fuel_flow_gph and s.fuel_flow_gph > 0.5 else ""))
            if s.wind_spd_kt is None:
                vals["바람 (측풍)"].set("—")
            else:
                import math; xw = s.wind_spd_kt * math.sin(math.radians((s.wind_dir_deg or 0) - s.hdg_deg))
                vals["바람 (측풍)"].set(f"{s.wind_dir_deg:03.0f}°/{s.wind_spd_kt:.0f} kt ({xw:+.0f})")
            phase_var.set(f"{ph}\n{PHASE_KO.get(ph, '')}"); phase_lbl.configure(bg=PHASE_COL.get(ph, "#eeeeee"), fg="white" if ph == "IMPACT" else "black")
            warn_txt.delete("1.0", "end")
            if st:
                for rid in st:
                    r = rule_by_id[rid]; ev = last_onset.get(rid)
                    warn_txt.insert("end", f"{rid} {r.name} ({r.severity})", r.severity); warn_txt.insert("end", f"  {ev.value if ev else ''}\n")
            else:
                warn_txt.insert("end", "없음")
        while not engine.transitions_q.empty():
            tr = engine.transitions_q.get(); log.insert("end", f"[t={tr.t_sim:7.1f}] 단계 {tr.from_phase} → {tr.to_phase}  {tr.note}\n", "PHASE"); log.see("end")
        while not engine.events_q.empty():
            e = engine.events_q.get(); log.insert("end", f"[t={e.t_sim:7.1f}] {e.rule_id} {e.name} {e.kind} ({e.severity}) {e.value}\n", e.severity); log.see("end")
            if auto_var.get() and e.kind == "ONSET" and e.severity == "WARNING":
                ask(f"{e.name} 경고가 발생했습니다. 지금 무엇을 해야 합니까?", auto=True)
        err = engine.error
        status_var.set(err if err else f"{'재생 ' + os.path.basename(a.replay) if a.replay else 'X-Plane ' + a.xp_host + ':' + str(a.xp_port)} | 수신 {engine.rows}행 | 이벤트 {len(engine.eng.events)} | CSV: {engine.csv_path or '저장 안 함'}")
        root.after(250, refresh)

    def on_close():
        engine.stop.set()
        try:
            engine.source.close()
        except Exception:
            pass
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh(); root.mainloop()


# ----------------------------------------------------------------------------- headless
def run_headless(engine):
    t = threading.Thread(target=engine.run, daemon=True); t.start()
    last_rows = -1
    while t.is_alive():
        time.sleep(0.2)
        while not engine.transitions_q.empty():
            tr = engine.transitions_q.get(); print(f"[t={tr.t_sim:7.1f}] 단계 {tr.from_phase} → {tr.to_phase}  {tr.note}")
        while not engine.events_q.empty():
            e = engine.events_q.get(); print(f"[t={e.t_sim:7.1f}] {e.rule_id} {e.name} {e.kind} ({e.severity}) {e.value}")
        if engine.error and engine.rows == last_rows:
            print(engine.error)
        last_rows = engine.rows
    snap = engine.snapshot()
    if snap:
        s, ph, st, *_ = snap; print(f"\n마지막 상태: {fmt_state(s)}\n단계 {ph}, 활성 경고 {st}, 수신 {engine.rows}행, 이벤트 {len(engine.eng.events)}건")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen2.5:7b-instruct"); ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--xp-host", default="127.0.0.1"); ap.add_argument("--xp-port", type=int, default=49000); ap.add_argument("--local-port", type=int, default=49005)
    ap.add_argument("--hz", type=int, default=10); ap.add_argument("--timeout", type=float, default=2.0)
    ap.add_argument("--replay", default=None, help="X-Plane 대신 저장 CSV 재생"); ap.add_argument("--speed", type=float, default=1.0, help="재생 배속 (0 = 최대)")
    ap.add_argument("--kb-dir", default=os.path.join(ROOT, "xp_kb", "kb")); ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--out", default=os.path.join(ROOT, "logs"), help="CSV 저장 폴더 (재생 모드에서는 저장 안 함)")
    ap.add_argument("--no-ui", action="store_true", help="화면 없이 판정 결과만 콘솔 출력")
    a = ap.parse_args()
    kb = KB(a.kb_dir)
    csv_path = None
    if not a.replay:
        os.makedirs(a.out, exist_ok=True); csv_path = os.path.join(a.out, dt.datetime.now().strftime("flight_%Y%m%d_%H%M%S.csv"))
    engine = Engine(make_source(a), csv_path)
    if a.no_ui:
        run_headless(engine); return
    threading.Thread(target=engine.run, daemon=True).start()
    run_ui(engine, kb, a)


if __name__ == "__main__":
    main()
