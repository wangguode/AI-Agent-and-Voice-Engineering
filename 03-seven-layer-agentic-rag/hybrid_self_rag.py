#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
企业级七层 RAG 核心引擎：BM25 + 稠密向量混合检索 (RRF) 与 Self-RAG 自省闭环
=========================================================================
对应知乎 @王果ai 专栏：
- 《RAG篇(1)：RAG的星辰大海——从朴素检索到Agentic范式，重构企业级RAG的七层架构》
- 《Agent篇(25)：混合检索实战 (Hybrid Search & Reranking) —— 打造工业级搜索引擎》
- 《Agent篇(26)：自省式检索 (Self-RAG) —— 我知道我不知道：构建可信赖的 RAG 闭环》

运行方式：
    python3 hybrid_self_rag.py
"""

from __future__ import annotations
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass
class KnowledgeChunk:
    doc_id: str
    title: str
    content: str
    dense_embedding: List[float]


class HybridSelfRagPipeline:
    """
    融合稀疏检索 (BM25) + 稠密向量余弦检索 + 倒数秩融合 (RRF) + Self-RAG 自省校验的工业级检索管道
    """

    def __init__(self, chunks: List[KnowledgeChunk], rrf_k: int = 60) -> None:
        self.chunks = chunks
        self.rrf_k = rrf_k
        self.tokenized_corpus = [self._tokenize(c.title + " " + c.content) for c in chunks]
        self.avg_dl = sum(len(tokens) for tokens in self.tokenized_corpus) / max(1, len(chunks))
        self.doc_freqs = self._compute_doc_freqs()

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        # 同时切分英文词元与中文双字 Gram，零外部依赖即可在本地精准复现混合检索
        en_words = [w.lower() for w in re.findall(r"[a-zA-Z0-9_-]+", text)]
        zh_chars = re.findall(r"[\u4e00-\u9fff]", text)
        zh_bigrams = [zh_chars[i] + zh_chars[i + 1] for i in range(len(zh_chars) - 1)]
        return en_words + zh_chars + zh_bigrams

    def _compute_doc_freqs(self) -> Dict[str, int]:
        df: Dict[str, int] = Counter()
        for tokens in self.tokenized_corpus:
            for token in set(tokens):
                df[token] += 1
        return df

    def sparse_bm25_rank(self, query: str, k1: float = 1.5, b: float = 0.75) -> List[Tuple[str, float]]:
        q_tokens = self._tokenize(query)
        n_docs = len(self.chunks)
        scores: List[Tuple[str, float]] = []

        for chunk, doc_tokens in zip(self.chunks, self.tokenized_corpus):
            tf_counter = Counter(doc_tokens)
            dl = len(doc_tokens)
            score = 0.0
            for qt in q_tokens:
                df = self.doc_freqs.get(qt, 0)
                if df == 0:
                    continue
                idf = math.log(1.0 + (n_docs - df + 0.5) / (df + 0.5))
                tf = tf_counter.get(qt, 0)
                numer = tf * (k1 + 1.0)
                denom = tf + k1 * (1.0 - b + b * (dl / self.avg_dl))
                score += idf * (numer / denom)
            scores.append((chunk.doc_id, score))

        return sorted(scores, key=lambda x: x[1], reverse=True)

    @staticmethod
    def _cosine_sim(v1: List[float], v2: List[float]) -> float:
        dot = sum(a * b for a, b in zip(v1, v2))
        n1 = math.sqrt(sum(a * a for a in v1))
        n2 = math.sqrt(sum(b * b for b in v2))
        return dot / (n1 * n2 + 1e-9)

    def dense_vector_rank(self, query_vec: List[float]) -> List[Tuple[str, float]]:
        scores = [(c.doc_id, self._cosine_sim(query_vec, c.dense_embedding)) for c in self.chunks]
        return sorted(scores, key=lambda x: x[1], reverse=True)

    def retrieve_with_self_reflection(
        self, query: str, query_vec: List[float], top_k: int = 3
    ) -> List[Tuple[KnowledgeChunk, float, str]]:
        """
        1. 稀疏 BM25 + 稠密向量检索
        2. Reciprocal Rank Fusion (RRF) 混合重排
        3. Self-RAG 相关性与证据支撑度自省评估 ([Relevant] / [Partially Supported] / [Rejected])
        """
        sparse_ranked = self.sparse_bm25_rank(query)
        dense_ranked = self.dense_vector_rank(query_vec)

        rrf_scores: Dict[str, float] = Counter()
        for rank_idx, (doc_id, _) in enumerate(sparse_ranked, start=1):
            rrf_scores[doc_id] += 1.0 / (self.rrf_k + rank_idx)
        for rank_idx, (doc_id, _) in enumerate(dense_ranked, start=1):
            rrf_scores[doc_id] += 1.0 / (self.rrf_k + rank_idx)

        chunk_map = {c.doc_id: c for c in self.chunks}
        q_tokens = set(self._tokenize(query))

        results: List[Tuple[KnowledgeChunk, float, str]] = []
        for doc_id, rrf_score in sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)[:top_k]:
            chunk = chunk_map[doc_id]
            doc_tokens = set(self._tokenize(chunk.title + " " + chunk.content))
            overlap = len(q_tokens & doc_tokens) / max(1, len(q_tokens))

            if overlap >= 0.25:
                verdict = "✅ [Self-RAG: HIGH_SUPPORT 强证据支撑]"
            elif overlap >= 0.10:
                verdict = "⚠️ [Self-RAG: PARTIAL_SUPPORT 需触发二次改写检索]"
            else:
                verdict = "❌ [Self-RAG: IRRELEVANT 过滤剔除防幻觉]"
            results.append((chunk, rrf_score, verdict))

        return results


if __name__ == "__main__":
    sample_kb = [
        KnowledgeChunk(
            doc_id="DOC-VOICE-01",
            title="FreeSWITCH 与 RTP 流式语音打断架构",
            content="通过解析 RFC 3550 RTP 音频包并计算 PCM 帧 RMS 能量，在 60ms 内触发 Barge-in 清空下行 TTS 缓冲。",
            dense_embedding=[0.92, 0.85, 0.12, 0.05],
        ),
        KnowledgeChunk(
            doc_id="DOC-HARNESS-02",
            title="Codex 式编码智能体 Harness 与任务循环收敛",
            content="Agent OS 的核心不是提示词，而是 Loop 收敛守卫、工具沙箱隔离、时间旅行快照与账单熔断。",
            dense_embedding=[0.15, 0.20, 0.95, 0.88],
        ),
        KnowledgeChunk(
            doc_id="DOC-RAG-03",
            title="企业级七层 RAG 混合检索与 GraphRAG",
            content="结合 Elasticsearch BM25 稀疏检索与 Milvus 稠密向量检索，利用 RRF 融合与 Self-RAG 闭环消除幻觉。",
            dense_embedding=[0.45, 0.60, 0.55, 0.40],
        ),
    ]

    pipeline = HybridSelfRagPipeline(sample_kb)
    query_text = "如何用 FreeSWITCH 和 RTP 实现毫秒级语音打断 Barge-in？"
    query_embedding = [0.90, 0.82, 0.10, 0.08]

    print("=" * 72)
    print(f"🔍 执行七层 Agentic RAG 混合检索 + Self-RAG 自省: {query_text}")
    print("=" * 72)
    for chunk, score, reflection in pipeline.retrieve_with_self_reflection(query_text, query_embedding):
        print(f"• [{chunk.doc_id}] {chunk.title} | RRF={score:.5f} | {reflection}")
