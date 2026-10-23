# xp_phase — Flight State Vector (STEP 2) + Phase Detector v0 (STEP 3) + Rule Engine v0 (STEP 4)

| 파일 | 내용 |
|---|---|
| `flight_state.py` | 수집 CSV 한 행 → `FlightState`(단위 통일: kt, ft, fpm, deg). `sim_time_s` 역행 시 `flight_id` +1 |
| `phase_detector.py` | 결정론적 상태기계. 임계값은 `Thresholds` 데이터클래스에 집중 |
| `run_phase_detection.py` | CSV → `<out>/<이름>_phased.csv`(phase 열 추가) + `<이름>_phases.txt`(구간·전이 요약) |
| `phased/` | STEP 3 을 2026-09-23/24 로그 4개(이륙·추락·복행·순항·착륙)에 적용한 결과 |
| `rule_engine.py` | STEP 4. 규칙 15개(`RULES`)와 기준속도(`SPEEDS`, `ACS`)를 선언적으로 정의. 이벤트(ONSET/CLEAR) + status |
| `run_rule_engine.py` | CSV → phase → 규칙 평가. `<out>/<이름>_situation.csv`(샘플별 phase·active_rules), `_events.csv`, `_events.txt` |
| `situation/` | STEP 4 를 로그 4개에 적용한 결과 |

실행 (표준 라이브러리만 사용)

```
cd C:\xp_collector\xp_phase
py run_phase_detection.py ..\logs\*.csv --out phased      # STEP 3 만
py run_rule_engine.py ..\logs\*.csv --out situation       # STEP 3 + 4
```

규칙 v0 (rule_engine.py)

| ID | 이름 | 등급 | 근거 |
|---|---|---|---|
| R01 | AIRSPEED_LOW_AIRBORNE | WARNING | 실속속도(플랩 업 48 / 10° 43 / 30° 40, 구간 보간)×1.1 — POH Fig 5-3 |
| R02/R03 | APPROACH_SPEED_HIGH/LOW | CAUTION | POH §4 접근속도 65-75(플랩 업)/60-70(30°) 사이 보간 ± ACS +10/-5 |
| R04 | FLAP_OVERSPEED | WARNING | Vfe 110/85 — POH Fig 2-1 |
| R05 | SINK_RATE_HIGH_LOW_ALT | WARNING | 1,000 ft AGL 미만 강하율 1,000 fpm — 설계값 |
| R06 | BANK_HIGH_LOW_ALT | CAUTION | 1,000 ft AGL 미만 경사 30° — 설계값 |
| R07 | POWER_LOSS_LOW_ALT | WARNING | 상승 중 1 s 내 스로틀 0.5 이상 감소 — 설계값 |
| R08 | EARLY_ROTATION | CAUTION | 부양 IAS < 55−5(플랩 업, POH §4) / 1.1×Vs(플랩 10°, 설계) |
| R09 | CLIMB_SPEED_OFF_VY | INFO | Enroute climb 70-85(POH §4) −5/+10 |
| R10/R11 | CRUISE_ALT/HDG_DEVIATION | INFO | ACS ±100 ft / ±10° (verify), 순항 진입 15 s 후 기준값 확정 |
| R12 | GO_AROUND | INFO | 복행 전이 — POH §4 Balked Landing 절차 연계 |
| R13/R14 | HARD_TOUCHDOWN / TOUCHDOWN_SPEED_HIGH | WARNING/CAUTION | 접지 VS −500 fpm / IAS 상한 — 설계값 (verify) |
| R15 | UNSTABILIZED_AT_500FT | CAUTION | 500 ft AGL 게이트: 속도·강하율·플랩·경사 — 설계값 |
| R16 | GO_AROUND_FLAPS_NOT_RETRACTED | CAUTION | 복행 중 플랩 30° 유지 5 s — POH §4 Balked Landing (p.93) |
| R17 | TAKEOFF_POWER_LOW | CAUTION | 풀스로틀 활주 중 RPM < 2,300 — POH §2 static RPM (p.41) |

속도 기준값은 2026-09-24에 C172S Information Manual(Rev.5) 원문(§2 p.40-41, §3 p.55, §4 p.81/91-93, §5 p.124)으로 확인·확정했다. ACS 허용오차(±100 ft, ±10°, +10/-5 kt)만 ACS 원문 확인이 남아 있다.

단계: GROUND_STOP, TAXI, TAKEOFF_ROLL, CLIMB, CRUISE, DESCENT, APPROACH, GO_AROUND, LANDING_ROLL, IMPACT

설계 요점
- 지상/공중은 `on_ground`가 1 s 이상 유지될 때만 확정(부양 직후 바운스 필터)
- 각 단계는 후보 조건이 dwell(CLIMB 2 s, CRUISE 8 s, DESCENT/APPROACH 3 s …) 동안 지속돼야 전이
- 지상 단계는 GS 기준(IAS는 저속에서 0 근처로 읽힘), VS는 1 s 이동평균
- 일시정지 행은 판정에서 제외하고 직전 phase 유지

v0 한계(STEP 4 이후 보완)
- APPROACH = "1,000 ft AGL 미만 강하"로 정의. 착륙 형태(플랩·출력)나 활주로 정렬은 아직 미반영
- 지상에서 짧은 풀스로틀도 TAKEOFF_ROLL로 잡힘(1 s dwell)
- LANDING_ROLL은 2026-09-24 AI 착륙 1건으로만 검증됨(접지 55 kt → 25 kt, 11 s)
