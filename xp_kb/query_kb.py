#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
query_kb.py — 검색 테스트 CLI
  py query_kb.py "before landing checklist" --phase APPROACH
  py query_kb.py "착륙 전에 뭘 확인해야 해?" --phase APPROACH -k 3
  py query_kb.py "engine failure after takeoff" --phase CLIMB --context   # LLM 컨텍스트 블록 출력
"""
import argparse, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_retriever import KB, expand_query

ap = argparse.ArgumentParser()
ap.add_argument("query"); ap.add_argument("--phase", default=None); ap.add_argument("-k", type=int, default=4)
ap.add_argument("--kb", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "kb"))
ap.add_argument("--context", action="store_true")
a = ap.parse_args()
kb = KB(a.kb)
print(f"query: {a.query!r}  phase={a.phase}  expanded: {expand_query(a.query)}")
hits = kb.retrieve(a.query, phase=a.phase, k=a.k)
if a.context:
    print(KB.format_context(hits))
else:
    for h in hits:
        print(f"\n[{h['score']:5.2f}] §{h['section']} p.{h['page']} {h['kind']:<9} {h['phase_tags']}  {h['path']}")
        print("   " + h["text"][:260].replace("\n", " | "))
