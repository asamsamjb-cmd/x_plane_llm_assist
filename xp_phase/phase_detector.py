# -*- coding: utf-8 -*-
"""
phase_detector.py — STEP 3: Flight Phase Detector v0 (deterministic, LLM 미사용)

입력: FlightState 시퀀스 (flight_state.iter_states)
출력: 각 샘플의 phase 문자열 + 전이 기록

단계 (v0)
  GROUND_STOP   지상 정지
  TAXI          지상 이동 (GS 2~35 kt, 스로틀 < 0.8)
  TAKEOFF_ROLL  이륙 활주 (지상, 스로틀 ≥ 0.8, GS ≥ 2 kt)
  CLIMB         상승 (공중, VS > +300 fpm)
  CRUISE        순항 (공중, |VS| ≤ 300 fpm, 8 s 유지)
  DESCENT       강하 (공중, VS < -300 fpm, AGL ≥ 1,000 ft)
  APPROACH      접근 (공중, AGL < 1,000 ft, 강하 또는 접근 중 수평)
  GO_AROUND     복행 (APPROACH 중 스로틀 ≥ 0.5 & VS > +100 → AGL 500 ft 또는 30 s 까지)
  LANDING_ROLL  착륙 활주 (공중 → 지상 확정 후, GS 25 kt 미만이 될 때까지)
  IMPACT        충돌 (접지 시 VS < -1,000 fpm 또는 |롤| > 60°; 리셋까지 유지)

설계 원칙
  · 지상/공중 판정은 on_ground 가 1.0 s 이상 유지될 때만 확정 (바운스 필터)
  · 각 단계는 후보 조건이 dwell 시간 동안 지속되어야 전이 (떨림 방지)
  · 지상 단계는 GS 기준 (IAS 는 저속에서 0 근처로 읽힘)
  · VS 는 1 s 이동평균 사용
임계값은 Thresholds 에 모아 두었고, 2026-09-23/24 X-Plane 로그 관측값과 C172 운용 특성으로 정했다.
POH/ACS 기준의 정식 임계값은 STEP 4 Rule Engine 에서 별도 정의한다.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from collections import deque
from flight_state import FlightState

GROUND_PHASES = {"GROUND_STOP", "TAXI", "TAKEOFF_ROLL"}
AIR_PHASES = {"CLIMB", "CRUISE", "DESCENT", "APPROACH", "GO_AROUND"}


@dataclass
class Thresholds:
    gs_stop_kt: float = 2.0          # 이하면 정지
    taxi_max_gs_kt: float = 35.0     # 초과하면 지상에서도 활주로 간주
    landing_roll_exit_gs_kt: float = 25.0  # 접지 후 GS 가 이 미만이면 TAXI (2026-09-24 AI 착륙: 55→25 kt, 11 s, 206 m)
    throttle_takeoff: float = 0.80   # 이상이면 이륙 출력
    throttle_go_around: float = 0.50 # APPROACH 중 이 이상 + 상승이면 복행
    vs_climb_fpm: float = 300.0
    vs_descent_fpm: float = -300.0
    approach_agl_ft: float = 1000.0  # 미만에서 강하하면 APPROACH
    go_around_exit_agl_ft: float = 500.0
    go_around_max_s: float = 30.0    # 복행 단계 최대 지속시간(초과 시 CLIMB)
    go_around_vs_fpm: float = 100.0
    impact_vs_fpm: float = -1000.0
    impact_roll_deg: float = 60.0
    air_ground_dwell_s: float = 1.0  # on_ground 확정 시간
    vs_window_s: float = 1.0         # VS 이동평균 창
    dwell_s: dict = field(default_factory=lambda: {
        "GROUND_STOP": 2.0, "TAXI": 2.0, "TAKEOFF_ROLL": 1.0,
        "CLIMB": 2.0, "CRUISE": 8.0, "DESCENT": 3.0, "APPROACH": 3.0,
        "GO_AROUND": 1.0, "LANDING_ROLL": 0.0, "IMPACT": 0.0,
    })


@dataclass
class Transition:
    flight_id: int
    t_sim: float
    seq: int
    from_phase: str
    to_phase: str
    note: str


class PhaseDetector:
    def __init__(self, th: Optional[Thresholds] = None):
        self.th = th or Thresholds()
        self.reset()

    def reset(self):
        self.phase: Optional[str] = None
        self.flight_id: Optional[int] = None
        self.cand: Optional[str] = None
        self.cand_since: float = 0.0
        self.gnd_since: Optional[float] = None
        self.air_since: Optional[float] = None
        self.last_on_ground: Optional[int] = None
        self.vs_buf = deque()
        self.transitions: List[Transition] = []
        self.vs_smooth: float = 0.0
        self.was_airborne: bool = False
        self.edge_state: Optional[FlightState] = None   # on_ground 가 바뀐 순간의 상태
        self.phase_since: float = 0.0

    # ---- helpers -----------------------------------------------------
    def _update_vs(self, t: float, vs: float) -> float:
        self.vs_buf.append((t, vs))
        while self.vs_buf and t - self.vs_buf[0][0] > self.th.vs_window_s:
            self.vs_buf.popleft()
        self.vs_smooth = sum(v for _, v in self.vs_buf) / len(self.vs_buf)
        return self.vs_smooth

    def _surface(self, s: FlightState) -> Tuple[bool, bool]:
        """(ground_confirmed, airborne_confirmed)"""
        if s.on_ground != self.last_on_ground:
            self.last_on_ground = s.on_ground
            self.edge_state = s
            if s.on_ground:
                self.gnd_since, self.air_since = s.t_sim, None
            else:
                self.air_since, self.gnd_since = s.t_sim, None
        d = self.th.air_ground_dwell_s
        g = self.gnd_since is not None and s.t_sim - self.gnd_since >= d
        a = self.air_since is not None and s.t_sim - self.air_since >= d
        return g, a

    # ---- main --------------------------------------------------------
    def update(self, s: FlightState) -> str:
        th = self.th
        if self.flight_id != s.flight_id:          # 새 비행: 상태 초기화
            fid = s.flight_id; tr = self.transitions
            self.reset(); self.flight_id = fid; self.transitions = tr
        if s.paused:                                # 일시정지: 직전 phase 유지
            return self.phase or "GROUND_STOP"

        vs = self._update_vs(s.t_sim, s.vs_fpm)
        ground, airborne = self._surface(s)
        cur = self.phase
        cand, note = cur, ""

        # ---------- 초기 상태 ----------
        if cur is None:
            if s.on_ground:
                cand = "TAKEOFF_ROLL" if (s.throttle >= th.throttle_takeoff and s.gs_kt >= th.gs_stop_kt) else \
                       ("GROUND_STOP" if s.gs_kt < th.gs_stop_kt else "TAXI")
            else:
                cand = "CLIMB" if vs > th.vs_climb_fpm else ("DESCENT" if vs < th.vs_descent_fpm else "CRUISE")
                self.was_airborne = True
            self.phase, self.phase_since = cand, s.t_sim
            self.transitions.append(Transition(s.flight_id, s.t_sim, s.seq, "-", cand, "initial"))
            return cand

        # ---------- 지상 확정 ----------
        if ground:
            if cur == "IMPACT":
                cand = "IMPACT"
            elif cur in AIR_PHASES:                 # 접지
                e = self.edge_state or s
                if e.vs_fpm < th.impact_vs_fpm or abs(e.roll_deg) > th.impact_roll_deg or self.vs_smooth < th.impact_vs_fpm:
                    cand, note = "IMPACT", f"touchdown t={e.t_sim:.1f} vs={e.vs_fpm:.0f} roll={e.roll_deg:.0f}"
                else:
                    cand, note = "LANDING_ROLL", f"touchdown t={e.t_sim:.1f} gs={e.gs_kt:.0f} ias={e.ias_kt:.0f} vs={e.vs_fpm:.0f}"
            elif cur == "LANDING_ROLL":
                if s.gs_kt < th.gs_stop_kt:
                    cand = "GROUND_STOP"
                elif s.gs_kt < th.landing_roll_exit_gs_kt and s.throttle < th.throttle_takeoff:
                    cand = "TAXI"
                else:
                    cand = "LANDING_ROLL"
            else:                                   # 지상 단계 간 전이
                if s.throttle >= th.throttle_takeoff and s.gs_kt >= th.gs_stop_kt:
                    cand = "TAKEOFF_ROLL"
                elif s.gs_kt < th.gs_stop_kt:
                    cand = "GROUND_STOP"
                elif cur == "TAKEOFF_ROLL" and s.throttle >= th.throttle_takeoff:
                    cand = "TAKEOFF_ROLL"            # 활주 중 순간 GS 조건 유지
                else:
                    cand = "TAXI"
        # ---------- 공중 확정 ----------
        elif airborne:
            if cur in GROUND_PHASES or cur == "LANDING_ROLL":
                e = self.edge_state or s
                cand, note = "CLIMB", f"liftoff t={e.t_sim:.1f} ias={e.ias_kt:.0f} gs={e.gs_kt:.0f} pitch={e.pitch_deg:.1f} flap={e.flap:.2f}"
                self.was_airborne = True
            elif cur == "GO_AROUND":
                cand = "CLIMB" if (s.agl_ft >= th.go_around_exit_agl_ft or s.t_sim - self.phase_since >= th.go_around_max_s) else "GO_AROUND"
            elif cur == "APPROACH" and s.throttle >= th.throttle_go_around and vs > th.go_around_vs_fpm:
                cand, note = "GO_AROUND", f"agl={s.agl_ft:.0f} ias={s.ias_kt:.0f} thr={s.throttle:.2f}"
            elif vs > th.vs_climb_fpm:
                cand = "CLIMB"
            elif vs < th.vs_descent_fpm:
                cand = "APPROACH" if s.agl_ft < th.approach_agl_ft else "DESCENT"
            else:                                    # 수평
                if cur in ("DESCENT", "APPROACH") and s.agl_ft < th.approach_agl_ft:
                    cand = "APPROACH"
                else:
                    cand = "CRUISE"
        else:
            cand = cur                               # 지상/공중 미확정 구간: 유지

        # ---------- dwell 적용 ----------
        if cand != self.cand:
            self.cand, self.cand_since = cand, s.t_sim
        if cand != cur and s.t_sim - self.cand_since >= th.dwell_s.get(cand, 0.0):
            self.transitions.append(Transition(s.flight_id, s.t_sim, s.seq, cur, cand, note))
            self.phase, self.phase_since = cand, s.t_sim
        return self.phase
