"""评测：跑用例集 → 判定 → 汇总 → 与 baseline 对比。

判定不看"答案像不像"，只看可验证的东西：
  - 有没有引用（must_cite）
  - 引用是不是期望的那一篇（expect_doc）
  - 负例（语料里没有的问题）有没有硬编答案
  - 是否撞到步数 / 预算上限

失败分五类，而不是一个"失败"：no_citation / wrong_doc / hallucinated /
refused_wrongly / limit_exceeded + 运行时错误（llm_error 等）。
分类才叫能指导行动，单个准确率不能。

用 --mock 跑通全链路（不需要 API key）：mock 模式直接用检索结果拼答案，
用来验证「判定逻辑本身」是对的——负例应当被判 hallucinated，正例应当 pass。
"""
import argparse
import json
import os
import re
import statistics as st
import time

import agent
import retrieval
import tools

REFUSE_WORDS = [
    "没有找到", "未提及", "没有提及", "无法确定", "找不到", "文档中没有",
    "语料里没有", "没有相关", "没有说明", "未说明",
]
CITE_RE = re.compile(r"\[([^\[\]]+?\.md#\d+)\]")


def strip_index(cid):
    return cid.split("#")[0]


def load_cases(path, limit=None):
    cases = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases[:limit] if limit else cases


def _mock_run(question, bm25):
    """无 key 自测：用检索结果直接拼答案，模拟一个"会引用但不会拒答"的 agent。

    取 top-5 而不是 top-3：mock 的目的是验证判定逻辑（能不能抓出乱编），
    不是评估检索排序质量。取太少会把"检索排序"的问题误记成"引用错"。
    """
    hits = bm25.search(question, top_k=5)
    if not hits:
        return {"answer": "（mock）没有检索到内容。", "citations": [], "confidence": "high",
                "steps": 1, "tokens": 0, "elapsed_ms": 0, "reason": "mock", "ok": True, "note": "mock"}
    body = "（mock）根据 " + "、".join(f"[{h['id']}]" for h in hits) + " 的内容整理如上。"
    return {"answer": body, "citations": [h["id"] for h in hits], "confidence": "high",
            "steps": 1, "tokens": 0, "elapsed_ms": 0, "reason": "mock", "ok": True, "note": "mock"}


def judge(case, res):
    """返回 (passed, fail_type, detail)。失败类型就五种，不搞更多。"""
    answer = (res.get("answer") or "").strip()
    cites = res.get("citations") or []
    conf = res.get("confidence") or "low"

    if res.get("reason") in ("step_limit", "budget_limit"):
        return False, "limit_exceeded", res.get("note", "")
    if res.get("reason") in ("llm_error",):
        return False, "runtime_error", res.get("note", "")
    if not answer:
        return False, "runtime_error", "答案为空"

    negative = bool(case.get("negative"))
    if negative:
        refused = conf == "low" or any(w in answer for w in REFUSE_WORDS)
        if refused:
            return True, "", "正确拒答"
        return False, "hallucinated", "语料里没有这个问题，但回答没有声明找不到"

    if case.get("must_cite", True) and not cites:
        return False, "no_citation", "答案里没有可回指的引用"
    must_all = case.get("must_cite_all")
    if must_all:
        got = {strip_index(c) for c in cites}
        missing = [d for d in must_all if d not in got]
        if missing:
            return False, "missing_source", f"没同时引用冲突双方，缺 {missing}"
    must_say = case.get("must_say_any")
    if must_say and not any(w in answer for w in must_say):
        return False, "no_conflict_flag", f"没指出文档间的不一致（应出现其中之一：{must_say[:3]}…）"
    expect = case.get("expect_doc")
    if expect:
        got = {strip_index(c) for c in cites}
        if expect not in got:
            return False, "wrong_doc", f"期望 {expect}，实际引用 {sorted(got)[:3]}"
    return True, "", ""


def summarize(rows):
    """分开算「真正跑完的」和「没跑成的」。

    runtime_error（余额不足 / 网络断）不算模型失败——把它算进通过率会虚低，
    但也不能当成功。所以单独记 not_run，通过率只在 completed 上算。
    """
    completed = [r for r in rows if r["fail_type"] != "runtime_error"]
    not_run = [r for r in rows if r["fail_type"] == "runtime_error"]
    total = len(completed)
    passed = sum(1 for r in completed if r["passed"])

    by_level = {}
    for r in completed:
        k = r["level"]
        d = by_level.setdefault(k, {"n": 0, "pass": 0})
        d["n"] += 1
        d["pass"] += 1 if r["passed"] else 0

    fails = {}
    for r in completed:
        if not r["passed"]:
            fails[r["fail_type"]] = fails.get(r["fail_type"], 0) + 1

    def avg(key):
        vals = [r[key] for r in completed if r.get(key) is not None]
        return round(st.mean(vals), 1) if vals else 0

    return {
        "total": len(rows),
        "completed": total,
        "not_run": len(not_run),
        "not_run_ids": [r["id"] for r in not_run],
        "passed": passed,
        "pass_rate": round(passed / total * 100, 1) if total else 0.0,
        "by_level": {k: {**v, "rate": round(v["pass"] / v["n"] * 100, 1) if v["n"] else 0}
                     for k, v in by_level.items()},
        "fail_types": fails,
        "avg_steps": avg("steps"),
        "avg_tokens": avg("tokens"),
        "avg_elapsed_ms": avg("elapsed_ms"),
    }


def print_report(s, baseline=None):
    print("\n" + "=" * 58)
    print(f"通过 {s['passed']}/{s['completed']}  通过率 {s['pass_rate']}%"
          + (f"   （另有 {s['not_run']} 条未跑成，未计入）" if s["not_run"] else ""))
    print("-" * 58)
    for lvl, v in sorted(s["by_level"].items()):
        print(f"  {lvl:9s} {v['pass']:>3}/{v['n']:<3}  {v['rate']:>5.1f}%")
    if s["fail_types"]:
        print("-" * 58)
        print("  失败分类：")
        for k, v in sorted(s["fail_types"].items(), key=lambda x: -x[1]):
            print(f"    {k:16s} {v}")
    if s["not_run"]:
        print("-" * 58)
        print(f"  未跑成（余额/网络）：{' '.join(s['not_run_ids'])}")
    print("-" * 58)
    print(f"  平均步数 {s['avg_steps']}  平均 token {s['avg_tokens']}  平均耗时 {s['avg_elapsed_ms']}ms")
    if baseline:
        print("-" * 58)
        d = s["pass_rate"] - baseline["pass_rate"]
        sign = "+" if d >= 0 else ""
        print(f"  对比 baseline：{baseline['pass_rate']}% → {s['pass_rate']}%  ({sign}{d:.1f} pt)")
        for lvl in sorted(set(s["by_level"]) | set(baseline.get("by_level", {}))):
            a = baseline.get("by_level", {}).get(lvl, {}).get("rate", 0)
            b = s["by_level"].get(lvl, {}).get("rate", 0)
            print(f"    {lvl:9s} {a:>5.1f}% → {b:>5.1f}%")
    print("=" * 58)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=os.path.join("evals", "cases_v1.jsonl"))
    ap.add_argument("--corpus", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus"))
    ap.add_argument("--out", default=None, help="默认 mock 模式写 mock_baseline.json，真实模式写 baseline.json")
    ap.add_argument("--baseline", default=None, help="上一次的结果文件，用于算涨跌")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--mock", action="store_true", help="不需要 API key，验证判定逻辑")
    ap.add_argument("--sleep", type=float, default=0.3, help="每条之间的间隔，防止触发限流")
    ap.add_argument("--only-failed", default=None,
                    help="读上一份报告，只重跑其中 runtime_error 的用例（省额度）")
    args = ap.parse_args()

    cases = load_cases(args.cases, args.limit)
    if not cases:
        print(f"[!] 没有用例：{args.cases}，先跑 make_cases.py")
        return 1

    if args.only_failed and os.path.exists(args.only_failed):
        prev = json.load(open(args.only_failed, encoding="utf-8"))
        want = {r["id"] for r in prev["rows"] if r["fail_type"] == "runtime_error"}
        if not want:
            print("[i] 上一份报告里没有未跑成的用例，退出")
            return 0
        merged = [c for c in cases if c["id"] in want]
        print(f"[i] 只重跑 {len(merged)} 条未完成的用例：{' '.join(sorted(want))}")
        cases = merged
        prev_rows = prev["rows"]
    elif args.only_failed:
        print(f"[!] 找不到上一份报告：{args.only_failed}")
        return 1
    else:
        prev_rows = None

    if not args.out:
        args.out = os.path.join("evals", "mock_baseline.json" if args.mock else "baseline.json")

    chunks = retrieval.load_corpus(args.corpus)
    bm25 = retrieval.BM25(chunks)
    tools.set_index(bm25)
    print(f"[i] 语料 {len(chunks)} 块 · 用例 {len(cases)} 条 · 模式 {'mock' if args.mock else 'live'}")

    rows = []
    for i, case in enumerate(cases, 1):
        if args.mock:
            res = _mock_run(case["question"], bm25)
        else:
            res = agent.run(case["question"], echo=False)
            time.sleep(args.sleep)
        passed, ftype, detail = judge(case, res)
        rows.append({
            "id": case["id"],
            "level": case["level"],
            "source": case.get("source", ""),
            "negative": bool(case.get("negative")),
            "question": case["question"],
            "passed": passed,
            "fail_type": ftype,
            "detail": detail,
            "reason": res.get("reason"),
            "steps": res.get("steps"),
            "tokens": res.get("tokens"),
            "elapsed_ms": res.get("elapsed_ms"),
            "citations": res.get("citations"),
            "answer_head": (res.get("answer") or "")[:120],
        })
        mark = "PASS" if passed else "FAIL"
        print(f"  [{i:02d}/{len(cases)}] {mark} {case['id']} {case['level']:8s} "
              f"{res.get('steps')}步 {res.get('tokens')}tok {ftype or ''}")

    s_rows = rows
    if prev_rows:
        # 未重跑的用例沿用上一份结果，保证汇总覆盖全量
        new_map = {r["id"]: r for r in rows}
        s_rows = [new_map.get(r["id"], r) for r in prev_rows]
        done_ids = {r["id"] for r in rows}
        print(f"[i] 合并：本次 {len(rows)} 条 + 沿用 {len(s_rows) - len(rows)} 条 = {len(s_rows)} 条")

    s = summarize(s_rows)
    baseline = None
    if args.baseline and os.path.exists(args.baseline):
        baseline = json.load(open(args.baseline, encoding="utf-8")).get("summary")
    print_report(s, baseline)

    report = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "mock": args.mock,
              "corpus": args.corpus, "summary": s, "rows": s_rows}
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"结果已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
