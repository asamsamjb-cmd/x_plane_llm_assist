# -*- coding: utf-8 -*-
"""
kb_retriever.py — STEP 5: 단계 인지형(phase-aware) POH 검색기

  from kb_retriever import KB
  kb = KB("kb")                                   # chunks.jsonl (+ embeddings.json 있으면 하이브리드)
  hits = kb.retrieve("before landing checklist", phase="APPROACH", k=3)
  print(KB.format_context(hits))                  # STEP 6 LLM Context Package 에 그대로 삽입

검색 점수 = BM25(어휘) [+ 코사인(임베딩, 선택)] + phase 보너스(청크 phase_tags 에 현재 단계 포함) + kind 보너스
  · BM25 는 표준 라이브러리만으로 구현 (외부 패키지 불필요)
  · 임베딩은 Ollama 의 /api/embed (예: bge-m3, nomic-embed-text) 를 쓰며, build_embeddings.py 로 미리 계산
  · 한국어 질의는 KO2EN 키워드 표로 영문 검색어를 보강 (v0 임시책; 다국어 임베딩이 붙으면 불필요)
"""
import json, math, os, re, urllib.request
from collections import Counter
from typing import Dict, List, Optional

PHASE_BONUS = 1.5       # 현재 단계와 태그가 맞는 청크 가산점 (정규화 점수 0~10 단위)
KIND_BONUS = 1.0        # 질의가 절차/체크리스트를 요구하고 청크가 checklist 인 경우
HEADING_BONUS = 5.0     # 질의 핵심어(의도어 제외)가 청크 제목 경로에 포함되는 비율 × 이 값
TOKEN_RX = re.compile(r"[a-z0-9]+")
STOP = {"the", "a", "an", "of", "to", "and", "or", "in", "on", "for", "is", "are", "be", "with", "at", "by", "as", "if", "it", "this", "that", "should", "may", "will", "any"}
SYN = {  # 검색어 확장
    "go": ["balked"], "around": ["balked"], "goaround": ["balked", "landing"],
    "checklist": ["procedure", "procedures"], "procedure": ["checklist", "procedures"],
    "land": ["landing"], "stall": ["stalls", "stall"], "flap": ["flaps"], "flaps": ["flap"],
    "speed": ["airspeed", "kias"], "airspeed": ["speed", "kias"], "rpm": ["power"], "glide": ["glide", "maximum"],
}
KO2EN = [  # (한국어 키워드, 영문 검색어) — 긴 키워드부터 매칭
    ("이륙 직후", "immediately after takeoff"), ("이륙직후", "immediately after takeoff"), ("비상착륙", "forced landing emergency landing without engine power"),
    ("무동력", "without engine power"), ("예방착륙", "precautionary landing"), ("단거리", "short field"), ("측풍", "crosswind"),
    ("체크리스트", "checklist"), ("점검표", "checklist"), ("절차", "procedure"), ("복행", "balked landing go around"),
    ("실속", "stall"), ("스핀", "spin"), ("플랩", "flaps"), ("이륙", "takeoff"), ("착륙", "landing"), ("접근", "approach"),
    ("상승률", "rate of climb"), ("강하율", "rate of descent"), ("상승", "climb"), ("순항", "cruise"), ("강하", "descent"),
    ("활공", "glide"), ("택시", "taxi"), ("활주", "roll"), ("시동", "starting engine"), ("엔진 고장", "engine failure"), ("엔진고장", "engine failure"),
    ("엔진", "engine"), ("고장", "failure"), ("화재", "fire"), ("결빙", "icing"), ("연료", "fuel"), ("혼합기", "mixture"), ("믹스처", "mixture"),
    ("스로틀", "throttle"), ("브레이크", "brakes"), ("트림", "trim"), ("비상", "emergency"), ("정상", "normal"),
    ("속도", "airspeed speed"), ("고도", "altitude"), ("제한", "limitations"), ("성능", "performance"), ("중량", "weight"),
    ("선회", "turn bank"), ("경사", "bank"), ("회전", "rotate nose wheel"), ("주의사항", "caution warning"), ("확인", "check"),
    ("전에", "before"), ("전 ", "before "), ("후에", "after"), ("동안", "during"), ("중에", "during"),
]


INTENT = {"checklist", "procedure", "procedures", "check", "what", "how", "tell", "me", "show", "list", "step", "steps"}


def stem(t: str) -> str:
    """아주 단순한 어간 추출: 복수형 → 파생접미사 순으로 제거 (speed/need 류는 보호)."""
    if len(t) <= 4 or t.isdigit():
        return t
    if t.endswith("s") and not t.endswith("ss") and len(t) > 4:
        t = t[:-2] if t.endswith("ies") else t[:-1]
    if t.endswith("eed") or len(t) <= 4:
        return t
    for suf in ("ation", "ing", "ed"):
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            return t[: -len(suf)]
    return t


def tokenize(text: str) -> List[str]:
    return [stem(t) for t in TOKEN_RX.findall(text.lower()) if t not in STOP]


def expand_query(q: str) -> str:
    extra = []
    for ko, en in KO2EN:
        if ko in q:
            extra.append(en)
    words = q + " " + " ".join(extra)
    toks = tokenize(words)
    for t in list(toks):
        toks += SYN.get(t, [])
    return " ".join(toks)


class BM25:
    def __init__(self, docs: List[List[str]], k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.docs = docs
        self.dl = [len(d) for d in docs]
        self.avgdl = sum(self.dl) / max(1, len(docs))
        self.tf = [Counter(d) for d in docs]
        df = Counter()
        for d in docs:
            df.update(set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def score(self, q: List[str], i: int) -> float:
        s, tf, dl = 0.0, self.tf[i], self.dl[i]
        for t in q:
            if t in tf:
                f = tf[t]
                s += self.idf.get(t, 0.0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
        return s


class KB:
    def __init__(self, kb_dir: str = "kb"):
        self.dir = kb_dir
        self.chunks = [json.loads(l) for l in open(os.path.join(kb_dir, "chunks.jsonl"), encoding="utf-8")]
        self.bm25 = BM25([tokenize(c["path"] + " " + c["path"] + " " + c["text"]) for c in self.chunks])
        self.path_toks = [set(tokenize(c["path"])) for c in self.chunks]
        self.emb = None
        p = os.path.join(kb_dir, "embeddings.json")
        if os.path.exists(p):
            e = json.load(open(p, encoding="utf-8"))
            if e.get("ids") == [c["id"] for c in self.chunks]:
                self.emb = e
        self.embed_model = self.emb["model"] if self.emb else None

    # ---- optional Ollama embeddings ------------------------------------
    @staticmethod
    def ollama_embed(texts: List[str], model: str, host: str = "http://localhost:11434") -> List[List[float]]:
        req = urllib.request.Request(host + "/api/embed", data=json.dumps({"model": model, "input": texts}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())["embeddings"]

    @staticmethod
    def _cos(a, b):
        num = sum(x * y for x, y in zip(a, b)); da = math.sqrt(sum(x * x for x in a)); db = math.sqrt(sum(y * y for y in b))
        return num / (da * db) if da and db else 0.0

    # ---- retrieval -------------------------------------------------------
    def retrieve(self, query: str, phase: Optional[str] = None, k: int = 4, sections: Optional[List[int]] = None,
                 want_checklist: Optional[bool] = None) -> List[Dict]:
        q_all = tokenize(expand_query(query))
        q = [t for t in q_all if t not in INTENT] or q_all      # 의도어(checklist 등)는 BM25 에서 제외, kind 보너스로 처리
        if want_checklist is None:
            want_checklist = any(w in query.lower() for w in ("checklist", "procedure", "체크리스트", "절차", "점검", "순서"))
        bm = [self.bm25.score(q, i) for i in range(len(self.chunks))]
        top = max(bm) or 1.0
        cos = None
        if self.emb:
            try:
                qv = self.ollama_embed([query], self.emb["model"])[0]
                cos = [self._cos(qv, v) for v in self.emb["vectors"]]
            except Exception:
                cos = None
        core = [t for t in set(q) if t not in INTENT and t not in STOP]
        vague = not core or top == 0.0                          # 검색어가 없으면 단계·절차 중심으로 폴백
        scored = []
        for i, c in enumerate(self.chunks):
            if c["kind"] == "toc" or (sections and c["section"] not in sections):
                continue
            if vague:
                pb = PHASE_BONUS if phase and phase in c["phase_tags"] else 0.0
                if pb and len(c["phase_tags"]) >= 4:
                    pb *= 0.5                                       # 실속·비상처럼 여러 단계에 걸친 일반 항목은 절반 가산
                s = pb + (KIND_BONUS if c["kind"] == "checklist" else 0.0) \
                    + (2.0 if c["section"] in (3, 4) else 0.0) + (0.5 if c["section"] == 4 else 0.0)
                if s > 0:
                    scored.append((s, i))
                continue
            s = bm[i] / top * 10.0                              # BM25 를 0~10 으로 정규화
            if cos is not None:
                s = 0.5 * s + 0.5 * max(0.0, cos[i]) * 10.0
            if core:
                s += HEADING_BONUS * sum(t in self.path_toks[i] for t in core) / len(core)
            if phase and phase in c["phase_tags"]:
                s += PHASE_BONUS
            if want_checklist and c["kind"] == "checklist":
                s += KIND_BONUS
            if s > 0:
                scored.append((s, i))
        scored.sort(reverse=True)
        out = []
        for s, i in scored[:k]:
            d = dict(self.chunks[i]); d["score"] = round(s, 2); out.append(d)
        return out

    @staticmethod
    def format_context(hits: List[Dict], max_chars: int = 3500) -> str:
        """LLM 프롬프트에 넣을 근거 블록. 각 청크에 출처(절/제목/쪽)를 붙인다."""
        parts, used = [], 0
        for h in hits:
            head = f"[POH §{h['section']} | {h['path']} | p.{h['page']} | {h['kind']}]"
            body = h["text"]
            if used + len(body) > max_chars:
                body = body[: max(0, max_chars - used)] + " ..."
            parts.append(head + "\n" + body)
            used += len(body)
            if used >= max_chars:
                break
        return "\n\n".join(parts)
