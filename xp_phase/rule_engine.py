# -*- coding: utf-8 -*-
"""
rule_engine.py — STEP 4: Rule-based Situation Logic v0 (deterministic, LLM 미사용)

입력: FlightState + phase (STEP 2·3 출력)
출력: 이벤트(ONSET/CLEAR) 목록 + 현재 활성 규칙 집합(status)

구조 (제안서 "event vs status 이중 처리")
  · 각 규칙은 조건이 dwell_s 이상 지속되면 ONSET 이벤트, clear_s 이상 해소되면 CLEAR 이벤트를 낸다.
  · status = 현재 ONSET 상태인 규칙 집합. LLM Context 에는 이벤트(변화)와 status(현재) 둘 다 전달한다.
  · 규칙은 RULES 표에 선언적으로 정의하고, 각 규칙에 근거(source)와 검증 필요 여부(verify)를 기록한다.

기준값 (C172S) — POH(Cessna 172S Information Manual, Rev.5 2004) 원문으로 확인 완료(2026-09-24).
ACS 허용오차(±100 ft, ±10°, +10/-5 kt)는 FAA Private Pilot ACS 원문 확인이 남아 있음(verify 표시).
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
from flight_state import FlightState

# ----------------------------------------------------------------------------
# 기준 속도/허용오차 (KIAS) — Cessna 172S Information Manual (Rev.5, 2004) 원문으로 확인 완료
#   page = 업로드 PDF(POH-Cessna-172S.pdf) 기준 쪽수, § = 매뉴얼 절 번호
# ----------------------------------------------------------------------------
SPEEDS = {
    "Vs1_flaps_up": 48.0,     # 실속, 플랩 업, 2550 lb, 수평      Fig 5-3 (p.124)  ※ "KIAS values are approximate"
    "Vs_flaps_10": 43.0,      # 실속, 플랩 10° (전방 CG 43 / 후방 42 → 보수적으로 43)  Fig 5-3 (p.124)
    "Vs0_flaps_30": 40.0,     # 실속, 플랩 30°                     Fig 5-3 (p.124)
    "Vr_normal": 55.0,        # Normal takeoff: lift nose wheel at 55 KIAS   §4 checklist (p.91)
    "climb_out_lo": 70.0, "climb_out_hi": 80.0,        # Normal takeoff climb speed 70-80        §4 (p.91)
    "short_field_climb": 56.0,                          # Short field: 56 KIAS until obstacles    §4 (p.92)
    "flap_retract_min": 60.0,                           # 플랩 회수 최저속도                        §4 (p.92, p.101)
    "Vx": 62.0, "Vy": 74.0,                             # SL                                       §4 (p.81)
    "enroute_climb_lo": 70.0, "enroute_climb_hi": 85.0, # Enroute climb 70-85                      §4 checklist (p.92)
    "app_flaps_up_lo": 65.0, "app_flaps_up_hi": 75.0,   # Normal approach, flaps up 65-75          §4 (p.81, p.93)
    "app_flaps_30_lo": 60.0, "app_flaps_30_hi": 70.0,   # Normal approach, flaps 30° 60-70         §4 (p.81, p.93)
    "short_field_app": 61.0,                            # Short field approach, flaps 30°          §4 (p.81, p.93)
    "balked_climb": 60.0,                               # Balked landing: full power, flaps 20°, 60 KIAS  §4 (p.81, p.93)
    "balked_flap_retract": 65.0,                        # 복행 후 플랩 완전 회수 속도               §4 (p.93)
    "Vfe_10": 110.0, "Vfe_10_30": 85.0,                 # Fig 2-1 (p.40)
    "Va_2550": 105.0, "Vno": 129.0, "Vne": 163.0,       # Fig 2-1 (p.40)
    "eng_fail_after_to_flaps_up": 70.0, "eng_fail_after_to_flaps_dn": 65.0, "max_glide": 68.0,  # §3 (p.55)
    "static_rpm_lo": 2300.0, "static_rpm_hi": 2400.0,   # 풀스로틀 정지 RPM 범위                    §2 (p.41)
    "max_rpm": 2700.0,                                  # §2 (p.41)
}
ACS = {
    "alt_tol_ft": 100.0, "hdg_tol_deg": 10.0, "ias_tol_kt": 10.0,   # Private Pilot ACS 일반 허용오차 (ACS 원문 확인 필요)
    "climb_ias_plus": 10.0, "climb_ias_minus": 5.0,                  # Vy +10/-5             (ACS 원문 확인 필요)
    "app_ias_plus": 10.0, "app_ias_minus": 5.0,                      # 접근속도 +10/-5        (ACS 원문 확인 필요)
    "pattern_bank_max_deg": 30.0,                                    # 장주 내 경사 한계 (관행/설계값)
}
STAB_GATE_AGL_FT = 500.0        # 안정화 접근 판정 고도 (설계값)
CRUISE_SETTLE_S = 15.0          # CRUISE 진입 후 이 시간이 지난 시점의 고도/방위를 기준값으로 잡음 (설계값)
LOW_ALT_FT = 1000.0             # "저고도" 규칙 적용 고도 (설계값)
PHASE_IN_AIR = {"CLIMB", "CRUISE", "DESCENT", "APPROACH", "GO_AROUND"}


def crosswind_kt(s) -> float:
    """측풍 성분(kt). 바람은 진북, 기수는 자북 기준이라 편차(수 도)는 무시."""
    import math
    if s.wind_spd_kt is None or s.wind_dir_deg is None:
        return 0.0
    return s.wind_spd_kt * math.sin(math.radians(s.wind_dir_deg - s.hdg_deg))


def stall_speed(flap: float) -> float:
    """플랩 비율(0=업, 0.33=10°, 1.0=30°)에 따른 실속속도. Fig 5-3 의 세 점을 구간 선형 보간."""
    f = max(0.0, min(1.0, flap))
    if f <= 0.33:
        return SPEEDS["Vs1_flaps_up"] + (SPEEDS["Vs_flaps_10"] - SPEEDS["Vs1_flaps_up"]) * (f / 0.33)
    return SPEEDS["Vs_flaps_10"] + (SPEEDS["Vs0_flaps_30"] - SPEEDS["Vs_flaps_10"]) * ((f - 0.33) / 0.67)


def approach_band(flap: float):
    """정상 접근속도 밴드 ± ACS 허용오차.
    POH 는 플랩 업(65-75)과 플랩 30°(60-70)만 제시 → 중간 플랩은 두 밴드 사이를 선형 보간(설계 판단)."""
    f = max(0.0, min(1.0, flap))
    lo = SPEEDS["app_flaps_up_lo"] + (SPEEDS["app_flaps_30_lo"] - SPEEDS["app_flaps_up_lo"]) * f
    hi = SPEEDS["app_flaps_up_hi"] + (SPEEDS["app_flaps_30_hi"] - SPEEDS["app_flaps_up_hi"]) * f
    return lo - ACS["app_ias_minus"], hi + ACS["app_ias_plus"]


@dataclass
class Ctx:
    """규칙 평가에 필요한 파생 정보 (엔진이 매 샘플 갱신)."""
    phase: str
    prev_phase: Optional[str]
    throttle_drop_1s: float          # 최근 1 s 스로틀 감소량
    cruise_ref_alt: Optional[float]  # CRUISE 진입 시 고도
    cruise_ref_hdg: Optional[float]  # CRUISE 진입 시 방위
    liftoff: Optional[FlightState]   # 이번 샘플에서 부양이 확정됐으면 그 순간 상태
    touchdown: Optional[FlightState]
    gate_500: bool                   # 이번 샘플이 APPROACH 중 500 ft AGL 첫 통과인가


@dataclass
class Rule:
    id: str
    name: str
    severity: str                    # INFO / CAUTION / WARNING
    cond: Callable[[FlightState, Ctx], bool]
    value: Callable[[FlightState, Ctx], str]
    source: str
    verify: bool = True
    dwell_s: float = 1.0
    clear_s: float = 2.0
    one_shot: bool = False           # 전이형 이벤트(부양·접지·복행): ONSET 만 내고 CLEAR 없음


def _hdg_diff(a, b):
    d = (a - b + 180.0) % 360.0 - 180.0
    return abs(d)


RULES: List[Rule] = [
    Rule("R01", "AIRSPEED_LOW_AIRBORNE", "WARNING",
         lambda s, c: c.phase in PHASE_IN_AIR and s.ias_kt < 1.1 * stall_speed(s.flap),
         lambda s, c: f"ias={s.ias_kt:.0f} < 1.1*Vs({stall_speed(s.flap):.0f}) flap={s.flap:.2f}",
         "POH Fig 5-3 실속속도(p.124) ×1.1", verify=False, dwell_s=1.0),
    Rule("R02", "APPROACH_SPEED_HIGH", "CAUTION",
         lambda s, c: c.phase == "APPROACH" and s.flap > 0.2 and s.ias_kt > approach_band(s.flap)[1],
         lambda s, c: f"ias={s.ias_kt:.0f} > {approach_band(s.flap)[1]:.0f} flap={s.flap:.2f}",
         "POH §4 접근속도 65-75/60-70(p.81,93) + ACS +10 kt", verify=False, dwell_s=3.0),
    Rule("R03", "APPROACH_SPEED_LOW", "CAUTION",
         lambda s, c: c.phase == "APPROACH" and s.flap > 0.2 and s.ias_kt < approach_band(s.flap)[0],
         lambda s, c: f"ias={s.ias_kt:.0f} < {approach_band(s.flap)[0]:.0f} flap={s.flap:.2f}",
         "POH §4 접근속도 65-75/60-70(p.81,93) - ACS 5 kt", verify=False, dwell_s=3.0),
    Rule("R04", "FLAP_OVERSPEED", "WARNING",
         lambda s, c: (s.flap > 0.4 and s.ias_kt > SPEEDS["Vfe_10_30"]) or (0.05 < s.flap <= 0.4 and s.ias_kt > SPEEDS["Vfe_10"]),
         lambda s, c: f"ias={s.ias_kt:.0f} flap={s.flap:.2f}",
         "POH Fig 2-1 Vfe 110/85 (p.40)", verify=False, dwell_s=0.5),
    Rule("R05", "SINK_RATE_HIGH_LOW_ALT", "WARNING",
         lambda s, c: c.phase in PHASE_IN_AIR and s.agl_ft < LOW_ALT_FT and s.vs_fpm < -1000.0,
         lambda s, c: f"vs={s.vs_fpm:.0f} agl={s.agl_ft:.0f}",
         "안정화 접근 기준(강하율 1,000 fpm) — 설계값", verify=False, dwell_s=1.0),
    Rule("R06", "BANK_HIGH_LOW_ALT", "CAUTION",
         lambda s, c: c.phase in PHASE_IN_AIR and s.agl_ft < LOW_ALT_FT and abs(s.roll_deg) > ACS["pattern_bank_max_deg"],
         lambda s, c: f"roll={s.roll_deg:.0f} agl={s.agl_ft:.0f}",
         "장주 내 경사 30° 한계 — 관행/설계값", verify=False, dwell_s=1.0),
    Rule("R07", "POWER_LOSS_LOW_ALT", "WARNING",
         lambda s, c: c.phase in ("CLIMB", "GO_AROUND") and s.agl_ft < LOW_ALT_FT and c.throttle_drop_1s > 0.5,
         lambda s, c: f"throttle drop {c.throttle_drop_1s:.2f}/1s agl={s.agl_ft:.0f}",
         "상승 중 급격한 출력 감소 — 설계값(2026-09-23 로그 2건)", verify=False, dwell_s=0.0, clear_s=5.0),
    Rule("R08", "EARLY_ROTATION", "CAUTION",
         lambda s, c: c.liftoff is not None and c.liftoff.ias_kt < ((1.1 * stall_speed(c.liftoff.flap)) if c.liftoff.flap > 0.2 else (SPEEDS["Vr_normal"] - 5.0)),
         lambda s, c: f"liftoff ias={c.liftoff.ias_kt:.0f} flap={c.liftoff.flap:.2f}",
         "POH §4 Normal takeoff 55 KIAS(p.91) - 5 kt; 플랩 10° 는 1.1×Vs(설계)", verify=False, dwell_s=0.0, one_shot=True),
    Rule("R09", "CLIMB_SPEED_OFF_VY", "INFO",
         lambda s, c: c.phase == "CLIMB" and s.agl_ft > 300.0 and not (SPEEDS["enroute_climb_lo"] - ACS["climb_ias_minus"] <= s.ias_kt <= SPEEDS["enroute_climb_hi"] + ACS["climb_ias_plus"]),
         lambda s, c: f"ias={s.ias_kt:.0f} (band {SPEEDS['enroute_climb_lo'] - ACS['climb_ias_minus']:.0f}~{SPEEDS['enroute_climb_hi'] + ACS['climb_ias_plus']:.0f})",
         "POH §4 Vy 74 / enroute climb 70-85(p.81,92) + ACS +10/-5", verify=False, dwell_s=10.0, clear_s=5.0),
    Rule("R10", "CRUISE_ALT_DEVIATION", "INFO",
         lambda s, c: c.phase == "CRUISE" and c.cruise_ref_alt is not None and abs(s.alt_ft - c.cruise_ref_alt) > ACS["alt_tol_ft"],
         lambda s, c: f"alt={s.alt_ft:.0f} ref={c.cruise_ref_alt:.0f}",
         "ACS 고도 ±100 ft (verify)", dwell_s=5.0),
    Rule("R11", "CRUISE_HDG_DEVIATION", "INFO",
         lambda s, c: c.phase == "CRUISE" and c.cruise_ref_hdg is not None and _hdg_diff(s.hdg_deg, c.cruise_ref_hdg) > ACS["hdg_tol_deg"],
         lambda s, c: f"hdg={s.hdg_deg:.0f} ref={c.cruise_ref_hdg:.0f}",
         "ACS 방위 ±10° (verify)", dwell_s=10.0),
    Rule("R12", "GO_AROUND", "INFO",
         lambda s, c: c.phase == "GO_AROUND" and c.prev_phase != "GO_AROUND",
         lambda s, c: f"agl={s.agl_ft:.0f} ias={s.ias_kt:.0f}",
         "POH §4 Balked Landing(p.93): 풀스로틀·플랩 20°·60 KIAS", verify=False, dwell_s=0.0, one_shot=True),
    Rule("R13", "HARD_TOUCHDOWN", "WARNING",
         lambda s, c: c.touchdown is not None and c.touchdown.vs_fpm < -500.0,
         lambda s, c: f"touchdown vs={c.touchdown.vs_fpm:.0f}",
         "접지 강하율 500 fpm — 설계값", verify=False, dwell_s=0.0, one_shot=True),
    Rule("R14", "TOUCHDOWN_SPEED_HIGH", "CAUTION",
         lambda s, c: c.touchdown is not None and c.touchdown.ias_kt > SPEEDS["app_flaps_30_lo"] + 5.0,
         lambda s, c: f"touchdown ias={c.touchdown.ias_kt:.0f}",
         "POH §4 접근속도 하한 60(p.93) + 5 kt — 설계값", verify=False, dwell_s=0.0, one_shot=True),
    Rule("R15", "UNSTABILIZED_AT_500FT", "CAUTION",
         lambda s, c: c.gate_500 and not (approach_band(s.flap)[0] <= s.ias_kt <= approach_band(s.flap)[1] and s.vs_fpm > -1000.0 and s.flap >= 0.3 and abs(s.roll_deg) <= 15.0),
         lambda s, c: f"ias={s.ias_kt:.0f} vs={s.vs_fpm:.0f} flap={s.flap:.2f} roll={s.roll_deg:.0f}",
         "안정화 접근 게이트(500 ft AGL) — 설계값 + POH §4 접근속도", verify=False, dwell_s=0.0, one_shot=True),
    Rule("R16", "GO_AROUND_FLAPS_NOT_RETRACTED", "CAUTION",
         lambda s, c: c.phase == "GO_AROUND" and s.flap > 0.75,
         lambda s, c: f"flap={s.flap:.2f} (POH: 즉시 20°로 회수) ias={s.ias_kt:.0f}",
         "POH §4 Balked Landing step 2 (p.93, p.109)", verify=False, dwell_s=5.0, clear_s=2.0),
    Rule("R18", "FUEL_LOW", "CAUTION",
         lambda s, c: s.fuel_gal is not None and s.fuel_gal < 8.0,
         lambda s, c: f"fuel={s.fuel_gal:.1f} gal" + (f" ({s.fuel_gal / s.fuel_flow_gph * 60:.0f} min at {s.fuel_flow_gph:.1f} gph)" if s.fuel_flow_gph and s.fuel_flow_gph > 0.5 else ""),
         "잔여 연료 8 gal 미만(VFR 예비 30분 + 여유) — 설계값", verify=False, dwell_s=5.0, clear_s=5.0),
    Rule("R19", "CROSSWIND_HIGH", "CAUTION",
         lambda s, c: c.phase in ("TAKEOFF_ROLL", "APPROACH", "LANDING_ROLL", "GO_AROUND") and s.wind_spd_kt is not None and abs(crosswind_kt(s)) > 15.0,
         lambda s, c: f"crosswind={crosswind_kt(s):+.0f} kt (wind {s.wind_dir_deg:.0f}°/{s.wind_spd_kt:.0f} kt)",
         "POH §4 max demonstrated crosswind 15 kt (p.81)", verify=False, dwell_s=3.0, clear_s=5.0),
    Rule("R20", "BUS_VOLTS_LOW", "CAUTION",
         lambda s, c: s.bus_volts is not None and s.rpm > 1000 and s.bus_volts < 24.5,
         lambda s, c: f"bus={s.bus_volts:.1f} V",
         "28 V 계통 저전압(LOW VOLTS) — 설계값", verify=False, dwell_s=10.0, clear_s=5.0),
    Rule("R17", "TAKEOFF_POWER_LOW", "CAUTION",
         lambda s, c: c.phase == "TAKEOFF_ROLL" and s.throttle >= 0.95 and s.rpm < SPEEDS["static_rpm_lo"],
         lambda s, c: f"rpm={s.rpm:.0f} < {SPEEDS['static_rpm_lo']:.0f} at full throttle",
         "POH §2 static RPM 2300-2400 (p.41)", verify=False, dwell_s=3.0, clear_s=2.0),
]


@dataclass
class Event:
    flight_id: int
    t_sim: float
    seq: int
    phase: str
    rule_id: str
    name: str
    kind: str          # ONSET / CLEAR
    severity: str
    value: str
    source: str


class RuleEngine:
    def __init__(self, rules: Optional[List[Rule]] = None):
        self.rules = rules or RULES
        self.reset()

    def reset(self):
        self.flight_id = None
        self.prev_phase = None
        self.active: Dict[str, float] = {}       # rule_id → ONSET 시각
        self.cand_since: Dict[str, float] = {}   # 조건 성립 시작 시각
        self.false_since: Dict[str, float] = {}  # 조건 해소 시작 시각
        self.thr_hist = []                        # (t, throttle)
        self.cruise_ref_alt = self.cruise_ref_hdg = None
        self.cruise_since = None
        self.gate_done = False
        self.was_airborne = False
        self.events: List[Event] = []

    def _ctx(self, s: FlightState, phase: str, liftoff, touchdown) -> Ctx:
        self.thr_hist.append((s.t_sim, s.throttle))
        while self.thr_hist and s.t_sim - self.thr_hist[0][0] > 1.0:
            self.thr_hist.pop(0)
        drop = max(v for _, v in self.thr_hist) - s.throttle if self.thr_hist else 0.0
        if phase == "CRUISE":
            if self.prev_phase != "CRUISE":
                self.cruise_since, self.cruise_ref_alt, self.cruise_ref_hdg = s.t_sim, None, None
            elif self.cruise_ref_alt is None and s.t_sim - self.cruise_since >= CRUISE_SETTLE_S:
                self.cruise_ref_alt, self.cruise_ref_hdg = s.alt_ft, s.hdg_deg   # 수평 안정 후 기준값 확정
        else:
            self.cruise_since = None
        if phase == "APPROACH" and self.prev_phase != "APPROACH":
            self.gate_done = False
        gate = False
        if phase == "APPROACH" and not self.gate_done and s.agl_ft <= STAB_GATE_AGL_FT and s.vs_fpm < 0:
            gate, self.gate_done = True, True
        return Ctx(phase, self.prev_phase, drop, self.cruise_ref_alt, self.cruise_ref_hdg, liftoff, touchdown, gate)

    def update(self, s: FlightState, phase: str, liftoff: Optional[FlightState] = None,
               touchdown: Optional[FlightState] = None) -> List[Event]:
        """한 샘플 평가. liftoff/touchdown 은 이번 샘플에서 확정된 전이가 있을 때만 전달."""
        if self.flight_id != s.flight_id:
            ev = self.events; self.reset(); self.events = ev; self.flight_id = s.flight_id
        if s.paused:
            return []
        c = self._ctx(s, phase, liftoff, touchdown)
        new: List[Event] = []
        for r in self.rules:
            try:
                hit = bool(r.cond(s, c))
            except Exception:
                hit = False
            if r.one_shot:
                if hit:
                    new.append(Event(s.flight_id, s.t_sim, s.seq, phase, r.id, r.name, "ONSET", r.severity, r.value(s, c), r.source))
                continue
            if hit:
                self.false_since.pop(r.id, None)
                self.cand_since.setdefault(r.id, s.t_sim)
                if r.id not in self.active and s.t_sim - self.cand_since[r.id] >= r.dwell_s:
                    self.active[r.id] = s.t_sim
                    new.append(Event(s.flight_id, s.t_sim, s.seq, phase, r.id, r.name, "ONSET", r.severity, r.value(s, c), r.source))
            else:
                self.cand_since.pop(r.id, None)
                if r.id in self.active:
                    self.false_since.setdefault(r.id, s.t_sim)
                    if s.t_sim - self.false_since[r.id] >= r.clear_s:
                        del self.active[r.id]; self.false_since.pop(r.id, None)
                        new.append(Event(s.flight_id, s.t_sim, s.seq, phase, r.id, r.name, "CLEAR", r.severity, "", r.source))
        self.prev_phase = phase
        self.events.extend(new)
        return new

    def status(self) -> List[str]:
        return sorted(self.active.keys())
