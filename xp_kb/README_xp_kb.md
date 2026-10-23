# xp_kb — STEP 5: POH/Checklist 지식베이스 + 단계 인지형 검색 (RAG v0)

| 파일 | 내용 |
|---|---|
| `build_kb.py` | POH PDF(또는 pdftotext 결과) → `kb/chunks.jsonl`. 절·제목 기준 청크, 목차 제외, 제목 키워드로 phase 태그 부여 |
| `kb_retriever.py` | `KB` 클래스: BM25(표준 라이브러리) + 제목 일치 보너스 + phase 보너스 + checklist 보너스. 한국어 질의는 키워드 표(KO2EN)로 영문 검색어 보강. `KB.format_context()`가 LLM 컨텍스트 블록 생성 |
| `query_kb.py` | 검색 테스트 CLI |
| `build_embeddings.py` | (선택) Ollama 임베딩(bge-m3 등)으로 하이브리드 검색 활성화 — 컨테이너에 Ollama가 없어 미검증 |
| `kb/chunks.jsonl` | Cessna 172S Information Manual(Rev.5, 2004) §1,2,3,4,5,7 → 377 청크 (체크리스트 48, phase 태그 125) |

실행

```
cd C:\xp_collector\xp_kb
py query_kb.py "착륙 전에 뭘 확인해야 해?" --phase APPROACH
py query_kb.py "engine failure right after takeoff" --phase CLIMB --context
py build_kb.py C:\path\POH-Cessna-172S.pdf --out kb        # 재생성 시 (poppler 의 pdftotext 필요)
```

검증(2026-09-24, 질의 12건): 영문/한국어 질의 모두 1위 결과가 해당 절차·표였음.
같은 질의라도 phase 가 다르면 결과가 달라짐 — 예: "현재 상태에서 주의할 점" → APPROACH 이면 NORMAL/SHORT FIELD LANDING, TAKEOFF_ROLL 이면 NORMAL/SHORT FIELD TAKEOFF·ENGINE FAILURE DURING TAKEOFF ROLL.

v0 한계
- 어휘 검색이라 키워드가 없는 자연어 질의는 phase 기반 폴백으로만 답함 → `ollama pull bge-m3` 후 `build_embeddings.py` 로 다국어 의미검색 추가 예정
- 청크 경계는 대문자 제목 휴리스틱에 의존(OCR 텍스트). 일부 경고문 문단이 제목으로 잡힘
- `kb/chunks.jsonl` 은 POH 원문을 담고 있어 연구 내부용으로만 사용(재배포 금지)
