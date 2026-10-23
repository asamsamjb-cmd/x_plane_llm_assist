#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_embeddings.py — (선택) Ollama 임베딩으로 kb/embeddings.json 생성 → 하이브리드 검색 활성화
  ollama pull bge-m3          (다국어: 한국어 질의 ↔ 영문 POH 직접 매칭)
  py build_embeddings.py --model bge-m3
※ 컨테이너에서는 Ollama 가 없어 실행 검증을 못 했음. 실패 시 오류 메시지를 보내주십시오.
"""
import argparse, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_retriever import KB

ap = argparse.ArgumentParser(); ap.add_argument("--model", default="bge-m3"); ap.add_argument("--kb", default="kb"); ap.add_argument("--host", default="http://localhost:11434")
a = ap.parse_args()
chunks = [json.loads(l) for l in open(os.path.join(a.kb, "chunks.jsonl"), encoding="utf-8")]
vecs = []
for i in range(0, len(chunks), 16):
    batch = [c["path"] + "\n" + c["text"] for c in chunks[i:i + 16]]
    vecs += KB.ollama_embed(batch, a.model, a.host)
    print(f"{min(i + 16, len(chunks))}/{len(chunks)}", end="\r")
json.dump({"model": a.model, "ids": [c["id"] for c in chunks], "vectors": vecs}, open(os.path.join(a.kb, "embeddings.json"), "w"))
print(f"\nsaved {a.kb}/embeddings.json ({len(vecs)} vectors, dim {len(vecs[0])})")
