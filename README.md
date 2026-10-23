# X-Plane 기반 LLM 항공 AI Assist 프로토타입

X-Plane 12에서 실시간으로 받은 비행 상태를 **규칙으로 판정**(비행단계·이상상황)하고, 그 상황에 맞는 POH(Pilot's Operating Handbook, 조종사 운용 교범) 절차를 **검색**한 뒤, 로컬 LLM이 조종사에게 **설명**하는 시스템입니다. 항공기를 제어하지 않으며, 조종사의 상황 인지와 판단을 돕는 Assist입니다.

설계 원칙 한 줄: **판단은 규칙이, 설명은 LLM이.** 안전과 관련된 판정(지금 어느 단계인가, 무엇이 기준을 벗어났는가)은 결정론적 코드가 계산하고, LLM은 그 결과와 POH 근거를 받아 문장으로 풀어 줍니다. 이 저장소에는 모델을 **학습(훈련)시키는 과정은 없습니다.** 공개 LLM을 그대로 쓰고, 입력(Context Package)을 잘 만드는 방식입니다.

---

## 1. 전체 구조

```
X-Plane 12 ──UDP(RREF)──▶ xp_collector.py ──▶ logs/flight_*.csv        [STEP 1 수집]
                                 │
                                 ▼
                  xp_phase/flight_state.py   상태 벡터(단위 통일)          [STEP 2]
                  xp_phase/phase_detector.py 비행단계 10종 판정            [STEP 3]
                  xp_phase/rule_engine.py    규칙 20개 → 이벤트/status     [STEP 4]
                                 │
                  xp_kb/kb_retriever.py      POH 청크 검색(단계 인지형)    [STEP 5]
                                 │
                  xp_llm/run_assist_replay.py  Context Package → Ollama   [STEP 6, 저장 로그 재생]
                  xp_llm/live_assist.py        실시간 화면 + 버튼 응답     [STEP 7, X-Plane 라이브]
```

| 폴더/파일 | 역할 |
|---|---|
| `xp_collector.py` | X-Plane → CSV 수집기. 추가 패키지 없음 |
| `mock_xplane.py` | X-Plane 없는 PC에서 수집기를 시험하는 가짜 X-Plane |
| `xp_phase/` | 상태 벡터, 단계 판정, 규칙 판정, 배치 실행기 |
| `xp_kb/` | POH 지식베이스 구축·검색. `kb/chunks.jsonl`이 지식베이스 본체 |
| `xp_llm/` | LLM 연동: 저장 로그 재생 실행기, 실시간 화면 |
| `logs/` | 수집된 비행 로그(CSV). 1행 = 0.1초 |
| `docs/` | 보고서·그림·설명 문서 |

모든 코드는 **Python 표준 라이브러리만** 사용합니다. `pip install`이 필요한 패키지는 없습니다.

---

## 2. 준비물 설치

### 2.1 Python
- Python 3.10 이상 (3.14까지 확인). python.org 설치 시 **"Add python.exe to PATH" 체크**.
- 확인: 명령 프롬프트에서 `py --version`
- PyCharm을 쓴다면 File → Open으로 이 저장소 폴더를 열고, 인터프리터를 위 Python으로 지정. PyCharm이 `.venv`를 만들어도 상관없음(설치할 패키지가 없음).

### 2.2 X-Plane 12
- 정식판 구매·설치(Steam 또는 x-plane.com, 약 60 USD). 데모판은 15분 후 조종 입력이 막혀 수집에 부적합.
- 권장 사양: RAM 16 GB 이상, GPU VRAM 4 GB 이상. LLM을 같은 PC에서 돌리려면 **VRAM 12 GB 이상**(RTX 3060 12 GB에서 X-Plane + 7B 모델 동시 실행 확인).
- 설정 변경은 **필요 없음**. 수집기는 X-Plane이 기본으로 여는 UDP 49000 포트의 RREF 인터페이스를 사용(Settings → Network 하단 "수신하는 포트(legacy)"가 49000인지만 확인).
- 전체화면 대신 **창 모드**(Settings → Graphics → Monitor Configuration → Windowed Simulator)로 두면 라이브 창을 겹쳐 볼 수 있음.

### 2.3 Ollama (로컬 LLM)
1. ollama.com에서 Windows용 설치. 설치 후 트레이에 Ollama 아이콘이 뜨면 서버가 돌고 있는 것(`http://localhost:11434`).
2. 모델 받기 (명령 프롬프트):
   ```
   ollama pull qwen2.5:7b-instruct      # 기본. VRAM 12 GB에서 X-Plane과 동시 실행 가능
   ollama pull llama3.1:8b              # 대안. 한국어 지시 준수가 더 안정적인 경우가 있음
   ollama list                          # 받은 모델 확인
   ```
3. `ollama serve`를 직접 칠 때 "Only one usage of each socket address" 오류가 나오면 **이미 서버가 돌고 있다는 뜻**이니 그대로 두면 됨.
4. 14B 이상 모델은 X-Plane과 같은 GPU에서 돌지 않음. 더 큰 모델은 다른 PC/서버에 두고 `--host http://서버IP:11434`로 연결.

### 2.4 POH 문서
- `xp_kb/kb/chunks.jsonl`은 Cessna 172S Information Manual(Rev.5, 2004)을 377개 조각으로 나눈 것. 저작권 문서이므로 **연구 내부용**으로만 쓰고 공개 저장소에는 올리지 않음.
- PDF 원본에서 다시 만들려면: `cd xp_kb` → `py build_kb.py POH-Cessna-172S.pdf --out kb` (poppler의 pdftotext 필요).

---

## 3. 폴더 구조 확인

```
<저장소 뿌리>\
   xp_collector.py  mock_xplane.py  README.md  .gitignore
   logs\        flight_*.csv
   xp_phase\    flight_state.py  phase_detector.py  rule_engine.py  run_phase_detection.py  run_rule_engine.py
   xp_kb\       build_kb.py  kb_retriever.py  query_kb.py  build_embeddings.py  kb\chunks.jsonl
   xp_llm\      run_assist_replay.py  live_assist.py
```

실행기들은 **자기 폴더 기준 상대경로**(`..\logs`, `..\xp_phase`, `..\xp_kb\kb`)를 쓰므로 이 구조를 유지해야 합니다. 경로에 한글·공백이 있어도 동작하지만, cmd에서 `cd`할 때는 따옴표로 감싸야 합니다.

---

## 4. 처음 한 번: X-Plane 없이 동작 확인

저장된 로그로 전 파이프라인을 돌려 봅니다(5분).

```
cd xp_phase
py run_phase_detection.py ..\logs\*.csv --out phased          # 단계 판정 → phased\*_phased.csv, *_phases.txt
py run_rule_engine.py ..\logs\*.csv --out situation            # 단계+규칙 → situation\*_events.txt
cd ..\xp_kb
py query_kb.py "착륙 전에 뭘 확인해야 해?" --phase APPROACH     # 검색 1위가 BEFORE LANDING p.92 이면 정상
cd ..\xp_llm
py run_assist_replay.py --dry-run                              # LLM 없이 4개 시점의 Context Package 생성
py run_assist_replay.py --model qwen2.5:7b-instruct            # Ollama 켜진 상태에서 실제 응답 → out\assist_replay_*.md
```

수집기만 따로 시험하려면 터미널 두 개로 `py mock_xplane.py`(가짜 X-Plane)와 `py xp_collector.py`를 띄우면 `OK: 37개 DataRef 모두 수신 중`이 떠야 합니다. (실제 X-Plane과 mock을 같은 PC에서 동시에 켜면 49000 포트가 충돌하니 둘 중 하나만.)

---

## 5. X-Plane에서 비행 데이터 수집 (STEP 1)

### 5.1 X-Plane 쪽
1. X-Plane 12 실행 → New Flight
2. Aircraft: **Cessna Skyhawk (172SP)**. 아날로그 계기든 G1000이든 수집에는 차이 없음
3. Location: 관제탑이 있는 공항, 시작 위치 **Runway**(시동 걸린 상태). 지금까지는 Salzburg(LOWS)를 사용
4. Weather: Clear, 바람 없음(처음엔 변수를 줄이기 위해)
5. Start Flight → 조종석 화면까지 로딩

### 5.2 수집기
```
cd <저장소 뿌리>
py xp_collector.py
```
1초 안에 아래처럼 찍히면 연결된 것입니다.
```
t=12.3s  IAS=0.0kt  ALT=1408ft  VS=+0fpm  HDG=332  ...  n=37
[collector] OK: 37개 DataRef 모두 수신 중
```
`Ctrl+C`로 종료하면 `logs\flight_YYYYMMDD_HHMMSS.csv`가 닫힙니다. 10 Hz로 기록되며 일시정지 중에도 기록됩니다(`paused=1`).

### 5.3 비행은 누가 하나
- **AI 자율비행(권장)**: X-Plane 메뉴바(마우스를 화면 맨 위로) → Flight → "AI flies your aircraft". 관제(ATC)와 교신 후 30~60초 뒤 스스로 택시·이륙·비행·착륙. 활주로 **정지 상태에서 켜고 착륙까지 끄지 말 것**(공중에서 끄면 스로틀이 키보드 값 0으로 돌아가 급강하). 단축키가 없으니 Settings → Keyboard에서 `AI`를 검색해 키를 하나 배정해 두면 편함.
- 수동 조종 핵심 키: `F4`/`F3`/`F2`/`F1` 스로틀 최대/조금 올림/조금 내림/아이들, `2`/`1` 플랩 내림/올림, `B` 브레이크, `↓`/`↑` 기수 올림/내림, `←`/`→` 롤, `,`/`.` 러더, `P` 일시정지, `Shift+9` 조종석 시점. 화면 빈 곳 클릭 = 마우스 요크(흰 네모) 켜짐/끄기.
- 단발기 특성상 풀스로틀을 넣으면 기수가 왼쪽으로 돌아가므로 `.`(오른쪽 러더)를 톡톡 눌러 잡아야 함. 비행 중 `F1`은 아이들까지 한 번에 내려가니 쓰지 말 것.

### 5.4 AI가 출발하지 않을 때
ATC 창이 "Control … reading you 1"에서 멈추면 관제탑 없는 위치라 허가를 못 받는 상태. 관제탑 있는 공항의 Runway에서 다시 시작하거나, ATC 창의 클립보드(Flight Plan)에서 목적지를 넣고 IFR로 파일링한 뒤 AI를 켜면 됩니다.

---

## 6. 수집 데이터 설명

`logs\flight_*.csv` 열(총 41열):

| 열 | 의미 |
|---|---|
| seq, wall_time, wall_epoch | 행 번호, PC 시각 |
| n_pairs | 그 주기에 받은 DataRef 개수(37이면 완전 수신) |
| airspeed_kias, altitude_ft, vs_fpm, heading_mag_deg | 속도계(KIAS), 고도계(ft MSL), 승강계(fpm), 자방위 |
| pitch_deg, roll_deg, throttle_ratio, engine_rpm, flap_ratio, on_ground | 자세, 스로틀 0~1, RPM, 플랩 0~1(0.33=10°), 접지 |
| agl_m, groundspeed_ms, elevation_msl_m, latitude, longitude | 지표고도(m), 대지속도(m/s), 해면고도(m), 위경도 |
| sim_time_s, paused | 시뮬 시각, 일시정지 |
| dest_dist_nm, dest_brg_deg, dest_ete_min, dest_id_0~7 | GPS 목적지 거리·방위·예상시간, 식별자(문자 코드) |
| fuel_kg_0, fuel_kg_1, fuel_flow_kgs | 연료 탱크 2개(kg), 유량(kg/s) |
| wind_spd_ms, wind_dir_deg, wind_spd_kt_old, wind_dir_deg_old | 바람(X-Plane 12 이름과 구형 이름, 둘 중 들어오는 것 사용) |
| oil_temp_c, bus_volts | 오일 온도, 버스 전압 |

주의할 특성
- `sim_time_s`가 0으로 되돌아가면 X-Plane 리셋(추락 후 재시작 등). `flight_state.py`가 `flight_id`를 +1 함.
- 속도계는 약 5 kt 이하에서 0으로 읽히므로 **지상 판정은 groundspeed** 사용.
- 2026-09 로그 4개(`flight_20260923_*`, `flight_20260924_*`)에는 항법·연료·바람 열이 없음(17 DataRef 시절). 그 로그를 재생하면 해당 정보는 "정보 없음"으로 나옴.

---

## 7. 단계 판정과 규칙 (STEP 2~4)

### 7.1 Flight State Vector (`xp_phase/flight_state.py`)
CSV 한 행을 `FlightState`로 변환. 단위를 kt·ft·fpm·deg로 통일하고, 새 열(목적지·연료·바람)은 없으면 `None`.

### 7.2 비행단계 (`xp_phase/phase_detector.py`)
GROUND_STOP, TAXI, TAKEOFF_ROLL, CLIMB, CRUISE, DESCENT, APPROACH, GO_AROUND, LANDING_ROLL, IMPACT 10종. 핵심은 두 가지입니다.
- 지상/공중은 `on_ground`가 **1초 이상 유지**될 때만 확정(부양·착륙 때 바퀴 튕김 제거)
- 후보 단계가 **dwell**(CLIMB 2 s, CRUISE 8 s, DESCENT·APPROACH 3 s …) 동안 지속돼야 전이(판정 떨림 제거)

임계값은 파일 상단 `Thresholds` 한 곳에 있습니다(GS 정지 2 kt, 이륙 스로틀 0.8, VS ±300 fpm, 접근 1,000 ft AGL, 착륙 활주 종료 25 kt 등). v0 한계: APPROACH는 "1,000 ft AGL 미만 강하"로만 정의되어 저고도 강하선회도 접근으로 잡힘.

### 7.3 규칙 (`xp_phase/rule_engine.py`)
규칙 20개가 `RULES` 표에 선언되어 있고 각 규칙에 근거(POH 쪽수 또는 설계값)가 적혀 있습니다. 조건이 `dwell_s` 지속되면 ONSET, 해소되면 CLEAR 이벤트를 내고, 활성 집합을 `status`로 유지합니다. 기준 속도는 `SPEEDS`(Cessna 172S Information Manual 원문으로 확인: 실속 48/43/40, 회전 55, Vy 74, 접근 65~75/60~70, Vfe 110/85, 정지 RPM 2,300~2,400 등).

| 규칙 | 뜻 |
|---|---|
| R01 저속(실속×1.1) · R02/R03 접근속도 초과/미달 · R04 플랩 과속 | 속도 |
| R05 저고도 강하율 · R06 저고도 경사 · R13 강한 접지 · R14 접지속도 과대 | 자세·강하 |
| R07 상승 중 출력 상실 · R17 이륙 출력 부족 · R20 저전압 | 출력·계통 |
| R08 조기 회전 · R09 상승속도 편차 · R10/R11 순항 고도/방위 편차 | 절차 편차 |
| R12 복행 발생 · R15 500 ft 안정화 게이트 · R16 복행 중 플랩 미회수 | 접근·복행 |
| R18 연료 부족 · R19 측풍 15 kt 초과 | 연료·기상 |

실행: `py run_rule_engine.py ..\logs\파일.csv --out situation` → `situation\파일_events.txt`에 시간순 이벤트, `_situation.csv`에 샘플별 phase와 active_rules.

---

## 8. POH 지식베이스와 검색 (STEP 5)

- `build_kb.py`: PDF 텍스트를 절·제목 단위로 자르고, 제목 키워드로 비행단계 태그를 붙여 `kb/chunks.jsonl`(377 청크, 체크리스트 48)을 만듦.
- `kb_retriever.py`의 `KB.retrieve(질의, phase, k)`: BM25(어휘) + 제목 일치 보너스 + **현재 단계 보너스** + 체크리스트 보너스. 한국어 질의는 `KO2EN` 표로 영문 검색어를 보강(착륙→landing 등). 키워드가 없는 질의는 그 단계의 체크리스트로 폴백.
- `KB.format_context(hits)`: 출처(절·제목·쪽수)를 붙인 근거 블록 생성 → LLM 입력.
- (선택) `ollama pull bge-m3` 후 `py build_embeddings.py`를 돌리면 의미 검색이 합쳐짐(미검증).

시험: `py query_kb.py "복행 절차 알려줘" --phase GO_AROUND` → BALKED LANDING이 1위면 정상.

---

## 9. LLM 연동 (STEP 6~7)

### 9.1 Context Package
LLM에 들어가는 입력은 다음을 하나로 묶은 텍스트입니다(`run_assist_replay.py`의 `build_context`).
```
[현재 비행 상태]  속도·고도·승강률·자세·출력·플랩·접지
[항법 · 연료 · 기상 · 계통]  목적지·거리·연료·바람·오일·전압 (있을 때)
[비행단계]  APPROACH
[활성 경고(status)]  R03 … 근거
[최근 이벤트 60 s]
[판단 힌트 (규칙 계산값)]  연료 충분/부족, 속도 밴드 대비, 측풍 대비 등 코드가 계산한 결론
[규칙 답변]  질문 유형별로 코드가 만든 정답 문장
[POH 근거]  검색된 절차 원문(출처 포함)
[조종사 질의]
```

### 9.2 응답 구조(라이브 창 기준)
1. **규칙 답변(즉시)** — 질문 유형(연료·목적지·바람·속도·출력·플랩·체크리스트·절차·일반)을 코드가 분류해 정답을 0초에 표시
2. **AI 보충 설명** — LLM이 스트리밍으로 이유·주의·다음 행동을 1~3문장 덧붙임(규칙 답 반복 금지, 한국어만)
3. **[검증]** — 인용한 쪽수·절이 근거 안에 있는지, 숫자가 컨텍스트 값인지, 지상/공중 모순이 없는지 자동 검사 → 통과/주의 표시. 중국어가 섞이면 한국어로 자동 재생성.

### 9.3 저장 로그 재생 (X-Plane 불필요)
```
cd xp_llm
py run_assist_replay.py --model qwen2.5:7b-instruct
py run_assist_replay.py --model qwen2.5:7b-instruct --at flight_20260924_002110.csv:2100 --q "접근 속도 적절해?"
```
결과: `out\assist_replay_*.md`(시점별 상태·경고·근거·규칙 답·LLM 보충·검증·Context Package 전문)와 `.jsonl`.

### 9.4 실시간 화면
```
cd xp_llm
py live_assist.py --model qwen2.5:7b-instruct                 # X-Plane 비행 화면이 떠 있어야 함
py live_assist.py --replay ..\logs\flight_20260924_002110.csv --speed 20 --model qwen2.5:7b-instruct   # X-Plane 없이 재생
```
창 구성: 실시간 값 → 현재 단계(색) + 활성 경고 → 이벤트 로그 → 질문 입력 + "AI 응답 생성". "항상 위" 체크로 X-Plane 위에 띄울 수 있고, "WARNING 발생 시 자동 응답"을 켜면 경고 때 자동으로 코칭을 요청합니다. 실행 중 `logs\`에 CSV도 저장됩니다. **수집기(`xp_collector.py`)와 동시에 켜지 말 것**(같은 49005 포트).

---

## 10. 시연 순서 (검증된 구성)

1. Ollama 서버 확인(`ollama list`) → 라이브 창에서 응답 한 번 생성해 모델을 메모리에 올려 둠
2. X-Plane: New Flight → Skyhawk → 관제탑 있는 공항 Runway 시작 → 조종석 로딩
3. `py live_assist.py --model qwen2.5:7b-instruct` → 상태줄에 "수신 n행" 증가 확인 → 창을 두 번째 모니터로
4. X-Plane에서 AI 자율비행 켜기 → 단계 전이와 이벤트가 로그에 쌓이는 것 보여주기
5. 질문 예: 순항 중 "연료 충분해?"(규칙 즉시) → 접근 중 "접근 속도 적절해?"(규칙+경고) → "지금 착륙해도 괜찮아?"(LLM 종합) → [검증] 줄 가리키기
6. 백업: X-Plane 녹화 영상과 `out\assist_replay_*.md`

---

## 11. 문제 해결

| 증상 | 원인·조치 |
|---|---|
| `WinError 10054` 또는 "받는 프로그램이 없음" | X-Plane이 안 떠 있거나 비행 화면이 아님. 수집기는 재시도하므로 X-Plane을 띄우면 자동 연결 |
| "수신 없음"이 계속 | 다른 수집기/라이브 창이 49005 포트를 잡고 있음 → 하나만 실행하거나 `--local-port 49006` |
| 일부 DataRef 미수신 경고 | 바람 구형 이름 등 해당 X-Plane 버전에 없는 항목. 값은 공란으로 저장되고 나머지는 정상 |
| `WinError 10061` (LLM 호출) | Ollama 서버가 꺼짐 → Ollama 앱 실행 또는 `ollama serve` |
| 모델 이름 오류 | `ollama list`에 보이는 이름 그대로 `--model`에 지정 |
| 응답이 중국어 | 자동 재생성됨. 반복되면 `--model llama3.1:8b` |
| 응답이 중간에 끊김 | 생성 길이 상한(320 토큰)을 `run_assist_replay.py`의 `num_predict`에서 조정 |
| AI 자율비행이 안 움직임 | 5.4항. ATC 허가 대기 |
| AI를 공중에서 켰더니 급강하 | 지상에서 켜서 끝까지 맡길 것. 공중 전환은 1,500 ft AGL·80 kt 이상에서 `F4` 후 |
| 라이브 창이 X-Plane 뒤로 숨음 | X-Plane을 창 모드로, 라이브 창 "항상 위" 체크 |
| `ModuleNotFoundError: flight_state` | 폴더 구조(3항)가 다름. `live_assist.py`는 시작 시 어디서 모듈을 찾았는지 4줄 출력하므로 그걸 확인 |

---

## 12. 현재 한계와 다음 과제

- 단계 정의 v0: APPROACH에 착륙 형태(플랩·출력·활주로 정렬)가 반영되지 않음 → 공항 데이터(`apt.dat`)로 활주로 상대 위치 추가
- 규칙 임계값 중 설계값(저고도 강하율 1,000 fpm, 경사 30°, 500 ft 게이트)은 ACS 원문 대조 미완
- 검색은 어휘 기반 → 임베딩 하이브리드(bge-m3) 평가
- LLM 평가 지표 없음 → 정답 라벨이 있는 시나리오 데이터(조이스틱으로 의도적 편차 재현) 확보 후 검출률·오탐률·응답 정확도 산출
- X-Plane AI 자율비행은 POH 속도 밴드 밖(접근 52~55 kt)에서 비행하므로 "정상 기준"으로 쓰면 안 됨. 기준은 POH/ACS
- 비행 후 리포트(전체 이벤트를 훈련 피드백으로 요약) 기능 미구현

---

## 13. 참고 자료
- 문서: `docs/` 안의 보고 PPT, 발표 가이드, 학습 노트(용어 설명), 그림(fig1~9)
- 기준 문서: Cessna 172S Information Manual(Rev.5, 2004), FAA Private Pilot ACS
- X-Plane UDP(RREF) 프로토콜: X-Plane 설치 폴더 `Instructions/` 와 `Resources/plugins/DataRefs.txt`
