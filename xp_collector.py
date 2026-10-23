#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xp_collector.py — X-Plane 12 실시간 비행데이터 수집기 (STEP 1)

  · X-Plane 내장 RREF(UDP) 인터페이스 사용 → 플러그인·설정 변경 불필요
  · Python 표준 라이브러리만 사용 (socket, struct, csv)
  · 구독한 DataRef 값을 수신 주기마다 CSV 한 행으로 저장
      wall_time / wall_epoch(PC 시각) + sim_time_s(시뮬 시각) + seq(행 번호) + n_pairs(수신 쌍 수)
  · --replay : 저장된 CSV를 같은 인터페이스(samples())로 재생 → 이후 State Vector / Phase 개발용

사용 예
  python xp_collector.py                        # X-Plane이 같은 PC (127.0.0.1:49000), 10 Hz, logs/ 에 저장
  python xp_collector.py --host 192.168.0.10    # X-Plane이 다른 PC일 때 (그 PC의 IP)
  python xp_collector.py --hz 20 --print-every 20 --out logs
  python xp_collector.py --replay logs/flight_20260923_101500.csv --speed 2   # 2배속 재생, 0 = 최대 속도

프로토콜 요약 (X-Plane 10.40+ / 11 / 12 공통)
  요청: b"RREF" + 0x00 + int32 freq + int32 index + char[400] dataref   (총 413 bytes) → X-Plane:49000
  응답: b"RREF," + (int32 index, float32 value) × N                       → 요청을 보낸 소켓(IP:port)으로 회신
  구독 해제: 같은 요청을 freq=0 으로 전송
"""
import argparse
import csv
import datetime as dt
import os
import socket
import struct
import sys
import time

# ---------------------------------------------------------------------------
# 구독 DataRef 목록
#   (CSV 열 이름, DataRef, 형)   형: "f" float, "i" int(0/1)
#   · RREF 응답은 항상 float32 → "i" 는 수신 후 정수로 변환
#   · 배열형 DataRef 는 [index] 로 원소 지정 (throttle_ratio[0] 등)
#   · ★ 마지막 항목이 수신될 때 CSV 한 행을 기록한다(주기 완료 표식). 순서를 바꿀 때 유의.
# ---------------------------------------------------------------------------
DATAREFS = [
    ("airspeed_kias",    "sim/cockpit2/gauges/indicators/airspeed_kts_pilot",    "f"),  # 조종석 속도계 표시값(KIAS)
    ("altitude_ft",      "sim/cockpit2/gauges/indicators/altitude_ft_pilot",     "f"),  # 고도계 표시값(ft, 고도계 세팅 반영)
    ("vs_fpm",           "sim/cockpit2/gauges/indicators/vvi_fpm_pilot",         "f"),  # 승강계(fpm)
    ("heading_mag_deg",  "sim/flightmodel/position/mag_psi",                     "f"),  # 자방위(deg)
    ("pitch_deg",        "sim/flightmodel/position/theta",                       "f"),
    ("roll_deg",         "sim/flightmodel/position/phi",                         "f"),
    ("throttle_ratio",   "sim/cockpit2/engine/actuators/throttle_ratio[0]",      "f"),  # 스로틀 명령(0~1)
    ("engine_rpm",       "sim/cockpit2/engine/indicators/engine_speed_rpm[0]",   "f"),  # 엔진 RPM(실제 출력 지표)
    ("flap_ratio",       "sim/flightmodel2/controls/flap_handle_deploy_ratio",   "f"),  # 플랩 전개 비율(0~1)
    ("on_ground",        "sim/flightmodel/failures/onground_any",                "i"),  # 접지 여부
    ("agl_m",            "sim/flightmodel/position/y_agl",                       "f"),  # 지표고도(m)
    ("groundspeed_ms",   "sim/flightmodel/position/groundspeed",                 "f"),  # 대지속도(m/s)
    ("elevation_msl_m",  "sim/flightmodel/position/elevation",                   "f"),  # 해면고도(m)
    ("latitude",         "sim/flightmodel/position/latitude",                    "f"),  # float32 정밀도(약 1 m)
    ("longitude",        "sim/flightmodel/position/longitude",                   "f"),
    # ---- 항법 · 연료 · 기상 · 계통 (2026-10-23 추가: LLM 컨텍스트 보강용) ----
    ("dest_dist_nm",     "sim/cockpit2/radios/indicators/gps_dme_distance_nm",   "f"),  # GPS 목적지(활성 waypoint)까지 거리(NM)
    ("dest_brg_deg",     "sim/cockpit2/radios/indicators/gps_bearing_deg_mag",   "f"),  # 목적지 방위(자북)
    ("dest_ete_min",     "sim/cockpit2/radios/indicators/gps_dme_time_min",      "f"),  # 목적지까지 예상 시간(min)
    ("dest_id_0",        "sim/cockpit2/radios/indicators/gps_nav_id[0]",         "i"),  # 목적지 식별자(문자 코드 8자)
    ("dest_id_1",        "sim/cockpit2/radios/indicators/gps_nav_id[1]",         "i"),
    ("dest_id_2",        "sim/cockpit2/radios/indicators/gps_nav_id[2]",         "i"),
    ("dest_id_3",        "sim/cockpit2/radios/indicators/gps_nav_id[3]",         "i"),
    ("dest_id_4",        "sim/cockpit2/radios/indicators/gps_nav_id[4]",         "i"),
    ("dest_id_5",        "sim/cockpit2/radios/indicators/gps_nav_id[5]",         "i"),
    ("dest_id_6",        "sim/cockpit2/radios/indicators/gps_nav_id[6]",         "i"),
    ("dest_id_7",        "sim/cockpit2/radios/indicators/gps_nav_id[7]",         "i"),
    ("fuel_kg_0",        "sim/cockpit2/fuel/fuel_quantity[0]",                   "f"),  # 좌측 탱크(kg)
    ("fuel_kg_1",        "sim/cockpit2/fuel/fuel_quantity[1]",                   "f"),  # 우측 탱크(kg)
    ("fuel_flow_kgs",    "sim/cockpit2/engine/indicators/fuel_flow_kg_sec[0]",   "f"),  # 연료 유량(kg/s)
    ("wind_spd_ms",      "sim/weather/aircraft/wind_now_speed_msc",              "f"),  # 기체 위치 바람(X-Plane 12)
    ("wind_dir_deg",     "sim/weather/aircraft/wind_now_direction_degt",         "f"),
    ("wind_spd_kt_old",  "sim/weather/wind_speed_kt",                            "f"),  # 구형 이름(둘 중 들어오는 것 사용)
    ("wind_dir_deg_old", "sim/weather/wind_direction_degt",                      "f"),
    ("oil_temp_c",       "sim/cockpit2/engine/indicators/oil_temperature_deg_C[0]", "f"),
    ("bus_volts",        "sim/cockpit2/electrical/bus_volts[0]",                 "f"),
    ("sim_time_s",       "sim/time/total_flight_time_sec",                       "f"),  # 시뮬 비행시각(s)
    ("paused",           "sim/time/paused",                                      "i"),  # 일시정지 여부 ★행 기록 표식
]
COLUMNS = ["seq", "wall_time", "wall_epoch", "n_pairs"] + [c for c, _, _ in DATAREFS]

RREF_REQ_FMT = "<4sxii400s"   # 'RREF' + NUL + freq + index + dataref(400)
RREF_PAIR_FMT = "<if"         # index(int32) + value(float32)


class LiveUDPSource:
    """X-Plane RREF 구독 소스. samples() 가 주기마다 dict 를 yield 한다(수신 없음 시 None)."""

    def __init__(self, host="127.0.0.1", port=49000, local_port=49005, hz=10, timeout=2.0):
        self.addr = (host, port)
        self.hz = int(hz)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", local_port))      # X-Plane 은 요청을 보낸 소켓으로 회신 → 반드시 같은 소켓으로 송수신
        self.sock.settimeout(timeout)                 # Windows 에서 Ctrl+C 가 먹히도록 블로킹 회피
        self.latest = {c: None for c, _, _ in DATAREFS}
        self.seen = set()
        self.marker_idx = len(DATAREFS) - 1
        self.pairs_since_row = 0
        self.first_rx = None
        self.timeout = timeout
        self.last_error = None   # "timeout" | "unreachable"

    def subscribe(self, freq=None):
        freq = self.hz if freq is None else int(freq)
        for idx, (_, dref, _) in enumerate(DATAREFS):
            msg = struct.pack(RREF_REQ_FMT, b"RREF", freq, idx, dref.encode("ascii"))
            assert len(msg) == 413
            self.sock.sendto(msg, self.addr)

    def unsubscribe(self):
        try:
            self.subscribe(freq=0)
        except OSError:
            pass

    def close(self):
        self.unsubscribe()
        self.sock.close()

    def missing(self):
        return [DATAREFS[i][1] for i in range(len(DATAREFS)) if i not in self.seen]

    def samples(self):
        self.subscribe()
        timeouts = 0
        while True:
            try:
                data, _ = self.sock.recvfrom(4096)
            except socket.timeout:
                self.last_error = "timeout"
                timeouts += 1
                if timeouts % 5 == 0:
                    self.subscribe()          # X-Plane 재시작·비행 재로딩 등에 대비해 재구독
                yield None
                continue
            except ConnectionResetError:
                # Windows 전용 현상: 보낸 포트(49000)에 수신 프로그램이 없으면 ICMP port-unreachable 이
                # 다음 recvfrom 에서 WinError 10054 로 보고됨 → "X-Plane 이 그 포트에서 듣고 있지 않음" 의미
                self.last_error = "unreachable"
                timeouts += 1
                time.sleep(self.timeout)      # 예외는 즉시 반환되므로 대기 후 재시도
                if timeouts % 5 == 0:
                    self.subscribe()
                yield None
                continue
            timeouts = 0
            self.last_error = None
            if data[:4] != b"RREF":
                continue
            body = data[5:]                    # 5번째 바이트는 ','
            row_ready = False
            for k in range(len(body) // 8):
                idx, val = struct.unpack_from(RREF_PAIR_FMT, body, 8 * k)
                if 0 <= idx < len(DATAREFS):
                    col, _, dtype = DATAREFS[idx]
                    self.latest[col] = int(round(val)) if dtype == "i" else float(val)
                    self.seen.add(idx)
                    self.pairs_since_row += 1
                    if idx == self.marker_idx:
                        row_ready = True
            if self.first_rx is None:
                self.first_rx = time.time()
            if row_ready:
                row = dict(self.latest)
                row["n_pairs"] = self.pairs_since_row
                self.pairs_since_row = 0
                yield row


class CSVReplaySource:
    """저장된 CSV 를 wall_epoch 간격대로 재생. speed=0 이면 지연 없이 최대 속도."""

    def __init__(self, path, speed=1.0):
        self.path = path
        self.speed = float(speed)

    def samples(self):
        with open(self.path, newline="", encoding="utf-8") as f:
            prev = None
            for raw in csv.DictReader(f):
                t = float(raw["wall_epoch"])
                if prev is not None and self.speed > 0:
                    time.sleep(max(0.0, (t - prev) / self.speed))
                prev = t
                row = {}
                for k, v in raw.items():
                    if v is None or v == "":
                        row[k] = None
                    else:
                        try:
                            row[k] = float(v) if ("." in v or "e" in v.lower()) else int(v)
                        except ValueError:
                            row[k] = v
                yield row


def _fmt_csv(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.5f}"
    return str(v)


def fmt_line(r):
    def g(k, f="{:.1f}"):
        v = r.get(k)
        return "--" if v is None else f.format(v)
    return (f"t={g('sim_time_s')}s  IAS={g('airspeed_kias')}kt  ALT={g('altitude_ft', '{:.0f}')}ft  "
            f"VS={g('vs_fpm', '{:+.0f}')}fpm  HDG={g('heading_mag_deg', '{:03.0f}')}  "
            f"P={g('pitch_deg')}  R={g('roll_deg')}  RPM={g('engine_rpm', '{:.0f}')}  "
            f"FLAP={g('flap_ratio', '{:.2f}')}  AGL={g('agl_m', '{:.0f}')}m  "
            f"GND={r.get('on_ground')}  PAUSE={r.get('paused')}  n={r.get('n_pairs')}")


def collect(args):
    os.makedirs(args.out, exist_ok=True)
    fname = os.path.join(args.out, dt.datetime.now().strftime("flight_%Y%m%d_%H%M%S.csv"))
    src = LiveUDPSource(args.host, args.port, args.local_port, args.hz, args.timeout)
    print(f"[collector] X-Plane {args.host}:{args.port}  ←→  local UDP {args.local_port}  |  {args.hz} Hz  |  "
          f"{len(DATAREFS)} datarefs")
    print(f"[collector] 저장: {fname}   (Ctrl+C 로 종료)")
    seq, warned = 0, False
    with open(fname, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        try:
            for row in src.samples():
                if row is None:
                    if src.last_error == "unreachable":
                        print(f"[collector] {args.host}:{args.port} 에서 받는 프로그램이 없음 — X-Plane 이 실행 중인지, "
                              f"Settings → Network 의 수신 UDP 포트가 {args.port} 인지 확인 (재시도 중 ...)")
                    else:
                        print("[collector] 수신 없음 — X-Plane 이 비행 화면인지 / --host IP / 방화벽(UDP) 확인 (재시도 중 ...)")
                    continue
                seq += 1
                now = time.time()
                row["seq"] = seq
                row["wall_epoch"] = f"{now:.3f}"
                row["wall_time"] = dt.datetime.fromtimestamp(now).isoformat(timespec="milliseconds")
                w.writerow({k: _fmt_csv(row.get(k)) for k in COLUMNS})
                f.flush()
                if seq % args.print_every == 0:
                    print(fmt_line(row))
                if not warned and src.first_rx and now - src.first_rx > 3.0:
                    warned = True
                    miss = src.missing()
                    if miss:
                        print("[collector] 경고: 3초간 미수신 DataRef (이름 오류 또는 해당 기체에 없음):")
                        for m in miss:
                            print("    -", m)
                    else:
                        print(f"[collector] OK: {len(DATAREFS)}개 DataRef 모두 수신 중")
        except KeyboardInterrupt:
            pass
        finally:
            src.close()
    print(f"[collector] 종료 — {seq} rows 저장: {fname}")


def replay(args):
    src = CSVReplaySource(args.replay, args.speed)
    print(f"[replay] {args.replay}  (speed x{args.speed})")
    n = 0
    t0 = time.time()
    try:
        for row in src.samples():
            n += 1
            if n % args.print_every == 0:
                print(fmt_line(row))
    except KeyboardInterrupt:
        pass
    print(f"[replay] {n} rows, {time.time() - t0:.1f}s")


def main():
    ap = argparse.ArgumentParser(description="X-Plane 12 RREF/UDP flight data collector")
    ap.add_argument("--host", default="127.0.0.1", help="X-Plane PC IP (같은 PC면 127.0.0.1)")
    ap.add_argument("--port", type=int, default=49000, help="X-Plane 수신 포트(고정 49000)")
    ap.add_argument("--local-port", type=int, default=49005, help="이 스크립트가 응답을 받을 UDP 포트")
    ap.add_argument("--hz", type=int, default=10, help="구독 주기(Hz)")
    ap.add_argument("--timeout", type=float, default=2.0, help="수신 대기 시간(s)")
    ap.add_argument("--out", default="logs", help="CSV 저장 폴더")
    ap.add_argument("--print-every", type=int, default=10, help="N행마다 콘솔 출력")
    ap.add_argument("--replay", default=None, help="재생할 CSV 경로 (지정 시 수집 대신 재생)")
    ap.add_argument("--speed", type=float, default=1.0, help="재생 배속 (0 = 최대 속도)")
    args = ap.parse_args()
    if args.replay:
        replay(args)
    else:
        collect(args)


if __name__ == "__main__":
    main()
