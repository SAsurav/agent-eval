"""检索层评测：不看模型，只算「正确答案排第几」。

为什么单独测检索：
  Agent 答错的常见原因不是模型笨，是检索没把对的段落捞进上下文。
  把这一层单独量出来，才知道该改检索还是改 prompt。
  而且它 0 成本、不需要 API key，可以随时跑。

指标：
  hit@1 / hit@3 —— 期望文档是否出现在 top-1 / top-3
  MRR           —— 期望文档排名倒数的均值（越接近 1 越好）
"""
import json
import os
import sys

import retrieval

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    corpus = os.path.join(HERE, "corpus")
    files = sys.argv[1:] or [
        os.path.join(HERE, "evals", "cases_v1.jsonl"),
        os.path.join(HERE, "evals", "cases_hard.jsonl"),
    ]
    cases = []
    for f in files:
        if not os.path.exists(f):
            continue
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if line:
                cases.append(json.loads(line))

    chunks = retrieval.load_corpus(corpus)
    bm25 = retrieval.BM25(chunks)
    print(f"语料 {len(chunks)} 块 / {len(set(c['doc'] for c in chunks))} 篇 · 用例 {len(cases)} 条\n")

    hit1 = hit3 = scored = 0
    rr = 0.0
    misses = []
    for c in cases:
        expect = c.get("expect_doc")
        if not expect:
            continue          # 负例没有期望文档，跳过
        scored += 1
        hits = bm25.search(c["question"], top_k=10)
        rank = next((i + 1 for i, h in enumerate(hits) if h["doc"] == expect), 0)
        if rank == 1:
            hit1 += 1
        if 1 <= rank <= 3:
            hit3 += 1
        rr += (1.0 / rank) if rank else 0.0
        if rank != 1:
            misses.append((c["id"], c["question"][:34], expect, rank))

    print(f"hit@1 = {hit1}/{scored} = {hit1 / scored * 100:.1f}%")
    print(f"hit@3 = {hit3}/{scored} = {hit3 / scored * 100:.1f}%")
    print(f"MRR   = {rr / scored:.3f}")

    not_top1 = [m for m in misses if m[3] > 1]
    if not_top1:
        print(f"\n没排第一的 {len(not_top1)} 条（rank 0 = 前十都没进）：")
        for cid, q, doc, rank in not_top1:
            print(f"  {cid}  rank={rank or '--'}  {q}")
            print(f"         期望: {doc}")

    zero = [m for m in misses if m[3] == 0]
    if zero:
        print(f"\n⚠ 前十都没进：{len(zero)} 条 —— 这些问题的答案检索层根本捞不到")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
