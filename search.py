"""先探路，不回答：判断一个问题在语料里到底有没有答案。

用法：
  python search.py 赞助商推荐要不要收费
  python search.py "我们老板叫什么名字"

为什么需要它：
  Agent 答不出来时会花 3-4 步反复检索，既慢又耗 token。
  先探一次只要 0.05 秒、0 token，就能知道该不该问。
"""
import sys

import retrieval

HERE = __import__("os").path.dirname(__import__("os").path.abspath(__file__))

# 判断"能不能答"只用绝对分数，领先倍数只作参考显示。原因：
#   ① 有答案时 top-1 会甩开其他段（如 17.3 vs 4.6）
#   ② 但答案分散在多个段落时（如"中国 IP 为什么是 0 结果"），top-1 与 top-2 会咬得很近，
#      领先倍数低却完全能答 —— 用倍数做硬判断会误杀。
#   ③ 反过来，问题很短（"weak 层"只有 3 个 token）时分数天然偏低，会被误判成答不出来。
# 所以：分数只做粗分档，最终以"最相关段落"给人自己看。
STRONG = 6.0
MID = 4.0


def main():
    q = " ".join(sys.argv[1:]).strip()
    if not q:
        print(__doc__)
        return 1

    chunks = retrieval.load_corpus(HERE + "/corpus")
    bm25 = retrieval.BM25(chunks)
    hits = bm25.search(q, top_k=5)

    print(f"问题：{q}")
    print(f"语料：{len(chunks)} 块 / {len(set(c['doc'] for c in chunks))} 篇\n")

    if not hits:
        print("命中：无")
        print("判断：语料里没有相关词 → 这个问题答不出来，Agent 会走到「没找到」。")
        return 0

    for i, h in enumerate(hits, 1):
        bar = "█" * max(1, int(h["score"] / 2))
        print(f"  {i}. {h['id']:34s} {h['score']:>6.2f}  {bar}")

    top = hits[0]["score"]
    lead = top / hits[1]["score"] if len(hits) > 1 and hits[1]["score"] > 0 else 99.0
    print()
    print(f"  top-1 = {top:.2f}   领先倍数 = {lead:.2f}x（仅供参考）")
    print()

    if top >= STRONG:
        print("判断：能答。分数够高，答案就在上面第一篇里。")
        if lead < LEAD_WEAK:
            print("      领先倍数偏低，说明答案分散在多个段落——不是答不出来。")
    elif top >= MID:
        print("判断：可能能答。分数中等，先看下面的段落内容再决定要不要问。")
    else:
        print("判断：大概率答不出来。分数很低，Agent 多半会说「语料里没有」，白花 3-4 步检索。")
        print("      例外：问题很短（≤3 个词）时分数天然偏低，仍以段落内容为准。")

    # 顺手展示最相关段落的开头，让人自己判断
    print(f"\n最相关段落（{hits[0]['id']}）：")
    print("  " + hits[0]["text"][:260].replace("\n", "\n  "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
