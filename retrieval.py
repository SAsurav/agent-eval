"""检索层：中文 BM25，零依赖（只用标准库 + numpy）。

为什么手写而不是上向量库：
  1. 个人项目跑通优先，BM25 对小语料足够，且没有 embedding 费用；
  2. 检索层是可替换的——只要 search() 契约不变，换成向量检索不影响 agent 循环。
  3. 关键设计：chunk 带稳定 id（文档名#序号），引用可以回指原文，评测才有判据。

中文切分没有 jieba，用字符 n-gram（默认 bigram）：对中文足够，英文/数字按词切。
"""
import glob
import os
import re
import numpy as np

CHUNK_CHARS = 420          # 每块目标长度
CHUNK_OVERLAP = 60         # 块间重叠，避免切断上下文
BM25_K1 = 1.5
BM25_B = 0.75

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_\-]{2,}|[\u4e00-\u9fff]")


def _tokens(text):
    """中文取 bigram，英文/数字按词。返回 list。"""
    raw = _TOKEN_RE.findall(text.lower())
    toks = []
    prev_cjk = None
    for t in raw:
        if len(t) == 1 and "\u4e00" <= t <= "\u9fff":
            if prev_cjk is not None:
                toks.append(prev_cjk + t)
            prev_cjk = t
        else:
            toks.append(t)
            prev_cjk = None
    return toks


def _split_chunks(text):
    """先按空行分段，再把短段合并到 CHUNK_CHARS 左右，保留 OVERLAP。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if len(buf) + len(p) + 1 <= CHUNK_CHARS:
            buf = buf + "\n" + p if buf else p
        else:
            if buf:
                chunks.append(buf)
            buf = p
    if buf:
        chunks.append(buf)

    # 加一点重叠，避免边界信息丢失
    out = []
    for i, c in enumerate(chunks):
        if i > 0 and CHUNK_OVERLAP:
            tail = chunks[i - 1][-CHUNK_OVERLAP:]
            c = tail + "\n" + c
        out.append(c)
    return out


def load_corpus(root, pattern="**/*.md", max_files=80):
    """读取 root 下所有匹配文件，返回 chunk 列表。

    chunk = {id, doc, path, text}
    id 用**完整相对路径** + 序号（如 "skills/job-match/SKILL.md#3"）：
    语料里同名 SKILL.md 有二十多个，只用文件名会让引用无法回指，评测也就失去判据。
    """
    files = sorted(glob.glob(os.path.join(root, pattern), recursive=True))
    files = [f for f in files if os.path.isfile(f)][:max_files]
    chunks = []
    for f in files:
        try:
            text = open(f, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if len(text.strip()) < 40:
            continue
        label = os.path.relpath(f, root).replace("\\", "/")
        for i, c in enumerate(_split_chunks(text)):
            chunks.append(
                {
                    "id": f"{label}#{i}",
                    "doc": label,
                    "path": f,
                    "text": c,
                }
            )
    return chunks


class BM25:
    def __init__(self, chunks):
        self.chunks = chunks
        self.docs = [_tokens(c["text"]) for c in chunks]
        self.N = len(self.docs)
        self.doc_len = np.array([len(d) for d in self.docs], dtype=float)
        if self.doc_len.size == 0:
            self.doc_len = np.zeros(1)
        self.avg_len = float(self.doc_len.mean()) if self.N else 1.0

        self.tf = []
        self.df = {}
        for d in self.docs:
            counts = {}
            for t in d:
                counts[t] = counts.get(t, 0) + 1
            self.tf.append(counts)
            for t in counts:
                self.df[t] = self.df.get(t, 0) + 1

    def search(self, query, top_k=5):
        if not self.N:
            return []
        q = _tokens(query)
        if not q:
            return []
        scores = np.zeros(self.N)
        for term in set(q):
            df = self.df.get(term, 0)
            if df == 0:
                continue
            idf = np.log(1 + (self.N - df + 0.5) / (df + 0.5))
            for i, counts in enumerate(self.tf):
                f = counts.get(term, 0)
                if not f:
                    continue
                denom = f + BM25_K1 * (1 - BM25_B + BM25_B * self.doc_len[i] / self.avg_len)
                scores[i] += idf * (f * (BM25_K1 + 1)) / denom
        order = np.argsort(-scores)[:top_k]
        hits = []
        for idx in order:
            if scores[idx] <= 0:
                continue
            c = self.chunks[int(idx)]
            hits.append(
                {
                    "id": c["id"],
                    "doc": c["doc"],
                    "text": c["text"][:900],
                    "score": round(float(scores[idx]), 4),
                }
            )
        return hits
