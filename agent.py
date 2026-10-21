"""手写 agentic loop。四件事：模型决策 → 工具执行 → 结果回填 → 终止判断。

三道终止条件（缺一不可）：
  1. 显式 finish 工具      —— 模型主动声明「我完成了」
  2. 模型不再发起 tool_use —— 自然收敛
  3. 步数 / token 预算硬上限 —— 兜底。前两道都可能失效（模型反复调工具、或一直不结束）

回填格式的差异（面试常问，这里按接口实现）：
  OpenAI 兼容接口（DeepSeek / GPT）：每个 tool_call 对应一条 role="tool" 消息，按 tool_call_id 回填。
  Claude Messages API：同一轮的多个 tool_result 必须打包进**一条** user 消息；
  若拆成多条，模型会认为并发调用已被打断，后续不再发起并发 tool_use。
  两者语义不同：前者是"逐个交付"，后者是"一批交付"。写错不报错，只是行为悄悄变差。

并发：同一轮的多个 tool_call 用线程池并行执行（工具是 IO 型：检索、写盘）。
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import llm
import retrieval
import tools
from trace import Tracer

MAX_STEPS = 8
TOKEN_BUDGET = 60000
TRIM_AFTER_STEP = 3          # 第 4 步起开始裁剪旧工具结果
TRIM_KEEP_CHARS = 700        # 裁剪后每条工具结果保留的长度

_HERE = os.path.dirname(os.path.abspath(__file__))

SYSTEM = """你是一个技术文档问答 Agent。工作方式：

1. 先用 search_docs 检索，拿到带 id 的片段，再回答。不要凭常识回答文档类问题。
2. 需要算术或统计时用 calc，不要心算。
3. 结论较长、或后续还要基于它推理时，用 save_note 落盘。
4. 拿到足够依据后立即调用 finish，不要为了让答案更长而多搜几轮。
5. answer 里必须带引用，格式 [文档名#序号]，例如 [03-付费判定规则.md#1]。
6. 检索确实没有相关内容时：用 confidence="low"，并在 answer 里明说"语料里没有找到"，绝不编造。
7. **多份文档可能互相矛盾**（同名指标算法不同、状态定义不一致、话术与规则打架、规范与实际不符）。
   一旦发现，必须在 answer 里写清三件事：哪两份文档冲突、冲突点是什么、各自口径分别是什么；
   然后说明"需要人工确认以哪份为准"。**不要自己挑一份当答案，也不要只引用其中一份。**
   检索时如果命中同一主题的多篇文档，务必都读一遍再决定。

禁止：编造文档里没有的事实；引用没出现在 search_docs 结果里的 id。"""


def _estimate_tokens(messages):
    """粗估：中文 1 字≈1 token，英文 4 字符≈1 token。用于预算与裁剪判断，不需要精确。"""
    n = 0
    for m in messages:
        c = m.get("content") or ""
        if isinstance(c, list):
            c = json.dumps(c, ensure_ascii=False)
        cjk = len(re.findall(r"[\u4e00-\u9fff]", c))
        n += cjk + (len(c) - cjk) // 4 + 4
    return n


def _trim(messages, step, tracer):
    """上下文裁剪：保留 system 与最近一轮，把更早的 tool 结果截短。

    这是「上下文是预算」的最小实现：不是等爆了再处理，而是按步数主动降。
    """
    trimmed = 0
    for i, m in enumerate(messages):
        if m.get("role") != "tool":
            continue
        if i >= len(messages) - 6:          # 最近 3 轮不动
            continue
        c = m.get("content") or ""
        if len(c) > TRIM_KEEP_CHARS:
            m["content"] = c[:TRIM_KEEP_CHARS] + f"\n…[已裁剪 {len(c) - TRIM_KEEP_CHARS} 字符]"
            trimmed += 1
    if trimmed:
        tracer.log("trim", step=step, trimmed=trimmed, est_tokens=_estimate_tokens(messages))
    return trimmed


def run(question, tracer=None, max_steps=MAX_STEPS, token_budget=TOKEN_BUDGET, echo=False):
    """跑一条 case，返回结果 dict。永不抛异常——所有失败都变成结构化结果。"""
    tracer = tracer or Tracer(re.sub(r"\W+", "_", question)[:40], echo=echo)
    started = time.time()
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": question},
    ]
    total_tokens = 0
    steps = 0
    tracer.log("start", question=question, max_steps=max_steps, token_budget=token_budget)

    while True:
        steps += 1
        if steps > max_steps:
            return _end(tracer, started, "step_limit", "", steps, total_tokens, None,
                        note=f"超过 {max_steps} 步仍未结束（可能是模型反复调工具）")
        if total_tokens > token_budget:
            return _end(tracer, started, "budget_limit", "", steps, total_tokens, None,
                        note=f"token 预算 {token_budget} 已用尽")

        if steps >= TRIM_AFTER_STEP:
            _trim(messages, steps, tracer)

        try:
            msg, meta = llm.chat(messages, tools=tools.TOOLS)
        except llm.LLMError as e:
            tracer.log("llm_error", step=steps, error=str(e))
            return _end(tracer, started, "llm_error", "", steps, total_tokens, None, note=str(e))

        total_tokens += meta.get("total_tokens", 0)
        tool_calls = msg.get("tool_calls") or []
        tracer.log(
            "assistant",
            step=steps,
            content=(msg.get("content") or "")[:400],
            tool_calls=[{"name": tc["function"]["name"], "args": tc["function"]["arguments"]} for tc in tool_calls],
            latency_ms=meta.get("latency_ms"),
            tokens=meta.get("total_tokens"),
        )

        # 回填 assistant 消息（必须保留 tool_calls，否则下一轮 tool 消息没有归属）
        messages.append(
            {
                "role": "assistant",
                "content": msg.get("content") or "",
                **({"tool_calls": tool_calls} if tool_calls else {}),
            }
        )

        # ---- 终止条件 2：模型不再发起工具调用
        if not tool_calls:
            answer = msg.get("content") or ""
            if not answer.strip():
                return _end(tracer, started, "empty_answer", "", steps, total_tokens, None,
                            note="模型既没有调用工具也没有输出内容")
            return _end(tracer, started, "no_tool_call", answer, steps, total_tokens,
                        {"citations": _extract_citations(answer), "confidence": "low"})

        # ---- 终止条件 1：显式 finish
        for tc in tool_calls:
            if tc["function"]["name"] == "finish":
                args = _parse_args(tc)
                tracer.log("finish", step=steps, args=args)
                answer = args.get("answer", "")
                return _end(
                    tracer, started, "explicit_finish", answer, steps, total_tokens,
                    {"citations": args.get("citations") or _extract_citations(answer),
                     "confidence": args.get("confidence", "high")},
                )

        # ---- 工具执行（并发）→ 逐条回填
        results = _run_tools(tool_calls, steps, tracer)
        for tc, res in zip(tool_calls, results):
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(res, ensure_ascii=False),
                }
            )


def _run_tools(tool_calls, step, tracer):
    def one(tc):
        name = tc["function"]["name"]
        args = _parse_args(tc)
        t0 = time.time()
        res = tools.call(name, args)
        ms = int((time.time() - t0) * 1000)
        tracer.log(
            "tool", step=step, name=name, args=args,
            is_error=res.get("is_error", False),
            ms=ms,
            result_len=len(json.dumps(res, ensure_ascii=False)),
            error_type=(res.get("error") or {}).get("type"),
        )
        return res

    if len(tool_calls) == 1:
        return [one(tool_calls[0])]
    with ThreadPoolExecutor(max_workers=min(4, len(tool_calls))) as pool:
        return list(pool.map(one, tool_calls))


def _parse_args(tc):
    raw = tc["function"].get("arguments") or "{}"
    try:
        args = json.loads(raw)
        return args if isinstance(args, dict) else {}
    except json.JSONDecodeError:
        return {}


_CITE_RE = re.compile(r"\[([^\[\]]+?\.md#\d+)\]")


def _extract_citations(answer):
    seen, out = set(), []
    for m in _CITE_RE.findall(answer or ""):
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _end(tracer, started, reason, answer, steps, tokens, extra, note=""):
    rec = {
        "reason": reason,
        "answer": answer,
        "steps": steps,
        "tokens": tokens,
        "elapsed_ms": int((time.time() - started) * 1000),
        "citations": (extra or {}).get("citations", []),
        "confidence": (extra or {}).get("confidence", "low"),
        "note": note,
        "ok": reason in ("explicit_finish", "no_tool_call"),
    }
    tracer.log("end", **{k: v for k, v in rec.items() if k != "answer"})
    return rec


# ---------------------------------------------------------------- CLI
def main():
    import argparse
    import glob
    import os as _os

    ap = argparse.ArgumentParser(description="跑一条问题，打印 trace")
    ap.add_argument("question", nargs="*", help="要问的问题")
    ap.add_argument("--corpus", default=_os.path.join(_HERE, "corpus"), help="文档目录")
    ap.add_argument("--echo", action="store_true", help="边跑边打印 trace")
    args = ap.parse_args()

    chunks = retrieval.load_corpus(args.corpus)
    if not chunks:
        print(f"[!] 语料为空：{args.corpus}")
        return 1
    tools.set_index(retrieval.BM25(chunks))
    print(f"[i] 语料 {len(chunks)} 块 / 来自 {args.corpus}")

    q = " ".join(args.question) or "这个仓库里的 skill 是如何描述自己的适用场景的？"
    print(f"[Q] {q}\n")
    res = run(q, echo=True)
    print("\n[答案]", res["answer"][:1200])
    print(f"[结束] reason={res['reason']} steps={res['steps']} tokens={res['tokens']} "
          f"elapsed={res['elapsed_ms']}ms conf={res['confidence']}")
    if res["note"]:
        print(f"[备注] {res['note']}")
    return 0 if res["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
