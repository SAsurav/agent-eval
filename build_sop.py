"""把内部作业 SOP（docx）拆成知识库语料。

拆成 6 篇而不是 1 篇：SOP 太长，整篇塞进去会变成一个巨大的 chunk，
检索命中后返回一大堆无关内容，反而降低回答质量。

脱敏：真实人名 / 公司名 / 活动名 / 酒店名**不写进代码**，由环境变量提供：
  set SOP_SENDER=<发送人姓名>
  set SOP_COMPANY=<公司英文名>
  set SOP_EVENT=<活动全名>
  set SOP_VENUE=<活动酒店>
未设置时不做替换——重新生成语料前务必先设好，否则敏感信息会进语料。
"""
import os
import re

from docx import Document

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "corpus")

# ===== 原始文件位置：用环境变量指定，仓库里不放真实路径 =====
# 仓库已带生成好的 corpus/，平时不需要跑。
# 需要重跑时：set SOP_DOCX=<...>\SOP.docx
SOP = os.environ.get("SOP_DOCX", r"<path>\SOP.docx")
# ===========================================================

# 脱敏映射：真实人名/公司/活动/酒店**不写进代码**，由环境变量提供。
# 重新生成语料前必须先设好，否则敏感信息会进 corpus/。
#   set SOP_SENDER=<发送人姓名>
#   set SOP_COMPANY=<公司英文名>
#   set SOP_EVENT=<活动全名>
#   set SOP_VENUE=<活动酒店>
REDACT = [x for x in [
    (os.environ.get("SOP_SENDER", ""), "[发送人]"),
    (os.environ.get("SOP_COMPANY", ""), "[公司]"),
    (os.environ.get("SOP_EVENT", ""), "[活动全名]"),
    (os.environ.get("SOP_VENUE", ""), "[活动酒店]"),
] if x[0]]


def clean(text):
    if not REDACT:
        print("  ⚠ 未设置脱敏环境变量，本次生成不会做替换（SOP_SENDER / SOP_COMPANY / "
              "SOP_EVENT / SOP_VENUE）")
    for a, b in REDACT:
        text = text.replace(a, b)
    return text


def write(name, title, body):
    os.makedirs(OUT, exist_ok=True)
    text = f"# {title}\n\n> 来源：内部作业 SOP（已脱敏）\n\n{clean(body).strip()}\n"
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        f.write(text)
    print(f"  {name:38s} {len(text.encode('utf-8')) // 1024:>3} KB")


def table_md(tb):
    rows = []
    for row in tb.rows:
        rows.append("| " + " | ".join(c.text.strip().replace("\n", " ") for c in row.cells) + " |")
    return "\n".join(rows)


def main():
    if not os.path.exists(SOP):
        print(f"  [!] 找不到 {SOP}")
        return
    doc = Document(SOP)
    paras = [p.text.strip() for p in doc.paragraphs]
    tables = [table_md(tb) for tb in doc.tables]

    def find(prefix):
        for i, p in enumerate(paras):
            if p.startswith(prefix):
                return i
        return -1

    def slice_between(start_prefix, end_prefix):
        a = find(start_prefix)
        if a < 0:
            return ""
        b = find(end_prefix) if end_prefix else len(paras)
        if b <= a:
            b = len(paras)
        return "\n".join(p for p in paras[a:b] if p)

    # 1. 日常四步（一、）
    write("12-SOP-日常四步.md", "SOP · 每天照着做的四步",
          slice_between("一、每天照着做", "二、优先级"))

    # 2. 优先级 + 状态（二、三、+ 前两张表）
    body = slice_between("二、优先级", "五、付费判定")
    if len(tables) >= 2:
        body += "\n\n## 优先级判定\n\n" + tables[0] + "\n\n## 状态定义\n\n" + tables[1]
    write("13-SOP-优先级与状态.md", "SOP · 优先级与状态定义", body)

    # 3. 付费判定（SOP 版本，与台账里的版本并存 → 故意保留冲突）
    body = slice_between("五、付费判定", "六、数量红线")
    if len(tables) >= 3:
        body += "\n\n" + tables[2]
    write("17-SOP-付费判定.md", "SOP · 付费判定（按顺序问）", body)

    # 4. 数量红线
    write("14-SOP-数量红线.md", "SOP · 账号操作的数量红线",
          slice_between("六、数量红线", "七、英文话术"))

    # 5. 英文话术
    write("15-SOP-英文话术.md", "SOP · 英文触达话术",
          slice_between("七、英文话术", "八、收工前"))

    # 6. 收工清单
    write("16-SOP-收工清单.md", "SOP · 每日收工检查清单",
          slice_between("八、收工前", None))

    files = sorted(f for f in os.listdir(OUT) if f.endswith(".md"))
    total = sum(os.path.getsize(os.path.join(OUT, f)) for f in files)
    print(f"\n语料共 {len(files)} 篇，{total // 1024} KB")


if __name__ == "__main__":
    main()
