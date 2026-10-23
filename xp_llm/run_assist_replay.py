#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_assist_replay.py — STEP 6 (최소 범위): 저장된 비행 로그를 재생해 지정 시점의 Context Package 를 만들고
Ollama 로컬 LLM 에 질의해 응답을 저장한다. X-Plane 없이 동작.

폴더 구조 (기본값 기준)
  C:\\xp_collector\\logs\\flight_*.csv
  C:\\xp_collector\\xp_phase\\   (flight_state.py, phase_detector.py, rule_engine.py)
  C:\\xp_collector\\xp_kb\\kb\\chunks.jsonl, xp_kb\\kb_retriever.py
  C:\\xp_collector\\xp_llm\\run_assist_replay.py   ← 이 파일

실행 예
  py run_assist_replay.py --model qwen2.5:7b-instruct                 # 기본 4개 시점, 기본 질문 1개
  py run_assist_replay.py --model qwen2.5:14b-instruct --host http://192.168.0.10:11434
  py run_assist_replay.py --dry-run                                    # LLM 호출 없이 Context Package 만 확인
  py run_assist_replay.py --model qwen2.5:7b-instruct --q "착륙 전 체크리스트 알려줘" --q "지금 속도 괜찮아?"
  py run_assist_replay.py --model qwen2.5:7b-instruct --at flight_20260924_002110.csv:1100 --at flight_20260924_002110.csv:2100

출력: out\\assist_replay_YYYYMMDD_HHMMSS.md (사람이 읽는 결과), 같은 이름의 .jsonl (분석용)
"""
import argparse, datetime as dt, json, os, sys, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_AT = [  # (csv, sim_time)  — 이륙 활주 / 초기 상승 / 순항 / 최종 접근
    ("flight_20260923_234954.csv", 340.0),
    ("flight_20260923_234954.csv", 365.0),
    ("flight_20260924_002110.csv", 1100.0),
    ("flight_20260924_002110.csv", 2100.0),
]
DEFAULT_Q = ["지금 뭘 확인해야 해?"]

SYSTEM_PROMPT = (
    "당신은 Cessna 172S 조종사를 보조하는 비행 상황 인지 AI Assist입니다. 항공기를 직접 제어하지 않으며 조종사의 판단을 돕습니다.\n"
    "언어 규칙: 반드시 한국어로만 답하십시오. 중국어(한자)와 영어 문장은 금지합니다. 항공 용어(IAS, KIAS, RPM, AGL)와 POH 제목은 영어 그대로 써도 됩니다.\n"
    "제공된 [현재 비행 상태], [항법 · 연료 · 기상 · 계통], [비행단계], [활성 경고], [최근 이벤트], [판단 힌트], [규칙 답변], [POH 근거]만 사용하십시오. "
    "제공되지 않은 수치·속도·절차는 만들어내지 말고, 모르면 모른다고 하십시오.\n"
    "[규칙 답변]은 이미 조종사 화면에 표시되었습니다. 그 내용을 반복하지 말고, 그 결론의 이유·주의할 점·다음 행동을 한국어 1~3문장으로만 덧붙이십시오. "
    "숫자는 [판단 힌트]와 [현재 비행 상태]에 있는 값만 쓰고, POH 인용은 [POH 근거]에 나열된 제목·쪽수만 쓰십시오. "
    "항공기가 접지 상태(지상)면 공중 비행처럼 설명하지 마십시오.\n\n"
    "예시 1) 질문: 연료 충분해?  규칙 답변: 잔여 연료 43.4 gal, 약 262분 비행 가능. 목적지까지 14분 + 예비 30분 → 충분.\n"
    "좋은 보충: 예비 연료를 포함해도 여유가 크므로 현재 출력으로 계속 비행해도 됩니다. 착륙 전 연료 선택 밸브가 BOTH인지만 확인하십시오.\n"
    "예시 2) 질문: 착륙 전에 뭘 확인해야 해?  규칙 답변: 현재 단계 APPROACH, 해당 절차 POH §4 BEFORE LANDING p.92 (좌석·벨트, 연료 선택 BOTH, 혼합기 RICH, 착륙등 ON, 오토파일럿 OFF).\n"
    "좋은 보충: 접근속도 미달 경고(R03)가 켜져 있으니 플랩 30° 기준 60~70 KIAS를 먼저 회복한 뒤 체크리스트를 진행하십시오.\n\n"
    "다시 강조: 답변은 한국어 문장으로만 작성합니다."
)
KO_ONLY_SUFFIX = "\n\n[지시] 반드시 한국어로만 답하십시오. 중국어·영어 문장 금지."


def has_chinese(text: str) -> bool:
    import re
    return re.search(r"[\u4e00-\u9fff]", text or "") is not None


def load_modules(phase_dir, kb_dir_parent):
    sys.path.insert(0, phase_dir); sys.path.insert(0, kb_dir_parent)
    from flight_state import iter_states
    from phase_detector import PhaseDetector, GROUND_PHASES
    from rule_engine import RuleEngine, RULES
    from kb_retriever import KB
    return iter_states, PhaseDetector, GROUND_PHASES, RuleEngine, RULES, KB


def snapshot_at(csv_path, target_t, iter_states, PhaseDetector, GROUND_PHASES, RuleEngine):
    """로그를 처음부터 재생해 target_t(초) 에 도달한 시점의 상태·단계·status·최근 이벤트를 반환."""
    det, eng = PhaseDetector(), RuleEngine()
    n_tr = 0
    for s in iter_states(csv_path):
        ph = det.update(s)
        liftoff = touchdown = None
        if len(det.transitions) > n_tr:
            tr = det.transitions[-1]; n_tr = len(det.transitions)
            if tr.to_phase == "CLIMB" and (tr.from_phase in GROUND_PHASES or tr.from_phase == "LANDING_ROLL"):
                liftoff = det.edge_state or s
            if tr.to_phase in ("LANDING_ROLL", "IMPACT"):
                touchdown = det.edge_state or s
        eng.update(s, ph, liftoff, touchdown)
        if not s.paused and s.t_sim >= target_t:
            recent = [e for e in eng.events if e.flight_id == s.flight_id and e.kind == "ONSET" and s.t_sim - 60.0 <= e.t_sim <= s.t_sim]
            last_onset = {}
            for e in eng.events:
                if e.kind == "ONSET":
                    last_onset[e.rule_id] = e
            return s, ph, eng.status(), last_onset, recent[-5:]
    raise ValueError(f"{os.path.basename(csv_path)}: t={target_t}s 에 도달하지 못함 (로그 범위 밖)")


def fmt_state(s):
    return (f"t={s.t_sim:.1f} s (flight {s.flight_id}) | IAS {s.ias_kt:.0f} kt, GS {s.gs_kt:.0f} kt | 고도 {s.alt_ft:.0f} ft MSL, {s.agl_ft:.0f} ft AGL | "
            f"승강률 {s.vs_fpm:+.0f} fpm | 방위 {s.hdg_deg:03.0f}° | 피치 {s.pitch_deg:.1f}°, 롤 {s.roll_deg:.1f}° | "
            f"스로틀 {s.throttle:.2f}, RPM {s.rpm:.0f} | 플랩 {s.flap * 30:.0f}° | 접지 {'예' if s.on_ground else '아니오'}")


def fmt_extra(s):
    """항법·연료·기상·계통 — 로그에 있는 항목만 문장으로."""
    parts = []
    if s.dest_dist_nm is not None:
        parts.append(f"목적지 {s.dest_id or '(미설정)'}까지 {s.dest_dist_nm:.1f} NM, 방위 {s.dest_brg_deg:03.0f}°" + (f", 약 {s.dest_ete_min:.0f}분" if s.dest_ete_min else ""))
    if s.fuel_gal is not None:
        end = f" (현재 소모율 {s.fuel_flow_gph:.1f} gph → 약 {s.fuel_gal / s.fuel_flow_gph * 60:.0f}분 비행 가능)" if s.fuel_flow_gph and s.fuel_flow_gph > 0.5 else ""
        parts.append(f"잔여 연료 {s.fuel_gal:.1f} gal" + end)
    if s.wind_spd_kt is not None:
        import math
        xw = s.wind_spd_kt * math.sin(math.radians((s.wind_dir_deg or 0) - s.hdg_deg))
        parts.append(f"바람 {s.wind_dir_deg:03.0f}°/{s.wind_spd_kt:.0f} kt (측풍 성분 {xw:+.0f} kt)")
    if s.oil_temp_c is not None or s.bus_volts is not None:
        parts.append(f"오일 온도 {s.oil_temp_c:.0f}°C, 전압 {s.bus_volts:.1f} V" if s.oil_temp_c is not None and s.bus_volts is not None else "")
    return " | ".join(p for p in parts if p) or "정보 없음(이 로그에는 항법·연료·기상 항목이 없음)"


NUMERIC_WORDS = ("연료", "fuel", "바람", "측풍", "wind", "목적지", "거리", "남았", "얼마나", "도착", "시간", "출력", "rpm", "속도", "고도")


def pick_k(question: str, default_k: int = 3) -> int:
    """연료·거리·바람·속도 같은 수치 질문은 POH 근거를 1개만 붙여 잡음을 줄인다."""
    q = question.lower()
    return 1 if any(w in q for w in NUMERIC_WORDS) else default_k


def judgment_hints(s, phase):
    """규칙으로 미리 계산한 결론. LLM 은 이 문장을 근거로 답만 하면 된다."""
    import math
    h = []
    # 연료
    if s.fuel_gal is not None:
        endur = (s.fuel_gal / s.fuel_flow_gph * 60.0) if s.fuel_flow_gph and s.fuel_flow_gph > 0.5 else None
        need = (s.dest_ete_min or 0.0) + 30.0           # 목적지까지 + VFR 예비 30분
        if endur is None:
            h.append(f"연료: 잔량 {s.fuel_gal:.1f} gal (소모율 미측정, 지상/아이들). 8 gal 미만이면 부족.")
        elif s.dest_ete_min:
            verdict = "충분" if endur >= need * 1.2 else ("빠듯(여유 20% 미만)" if endur >= need else "부족")
            h.append(f"연료: 잔량 {s.fuel_gal:.1f} gal → 약 {endur:.0f}분 비행 가능, 목적지까지 {s.dest_ete_min:.0f}분 + 예비 30분 = {need:.0f}분 필요 → {verdict}.")
        else:
            h.append(f"연료: 잔량 {s.fuel_gal:.1f} gal → 약 {endur:.0f}분 비행 가능" + (" → 충분." if endur >= 60 else " → 60분 미만, 주의."))
    # 목적지
    if s.dest_dist_nm is not None:
        h.append(f"목적지: {s.dest_id or '(미설정)'}까지 {s.dest_dist_nm:.1f} NM, 방위 {s.dest_brg_deg:03.0f}°" + (f", 약 {s.dest_ete_min:.0f}분." if s.dest_ete_min else "."))
    # 바람
    if s.wind_spd_kt is not None:
        xw = s.wind_spd_kt * math.sin(math.radians((s.wind_dir_deg or 0) - s.hdg_deg))
        h.append(f"바람: {s.wind_dir_deg:03.0f}°/{s.wind_spd_kt:.0f} kt, 측풍 성분 {abs(xw):.0f} kt {'우측' if xw > 0 else '좌측'} → POH 최대 실증 측풍 15 kt 대비 " + ("초과, 주의." if abs(xw) > 15 else "이내."))
    # 속도 / 출력 (단계별)
    if phase == "APPROACH":
        lo, hi = (60, 70) if s.flap >= 0.9 else (65, 75)
        h.append(f"접근속도: IAS {s.ias_kt:.0f} kt, POH 권고 {lo}~{hi} KIAS(플랩 {s.flap*30:.0f}°) → " + ("미달." if s.ias_kt < lo else "초과." if s.ias_kt > hi else "범위 내."))
    elif phase == "CLIMB":
        h.append(f"상승속도: IAS {s.ias_kt:.0f} kt, POH enroute climb 70~85 KIAS → " + ("느림." if s.ias_kt < 70 else "빠름." if s.ias_kt > 85 else "범위 내."))
    elif phase == "CRUISE":
        h.append(f"순항 출력: {s.rpm:.0f} RPM, POH 2,100~2,700 RPM(75% 이하 권장 ≈ 2,300) → " + ("권장치보다 높음." if s.rpm > 2450 else "범위 내."))
    if s.flap > 0.05:
        vfe = 110 if s.flap <= 0.4 else 85
        h.append(f"플랩 {s.flap*30:.0f}° 상태, 제한속도 Vfe {vfe} KIAS, 현재 {s.ias_kt:.0f} kt → " + ("초과!" if s.ias_kt > vfe else "이내."))
    if s.on_ground:
        h.append("항공기는 지상에 있음(접지).")
    return h


INTENT_WORDS = {
    "fuel": ("연료", "fuel", "기름"), "destination": ("목적지", "거리", "남았", "도착", "얼마나 남", "destination", "distance"),
    "wind": ("바람", "측풍", "wind", "crosswind"), "speed": ("속도", "빠른", "느린", "speed", "kt", "airspeed", "실속"),
    "power": ("출력", "rpm", "스로틀", "throttle", "power"), "flap": ("플랩", "flap"),
    "checklist": ("체크리스트", "점검표", "checklist", "확인해야", "뭘 확인", "점검"), "procedure": ("절차", "어떻게 해", "어떻게 하", "procedure", "복행", "비상", "고장"),
}


def classify_intent(question: str) -> str:
    q = question.lower()
    for intent, words in INTENT_WORDS.items():
        if any(w in q for w in words):
            return intent
    return "general"


def rule_answer(intent, s, phase, status, last_onset, hits, RULES):
    """규칙으로 만든 정답 문장(즉시 표시용). LLM 없이도 맞는 답."""
    import math
    rule_by_id = {r.id: r for r in RULES}
    ko = {"GROUND_STOP": "지상 정지", "TAXI": "택시", "TAKEOFF_ROLL": "이륙 활주", "CLIMB": "상승", "CRUISE": "순항", "DESCENT": "강하", "APPROACH": "접근", "GO_AROUND": "복행", "LANDING_ROLL": "착륙 활주", "IMPACT": "충돌"}
    warn = "; ".join(f"{rid} {rule_by_id[rid].name}({rule_by_id[rid].severity})" + (f": {last_onset[rid].value}" if rid in last_onset else "") for rid in status) or "없음"
    proc = f"POH §{hits[0]['section']} {hits[0]['path']} p.{hits[0]['page']}" if hits else "해당 절차 없음"
    hints = judgment_hints(s, phase); H = {h.split(":")[0]: h for h in hints}
    if intent == "fuel":
        return H.get("연료", "이 비행에는 연료 정보가 없습니다(수집 항목에 연료가 포함된 로그가 필요).")
    if intent == "destination":
        return H.get("목적지", "GPS에 활성 목적지가 없거나 이 로그에 항법 정보가 없습니다.")
    if intent == "wind":
        return H.get("바람", "이 비행에는 바람 정보가 없습니다.")
    if intent == "speed":
        line = H.get("접근속도") or H.get("상승속도") or (f"현재 IAS {s.ias_kt:.0f} kt, GS {s.gs_kt:.0f} kt ({ko.get(phase, phase)} 단계).")
        return line + (f" 활성 경고: {warn}." if status else "")
    if intent == "power":
        return H.get("순항 출력", f"현재 스로틀 {s.throttle:.2f}, {s.rpm:.0f} RPM ({ko.get(phase, phase)} 단계).")
    if intent == "flap":
        base = next((h for h in hints if h.startswith("플랩")), f"플랩 {s.flap*30:.0f}° (올라간 상태). 전개 제한속도: 10°는 110 KIAS 이하, 10~30°는 85 KIAS 이하(POH Fig 2-1).")
        return base
    if intent in ("checklist", "procedure"):
        body = ""
        if hits and hits[0].get("kind") == "checklist":
            items = [l.strip() for l in hits[0]["text"].splitlines() if l.strip()][:8]
            body = " / ".join(items)
        return f"현재 단계 {phase}({ko.get(phase, '')}) 해당 절차: {proc}." + (f" 항목: {body}" if body else "") + (f" 활성 경고: {warn}." if status else "")
    # general / status
    return (f"현재 {ko.get(phase, phase)}({phase}) 단계, IAS {s.ias_kt:.0f} kt, 고도 {s.agl_ft:.0f} ft AGL, 승강률 {s.vs_fpm:+.0f} fpm, 플랩 {s.flap*30:.0f}°. "
            f"활성 경고: {warn}. 참고 절차: {proc}.")


def verify_answer(text, ctx, hits, s):
    """LLM 응답 자동 검사: 인용 쪽수·절이 근거 안에 있는가, 숫자가 컨텍스트에 있는 값인가, 지상/공중 모순은 없는가."""
    import re
    issues = []
    pages = {str(h["page"]) for h in hits}; secs = {str(h["section"]) for h in hits}
    for pg in re.findall(r"p\.?\s?(\d{1,3})", text):
        if pg not in pages:
            issues.append(f"근거에 없는 쪽수 인용 p.{pg}")
    for sc in re.findall(r"§\s?(\d)", text):
        if sc not in secs:
            issues.append(f"근거에 없는 절 인용 §{sc}")
    ctx_nums = set(re.findall(r"\d+(?:[.,]\d+)?", ctx.replace(",", "")))
    for n in re.findall(r"\d+(?:\.\d+)?", text.replace(",", "")):
        if len(n.replace(".", "")) >= 2 and n not in ctx_nums and n.rstrip("0").rstrip(".") not in ctx_nums:
            issues.append(f"컨텍스트에 없는 숫자 {n}")
    if s.on_ground and any(w in text for w in ("비행 중", "상승 중", "순항 중", "접근 중")):
        issues.append("지상 상태인데 비행 중으로 서술")
    return (len(issues) == 0), issues[:5]


def stream_ollama(host, model, system, user, on_token, timeout=300, num_predict=320):
    """스트리밍 호출: 토큰이 올 때마다 on_token(text) 호출. 전체 문자열 반환."""
    body = {"model": model, "stream": True, "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": num_predict},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(host.rstrip("/") + "/api/chat", data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"})
    out = []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for line in r:
            if not line.strip():
                continue
            d = json.loads(line.decode("utf-8"))
            tok = d.get("message", {}).get("content", "")
            if tok:
                out.append(tok); on_token(tok)
            if d.get("done"):
                break
    return "".join(out).strip()


def build_context(s, phase, status, last_onset, recent, hits, question, RULES, KB):
    rule_by_id = {r.id: r for r in RULES}
    lines = ["[현재 비행 상태]", fmt_state(s), "", "[항법 · 연료 · 기상 · 계통]", fmt_extra(s), "", f"[비행단계] {phase}", "", "[활성 경고(status)]"]
    if status:
        for rid in status:
            r = rule_by_id[rid]; ev = last_onset.get(rid)
            lines.append(f"- {rid} {r.name} ({r.severity}): {ev.value if ev else ''} — 근거: {r.source}")
    else:
        lines.append("- 없음")
    lines += ["", "[최근 이벤트 60 s]"]
    lines += [f"- t={e.t_sim:.0f} s {e.rule_id} {e.name} {e.kind} ({e.severity}) {e.value}" for e in recent] or ["- 없음"]
    hints = judgment_hints(s, phase)
    lines += ["", "[판단 힌트 (규칙 계산값)]"] + ([f"- {x}" for x in hints] or ["- 없음"])
    lines += ["", "[규칙 답변 (화면에 이미 표시됨)]", rule_answer(classify_intent(question), s, phase, status, last_onset, hits, RULES)]
    lines += ["", "[POH 근거]", KB.format_context(hits, max_chars=2000), "", f"[조종사 질의] {question}"]
    return "\n".join(lines)


def ask_ollama(host, model, system, user, timeout=300):
    body = {"model": model, "stream": False, "options": {"temperature": 0.2, "num_ctx": 8192, "num_predict": 320},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(host.rstrip("/") + "/api/chat", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["message"]["content"].strip(), data.get("eval_count"), data.get("total_duration")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen2.5:7b-instruct", help="ollama list 에 보이는 모델 이름")
    ap.add_argument("--host", default="http://localhost:11434", help="Ollama 주소 (서버면 http://IP:11434)")
    ap.add_argument("--logs", default=os.path.join(ROOT, "logs"))
    ap.add_argument("--phase-dir", default=os.path.join(ROOT, "xp_phase"))
    ap.add_argument("--kb-dir", default=os.path.join(ROOT, "xp_kb", "kb"))
    ap.add_argument("--at", action="append", help="csv파일명:시뮬시각  (여러 번 지정 가능). 생략 시 기본 4개 시점")
    ap.add_argument("--q", action="append", help="조종사 질의 (여러 번 지정 가능)")
    ap.add_argument("--k", type=int, default=3, help="검색 근거 개수")
    ap.add_argument("--dry-run", action="store_true", help="LLM 호출 없이 Context Package 만 저장")
    ap.add_argument("--out", default=os.path.join(HERE, "out"))
    a = ap.parse_args()

    iter_states, PhaseDetector, GROUND_PHASES, RuleEngine, RULES, KB = load_modules(a.phase_dir, os.path.dirname(a.kb_dir))
    kb = KB(a.kb_dir)
    ats = [(x.rsplit(":", 1)[0], float(x.rsplit(":", 1)[1])) for x in a.at] if a.at else DEFAULT_AT
    qs = a.q or DEFAULT_Q
    os.makedirs(a.out, exist_ok=True)
    stem = dt.datetime.now().strftime("assist_replay_%Y%m%d_%H%M%S")
    md_path, jl_path = os.path.join(a.out, stem + ".md"), os.path.join(a.out, stem + ".jsonl")
    md = [f"# AI Assist 재생 결과 — {dt.datetime.now():%Y-%m-%d %H:%M}", f"모델: {'(dry-run)' if a.dry_run else a.model} @ {a.host}", ""]
    n = 0
    with open(jl_path, "w", encoding="utf-8") as jl:
        for csv_name, t in ats:
            csv_path = csv_name if os.path.isabs(csv_name) else os.path.join(a.logs, csv_name)
            try:
                s, phase, status, last_onset, recent = snapshot_at(csv_path, t, iter_states, PhaseDetector, GROUND_PHASES, RuleEngine)
            except Exception as e:
                print("[skip]", e); md.append(f"## {csv_name} t={t:.0f}s — 건너뜀: {e}\n"); continue
            for q in qs:
                n += 1
                hits = kb.retrieve(q, phase=phase, k=pick_k(q, a.k))
                ctx = build_context(s, phase, status, last_onset, recent, hits, q, RULES, KB)
                print(f"\n===== [{n}] {csv_name} t={s.t_sim:.1f}s phase={phase} status={status} q={q!r}")
                print(f"  근거: " + "; ".join(f"§{h['section']} {h['path'][:40]} p.{h['page']}" for h in hits))
                if a.dry_run:
                    resp, meta = "(dry-run: LLM 호출 생략)", {}
                else:
                    try:
                        resp, ev, dur = ask_ollama(a.host, a.model, SYSTEM_PROMPT, ctx)
                        if has_chinese(resp):
                            resp, ev, dur = ask_ollama(a.host, a.model, SYSTEM_PROMPT, ctx + KO_ONLY_SUFFIX)
                        meta = {"eval_count": ev, "total_duration_s": round((dur or 0) / 1e9, 1)}
                    except urllib.error.URLError as e:
                        print(f"  [오류] Ollama 접속 실패: {e}. Ollama 실행 여부, --host, 모델 이름(ollama list)을 확인하십시오.")
                        resp, meta = f"(오류: {e})", {}
                print("  응답:", resp.replace("\n", "\n        "))
                ra = rule_answer(classify_intent(q), s, phase, status, last_onset, hits, RULES)
                ok, issues = verify_answer(resp, ctx, hits, s) if not a.dry_run else (True, [])
                md += [f"## [{n}] {csv_name}  t={s.t_sim:.0f} s  —  단계 {phase}  —  질의: {q}", "",
                       "**상태**: " + fmt_state(s), "", "**활성 경고**: " + (", ".join(status) if status else "없음"), "",
                       "**검색 근거**: " + "; ".join(f"POH §{h['section']} {h['path']} p.{h['page']}" for h in hits), "",
                       "**규칙 답변(즉시)**: " + ra, "", "**LLM 보충 설명**", "", resp, "",
                       "**자동 검증**: " + ("통과" if ok else "주의 — " + "; ".join(issues)), "",
                       "<details><summary>Context Package 전문</summary>", "", "```", ctx, "```", "</details>", ""]
                jl.write(json.dumps({"n": n, "csv": csv_name, "t": s.t_sim, "flight_id": s.flight_id, "phase": phase, "status": status,
                                     "question": q, "evidence": [{"id": h["id"], "path": h["path"], "page": h["page"]} for h in hits],
                                     "context": ctx, "response": resp, "meta": meta}, ensure_ascii=False) + "\n")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md))
    print(f"\n저장: {md_path}\n      {jl_path}")


if __name__ == "__main__":
    main()
