#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mock_xplane.py — X-Plane 없이 xp_collector.py 를 시험하기 위한 가짜 X-Plane (RREF 응답기)

  python mock_xplane.py          # 49000 포트에서 RREF 구독을 받아 가상 비행 프로파일 값을 송신
  (다른 터미널)  python xp_collector.py

  · 실제 X-Plane 과 같은 PC 에서 동시에 실행하지 마십시오 (49000 포트 충돌)
  · 프로파일: 지상 대기(10s) → 이륙활주 → 55kt 회전 → 700fpm 상승 → 3000ft AGL 순항
  · 값은 검증용 단순 모델이며 실제 C172 동역학이 아님
"""
import math
import socket
import struct
import time

PORT = 49000
FIELD_ELEV_FT = 430.0   # 가상 공항 표고(ft)


def profile(t):
    """t: 시작 후 경과 초. DataRef 이름 → 값 dict 반환."""
    ias = alt = vs = pitch = 0.0
    thr, rpm, flap, gnd = 0.10, 1000.0, 0.0, 1
    if t < 10:                                   # 지상 대기
        pass
    elif t < 24.0:                               # 이륙 활주 (15 s 동안 0 → 60 kt)
        ias = 60.0 * (t - 10) / 15.0
        thr, rpm = 1.0, 2400.0
        flap = 0.0
    else:                                        # 회전 이후
        tc = t - 24.0
        gnd = 0
        thr, rpm = 1.0, 2500.0
        ias = min(75.0, 56.0 + 2.0 * tc)
        alt = min(3000.0, 700.0 / 60.0 * tc)     # 700 fpm 상승, 3000 ft AGL 까지
        if alt < 3000.0:
            vs, pitch = 700.0, 8.0
        else:                                    # 순항
            vs, pitch = 0.0, 2.0
            ias, thr, rpm = 110.0, 0.70, 2300.0
    hdg = (180.0 + 2.0 * math.sin(t / 7.0)) % 360.0
    roll = 1.5 * math.sin(t / 5.0) if gnd == 0 else 0.0
    gs = ias * 0.5144 if gnd == 0 else ias * 0.5144
    return {
        "sim/cockpit2/gauges/indicators/airspeed_kts_pilot": ias,
        "sim/cockpit2/gauges/indicators/altitude_ft_pilot": FIELD_ELEV_FT + alt,
        "sim/cockpit2/gauges/indicators/vvi_fpm_pilot": vs,
        "sim/flightmodel/position/mag_psi": hdg,
        "sim/flightmodel/position/theta": pitch,
        "sim/flightmodel/position/phi": roll,
        "sim/cockpit2/engine/actuators/throttle_ratio[0]": thr,
        "sim/cockpit2/engine/indicators/engine_speed_rpm[0]": rpm,
        "sim/flightmodel2/controls/flap_handle_deploy_ratio": flap,
        "sim/flightmodel/failures/onground_any": float(gnd),
        "sim/flightmodel/position/y_agl": alt * 0.3048,
        "sim/flightmodel/position/groundspeed": gs,
        "sim/flightmodel/position/elevation": (FIELD_ELEV_FT + alt) * 0.3048,
        "sim/flightmodel/position/latitude": 37.5583,
        "sim/flightmodel/position/longitude": 126.7906,
        "sim/time/total_flight_time_sec": t,
        "sim/time/paused": 0.0,
        "sim/cockpit2/radios/indicators/gps_dme_distance_nm": max(0.0, 25.0 - 0.02 * t),
        "sim/cockpit2/radios/indicators/gps_bearing_deg_mag": 62.0,
        "sim/cockpit2/radios/indicators/gps_dme_time_min": max(0.0, 14.0 - 0.011 * t),
        **{f"sim/cockpit2/radios/indicators/gps_nav_id[{i}]": float(ord(c)) for i, c in enumerate("EDMP")},
        **{f"sim/cockpit2/radios/indicators/gps_nav_id[{i}]": 0.0 for i in range(4, 8)},
        "sim/cockpit2/fuel/fuel_quantity[0]": 60.0, "sim/cockpit2/fuel/fuel_quantity[1]": 58.0,
        "sim/cockpit2/engine/indicators/fuel_flow_kg_sec[0]": 0.0075,
        "sim/weather/aircraft/wind_now_speed_msc": 4.0, "sim/weather/aircraft/wind_now_direction_degt": 240.0,
        "sim/cockpit2/engine/indicators/oil_temperature_deg_C[0]": 82.0, "sim/cockpit2/electrical/bus_volts[0]": 27.8,
    }


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", PORT))
    sock.settimeout(0.005)
    subs = {}              # client addr → {index: (dataref, freq)}
    next_send = {}         # client addr → 다음 송신 시각
    t0 = time.time()
    print(f"[mock-xplane] listening UDP {PORT}  (Ctrl+C 로 종료)")
    try:
        while True:
            try:
                data, addr = sock.recvfrom(1024)
                if data[:4] == b"RREF" and len(data) == 413:
                    _, freq, idx, name = struct.unpack("<4sxii400s", data)
                    name = name.split(b"\x00", 1)[0].decode("ascii", "ignore")
                    d = subs.setdefault(addr, {})
                    if freq <= 0:
                        d.pop(idx, None)
                        print(f"[mock-xplane] {addr} unsubscribe idx={idx}")
                    else:
                        if idx not in d:
                            print(f"[mock-xplane] {addr} subscribe idx={idx} freq={freq} {name}")
                        d[idx] = (name, freq)
                        next_send.setdefault(addr, 0.0)
            except socket.timeout:
                pass
            now = time.time()
            vals = None
            for addr, d in list(subs.items()):
                if not d or now < next_send.get(addr, 0.0):
                    continue
                freq = max(v[1] for v in d.values())
                next_send[addr] = now + 1.0 / freq
                if vals is None:
                    vals = profile(now - t0)
                body = b"".join(struct.pack("<if", idx, vals[name])
                                for idx, (name, _) in sorted(d.items()) if name in vals)
                if body:
                    sock.sendto(b"RREF," + body, addr)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        print("[mock-xplane] stopped")


if __name__ == "__main__":
    main()
