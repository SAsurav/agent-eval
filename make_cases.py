"""生成评测用例。

两类来源，刻意分开标注：

  [from_corpus] 直接从语料里抽：拿每篇文档的首个有效段落当"事实锚点"，
                问题由该文档的标题 + 主题词拼出来。可复现，ground truth 客观
                （正确答案必须引用这一篇）。
  [handmade]    我手写的边界与拒答题。拒答题（negative）是这套评测里最有价值的一类：
                它测的不是"能不能答对"，而是"不知道时会不会硬编"。

每条用例先做自检（sanity）：用同一个 BM25 检一遍，看期望文档能不能进 top-5。
进不去就标 sanity=false —— 用例本身可能是坏的，不能让它污染 agent 的分数。
（judge 自己也要被 eval，这条同样适用于用例集。）

用法：
  python make_cases.py --corpus corpus --out evals/cases_v1.jsonl
  python make_cases.py            # 默认读 ./corpus，输出 evals/cases_v1.jsonl
"""
import argparse
import json
import os
import random
import re

import retrieval

# ---------------------------------------------------------------- 手写用例
# negative=True 表示"语料里没有，正确行为是明确说找不到"。
HANDMADE = [
    # --- boundary：业务规则类，必须精确落到某一篇
    {"level": "boundary", "q": "赞助商推荐来的客户要不要收费？",
     "doc": "03-付费判定规则.md"},
    {"level": "boundary", "q": "拿不准某个客户该不该免费的时候应该怎么处理？",
     "doc": "03-付费判定规则.md"},
    {"level": "boundary", "q": "哪些事情是明确不允许自己决定的？",
     "doc": "03-付费判定规则.md"},
    {"level": "boundary", "q": "为什么中国 IP 会导致抓取结果全部为 0？",
     "doc": "01-领英抓取工作流.md"},
    {"level": "boundary", "q": "weak 层的联系人应该怎么处理，为什么？",
     "doc": "01-领英抓取工作流.md"},
    {"level": "boundary", "q": "为什么不能相信界面回读来判断消息是否真的发出去了？",
     "doc": "06-案例-四个关键决策.md"},
    {"level": "boundary", "q": "幂等重算和保存进度这两种恢复方式有什么区别？",
     "doc": "06-案例-四个关键决策.md"},
    {"level": "boundary", "q": "跟进台账里的状态可以填哪些值？",
     "doc": "02-跟进台账使用说明.md"},
    # --- negative：语料里没有，正确行为是拒答而不是编
    {"level": "hard", "negative": True, "q": "2025 年这套流程带来的营收是多少？"},
    {"level": "hard", "negative": True, "q": "赞助商名单里一共有几家公司？"},
    {"level": "hard", "negative": True, "q": "抓取程序部署在哪家云厂商、什么区域？"},
    {"level": "hard", "negative": True, "q": "作者每周在这套流程上投入多少小时？"},
    {"level": "hard", "negative": True, "q": "这些线索的平均成交周期是多久？"},
]

STOP = set("的了吗呢是在和与及对这个那个我们你们他们一个如何什么怎么为什么哪些".split())


def _first_meaningful_para(text):
    for p in re.split(r"\n\s*\n", text):
        p = p.strip().lstrip("#").strip()
        if len(p) >= 40 and not p.startswith("```"):
            return p
    return ""


def _title_of(text, fallback):
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return fallback


def _keywords(text, n=4):
    toks = re.findall(r"[\u4e00-\u9fff]{2,6}|[a-zA-Z][a-zA-Z0-9_\-]{2,}", text)
    seen, out = set(), []
    for t in toks:
        if t in STOP or t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= n:
            break
    return out


def build(corpus_root, n_from_corpus=19, seed=7):
    chunks = retrieval.load_corpus(corpus_root)
    bm25 = retrieval.BM25(chunks)

    by_doc = {}
    for c in chunks:
        by_doc.setdefault(c["doc"], []).append(c)

    docs = sorted(by_doc)
    random.Random(seed).shuffle(docs)

    cases = []
    cid = 0
    for doc in docs:
        if len([x for x in cases if x.get("source") == "from_corpus"]) >= n_from_corpus:
            break
        head = by_doc[doc][0]
        title = _title_of(head["text"], os.path.basename(doc))
        kws = _keywords(_first_meaningful_para(head["text"]) or head["text"])
        if not kws:
            continue
        for variant in ("kw", "whole"):
            if len([x for x in cases if x.get("source") == "from_corpus"]) >= n_from_corpus:
                break
            lvl = "simple" if variant == "whole" else "boundary"
            if variant == "kw":
                q = f"关于「{title}」，文档里对 {'、'.join(kws[:3])} 是怎么规定的？"
            else:
                q = f"「{title}」这份文档主要讲什么？"
            cid += 1
            cases.append({
                "id": f"c{cid:03d}",
                "level": lvl,
                "source": "from_corpus",
                "question": q,
                "expect_doc": doc,
                "must_cite": True,
            })

    for h in HANDMADE:
        cid += 1
        item = {
            "id": f"c{cid:03d}",
            "level": h["level"],
            "source": "handmade",
            "question": h["q"],
            "must_cite": not h.get("negative", False),
            "negative": h.get("negative", False),
        }
        if h.get("doc"):
            item["expect_doc"] = h["doc"]
        cases.append(item)

    # ---- 自检：期望文档能不能被检索进 top-5
    for c in cases:
        doc = c.get("expect_doc")
        if not doc:
            c["sanity"] = None
            continue
        hits = bm25.search(c["question"], top_k=5)
        c["sanity"] = any(h["doc"] == doc for h in hits)
        if not c["sanity"]:
            c["sanity_note"] = "期望文档未进入 top-5：可能用例措辞与原文不匹配，先改题或用例作废"

    return cases


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus"))
    ap.add_argument("--out", default=os.path.join("evals", "cases_v1.jsonl"))
    ap.add_argument("--n", type=int, default=19, help="从语料自动生成的条数")
    args = ap.parse_args()

    cases = build(args.corpus, n_from_corpus=args.n)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    bad = [c for c in cases if c.get("sanity") is False]
    by_level = {}
    for c in cases:
        by_level[c["level"]] = by_level.get(c["level"], 0) + 1
    print(f"已生成 {len(cases)} 条 → {args.out}")
    print(f"  分层: {by_level}")
    print(f"  其中负例（应拒答）{sum(1 for c in cases if c.get('negative'))} 条")
    print(f"  自检未通过 {len(bad)} 条")
    for c in bad[:6]:
        print(f"    ! {c['id']} {c['question'][:44]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
