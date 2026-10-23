# -*- coding: utf-8 -*-
"""
flight_state.py — STEP 2: Flight State Vector

xp_collector.py 가 기록한 CSV 한 행을 시스템 공통 상태(FlightState)로 정규화한다.
  · 단위 통일: 속도 kt, 고도 ft, 승강률 fpm, 각도 deg, 비율 0~1
  · flight_id: sim_time_s 가 되돌아가면(X-Plane 리셋) +1
  · paused 행은 상태 계산에서 제외(phase 는 직전 값 유지)
Phase Detector, Rule Engine, LLM Context 는 모두 이 구조만 입력으로 받는다.
"""
from dataclasses import dataclass, asdict, fields
from typing import Iterator, Optional
import csv

FT_PER_M = 3.28084
KT_PER_MS = 1.94384


@dataclass
class FlightState:
    flight_id: int
    seq: int
    t_sim: float          # sim/time/total_flight_time_sec (s)
    t_wall: float         # epoch (s)
    ias_kt: float         # 지시대기속도 (약 5 kt 이하에서는 0 근처로 읽힘 → 지상 판정에 사용 금지)
    gs_kt: float          # 대지속도
    alt_ft: float         # 고도계 표시값 (MSL, ft)
    agl_ft: float         # 지표고도 (ft)
    vs_fpm: float         # 승강률
    hdg_deg: float        # 자방위
    pitch_deg: float
    roll_deg: float
    throttle: float       # 0~1
    rpm: float
    flap: float           # 0~1 (C172: 0.33 = 10°, 0.67 = 20°, 1.0 = 30°)
    on_ground: int        # 0/1
    paused: int           # 0/1
    # ---- 선택 항목 (2026-10-23 추가, 로그에 없으면 None) ----
    dest_id: Optional[str] = None      # GPS 목적지 식별자 (예: EDMP)
    dest_dist_nm: Optional[float] = None
    dest_brg_deg: Optional[float] = None
    dest_ete_min: Optional[float] = None
    fuel_gal: Optional[float] = None   # 총 잔여 연료 (US gal)
    fuel_flow_gph: Optional[float] = None
    wind_dir_deg: Optional[float] = None  # 진북 기준
    wind_spd_kt: Optional[float] = None
    oil_temp_c: Optional[float] = None
    bus_volts: Optional[float] = None

    @classmethod
    def from_row(cls, row: dict, flight_id: int) -> "FlightState":
        f = lambda k: float(row[k])
        def g(k):
            v = row.get(k)
            try:
                return None if v is None or v == "" else float(v)
            except (TypeError, ValueError):
                return None
        codes = [g(f"dest_id_{i}") for i in range(8)]
        dest = "".join(chr(int(c)) for c in codes if c is not None and 32 < c < 127).strip() or None
        fuel = None if g("fuel_kg_0") is None else (g("fuel_kg_0") + (g("fuel_kg_1") or 0.0)) * 2.2046 / 6.0
        flow = None if g("fuel_flow_kgs") is None else g("fuel_flow_kgs") * 3600.0 * 2.2046 / 6.0
        if g("wind_spd_ms") is not None:
            wspd, wdir = g("wind_spd_ms") * KT_PER_MS, g("wind_dir_deg")
        else:
            wspd, wdir = g("wind_spd_kt_old"), g("wind_dir_deg_old")
        return cls(
            dest_id=dest, dest_dist_nm=g("dest_dist_nm"), dest_brg_deg=g("dest_brg_deg"), dest_ete_min=g("dest_ete_min"),
            fuel_gal=fuel, fuel_flow_gph=flow, wind_dir_deg=wdir, wind_spd_kt=wspd, oil_temp_c=g("oil_temp_c"), bus_volts=g("bus_volts"),
            flight_id=flight_id, seq=int(float(row["seq"])),
            t_sim=f("sim_time_s"), t_wall=f("wall_epoch"),
            ias_kt=f("airspeed_kias"), gs_kt=f("groundspeed_ms") * KT_PER_MS,
            alt_ft=f("altitude_ft"), agl_ft=f("agl_m") * FT_PER_M, vs_fpm=f("vs_fpm"),
            hdg_deg=f("heading_mag_deg"), pitch_deg=f("pitch_deg"), roll_deg=f("roll_deg"),
            throttle=f("throttle_ratio"), rpm=f("engine_rpm"), flap=f("flap_ratio"),
            on_ground=int(round(f("on_ground"))), paused=int(round(f("paused"))),
        )

    def as_dict(self) -> dict:
        return asdict(self)


COLUMNS = [fl.name for fl in fields(FlightState)]


def iter_states(csv_path: str) -> Iterator[FlightState]:
    """CSV 를 순서대로 읽어 FlightState 를 yield. sim_time 역행(1 s 이상)마다 flight_id 증가."""
    flight_id, prev_t = 1, None
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            t = float(row["sim_time_s"])
            if prev_t is not None and t < prev_t - 1.0:
                flight_id += 1
            prev_t = t
            yield FlightState.from_row(row, flight_id)
