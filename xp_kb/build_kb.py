#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_kb.py — STEP 5: Cessna 172S POH → 지식베이스(chunks.jsonl)

  py build_kb.py POH-Cessna-172S.pdf --out kb            (pdftotext 필요: poppler)
  py build_kb.py poh_layout.txt --out kb                  (이미 추출한 텍스트도 가능)

처리
  1. 페이지별 텍스트 → 헤더/푸터(CESSNA, MODEL 172S, 쪽번호, 개정일) 제거, 절(section) 번호 인식
  2. 대문자 제목 행을 기준으로 청크 분할 (상위 제목 > 하위 제목 형태로 경로 유지)
  3. 제목 키워드로 비행단계 태그(phase_tags) 부여 → 단계 인지형 검색에 사용
  4. 긴 청크는 문단 단위로 분할 (최대 약 1,200자)
출력: kb/chunks.jsonl  (id, section, heading, path, page, phase_tags, kind, text)
"""
import argparse, json, os, re, subprocess, sys

PHASE_MAP = [   # (제목에 포함된 키워드, 부여할 phase 태그)
    (r"PREFLIGHT|BEFORE STARTING|STARTING ENGINE|BEFORE TAXI|SECURING", ["GROUND_STOP"]),
    (r"\bTAXI", ["TAXI"]),
    (r"BEFORE TAKE ?OFF", ["GROUND_STOP", "TAXI"]),
    (r"TAKE ?OFF ROLL|DURING TAKE ?OFF", ["TAKEOFF_ROLL"]),
    (r"AFTER TAKE ?OFF", ["CLIMB"]),
    (r"TAKE ?OFF", ["TAKEOFF_ROLL", "CLIMB"]),
    (r"CLIMB", ["CLIMB"]),
    (r"CRUISE", ["CRUISE"]),
    (r"DESCENT", ["DESCENT"]),
    (r"BEFORE LANDING|APPROACH", ["APPROACH"]),
    (r"BALKED|GO.?AROUND", ["GO_AROUND"]),
    (r"AFTER LANDING", ["LANDING_ROLL", "TAXI"]),
    (r"LANDING", ["APPROACH", "LANDING_ROLL"]),
    (r"STALL|SPIN", ["CLIMB", "CRUISE", "DESCENT", "APPROACH"]),
    (r"ENGINE FAILURE DURING FLIGHT|FORCED LANDING|MAXIMUM GLIDE|DITCHING|FIRE|ICING|STATIC SOURCE|ELECTRICAL|ROUGH ENGINE|LOSS OF POWER|EMERGENCY", ["CLIMB", "CRUISE", "DESCENT", "APPROACH"]),
]
HEADER_RX = re.compile(r"^\s*(CESSNA|MODEL 172S|SECTION\s*\d|INTRODUCTION\s*$|NORMAL PROCEDURES\s*$|EMERGENCY PROCEDURES\s*$|LIMITATIONS\s*$|PERFORMANCE\s*$|GENERAL\s*$|WEIGHT & BALANCE|AIRPLANE & SYSTEMS|HANDLING, SERVICE|SUPPLEMENTS)")
FOOTER_RX = re.compile(r"(^\s*I?\s*Revision \d|^\s*\d+-\d+\s*I?$|July \d+/\d+|May \d+/\d+|^\s*U\.S\.\s*$|^\s*I?\s*\d+-\d+\s+Revision|Revision \d\s+\d+-\d+|^\s*I\s*$)")
SECTION_RX = re.compile(r"SECTION\s*(\d)")
HEADING_RX = re.compile(r"^[A-Z0-9][A-Z0-9 ,'&/()\-\.]{3,70}$")


def page_texts(path):
    if path.lower().endswith(".pdf"):
        txt = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True, text=True, check=True).stdout
    else:
        txt = open(path, encoding="utf-8").read()
    return txt.split("\f")


INLINE_CAPS = {"NOTE", "WARNING", "CAUTION"}


def is_caps(line):
    t = line.strip()
    letters = sum(ch.isalpha() for ch in t)
    return letters >= 4 and t == t.upper() and letters / max(1, len(t.replace(" ", ""))) > 0.6


def is_heading(line, next_line=""):
    t = line.strip()
    if t in INLINE_CAPS or not HEADING_RX.match(t) or len(t.split()) > 6:
        return False
    if re.search(r"\d\.\s", t) or t.endswith(".") or t.endswith(":") or "MODEL 172S" in t or t.startswith("CESSNA"):
        return False
    if next_line.strip() and is_caps(next_line) and len(next_line.strip()) > 25:
        return False          # 대문자 경고문 문단의 첫 줄
    return is_caps(t)


def phase_tags(heading_path):
    tags = []
    for rx, ph in PHASE_MAP:
        if re.search(rx, heading_path, re.I):
            for p in ph:
                if p not in tags:
                    tags.append(p)
            break
    return tags


def chunk_kind(text):
    toc_lines = re.findall(r"(?:\. ){3,}.*\b\d{1,2}-\d{1,3}\s*$|\.{4,}.*\b\d{1,2}-\d{1,3}\s*$", text, re.M)
    if len(toc_lines) >= 3 or "TABLE OF CONTENTS" in text:
        return "toc"                                   # 목차 페이지(점선 뒤 쪽번호): 검색에서 제외
    steps = len(re.findall(r"^\s*\d{1,2}\.\s", text, re.M))
    return "checklist" if steps >= 3 else "text"


def build(pages, max_chars=1200):
    chunks, section = [], None
    parent, heading, buf, page_of_heading = "", "", [], 1
    page_top = {}          # page → 그 페이지의 첫 제목 (상위 제목이 없는 청크의 문맥으로 사용)

    def flush():
        text = "\n".join(l.rstrip() for l in buf).strip()
        text = re.sub(r"[ \t]+", " ", text)
        if len(text) < 25 or not heading:
            return
        path = f"{parent} > {heading}" if parent and parent != heading else heading
        tags = phase_tags(path)
        top = page_top.get(page_of_heading, "")
        if not tags and top and top not in path:       # 제목만으로 단계를 알 수 없는 표/노트 → 페이지 첫 제목을 문맥으로
            path = f"{top} > {path}"
            tags = phase_tags(path)
        paras = re.split(r"\n\s*\n", text)
        cur, parts = "", []
        for p in paras:
            if len(cur) + len(p) > max_chars and cur:
                parts.append(cur); cur = p
            else:
                cur = (cur + "\n\n" + p).strip()
        if cur:
            parts.append(cur)
        for k, part in enumerate(parts):
            chunks.append({"id": f"c{len(chunks)+1:04d}", "section": section, "heading": heading, "path": path,
                           "page": page_of_heading, "phase_tags": tags, "kind": chunk_kind(part),
                           "text": part if k == 0 else f"[{path} (cont.)]\n" + part})

    for pno, ptxt in enumerate(pages, start=1):
        lines = ptxt.splitlines()
        head = " ".join(lines[:3])
        m = SECTION_RX.search(head)
        if m:
            section = int(m.group(1))
        prev_was_heading = False
        for i, raw in enumerate(lines):
            line = raw.rstrip()
            if not line.strip():
                if buf and buf[-1].strip():
                    buf.append("")
                continue
            if (i < 3 and HEADER_RX.match(line)) or "MODEL 172S" in line or line.strip().startswith("CESSNA"):
                continue
            if FOOTER_RX.search(line):
                continue
            nxt = next((l for l in lines[i + 1:i + 4] if l.strip()), "")
            if is_heading(line, nxt):
                t = re.sub(r"\s+", " ", line.strip())
                t = t.replace("TAK EOFF", "TAKEOFF").replace("TAKE OFF", "TAKEOFF")
                if prev_was_heading and not any(l.strip() for l in buf):
                    parent, heading = heading, t          # 제목이 연달아 나오면 상위>하위
                else:
                    flush(); buf = []
                    if not prev_was_heading:
                        parent = ""
                    heading = t
                page_of_heading = pno
                page_top.setdefault(pno, t)
                prev_was_heading = True
                continue
            buf.append(line)
            prev_was_heading = False
    flush()
    return chunks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src"); ap.add_argument("--out", default="kb"); ap.add_argument("--sections", default="1,2,3,4,5,7")
    a = ap.parse_args()
    keep = {int(x) for x in a.sections.split(",")}
    pages = page_texts(a.src)
    chunks = [c for c in build(pages) if c["section"] in keep]
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "chunks.jsonl"), "w", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")
    from collections import Counter
    print(f"chunks: {len(chunks)}  by section: {dict(sorted(Counter(c['section'] for c in chunks).items()))}")
    print(f"checklists: {sum(c['kind']=='checklist' for c in chunks)}  with phase tags: {sum(bool(c['phase_tags']) for c in chunks)}")


if __name__ == "__main__":
    main()
