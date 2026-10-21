"""领英抓取结果的端到端评测：生成标注表 / 汇总标注结果。

用法：
  # 0) 演示（用仓库里的脱敏样例，10 条假数据）
  python label_53.py --make --results sample_results.json

  # 1) 生成标注表（带下拉和预判，可直接用 Excel 填）
  python label_53.py --make --results <你的 results.json>

  # 2) 填完后汇总（算噪音率、分层准确率、错误归因）
  python label_53.py --analyze

默认读 sample_results.json（10 条脱敏假数据），真实跑时用 --results 指向自己的数据。

设计说明：
  - 判定列只有两个选项（该进 / 不该进），不给人留"说不清"的余地；
    说不清的填「不该进」+ 原因选「信息不足」——拿不准的线索发给销售本来就是浪费。
  - 「预判」列是脚本的自动初判，只用来加速复核，不计入指标。
  - 分层算 strong / weak 的精确率：这两层的含义完全不同
    （strong = 有「目前就职」原话可验证；weak = 只有 headline 兜底），混在一起算总分没意义。
  - 业务前提是「宁滥勿缺」：漏人比多抓贵，所以主指标用**噪音率**（完全无关的人占多少），
    而不是精确率。擦边的本来就该留着。
"""
import argparse
import collections
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RESULTS = os.path.join(HERE, "sample_results.json")
OUT_XLSX = os.path.join(HERE, "领英标注表.xlsx")

SEA = ["新加坡", "马来西亚", "印尼", "越南", "泰国", "菲律宾"]
SUSPECT_WORDS = ["Consultant", "Marketing", "Product Manager", "Account Executive",
                 "Intern", "Assistant", "Coordinator", "Analyst", "Recruit"]

# 地点统一成国家：下游按国家分组统计，不需要省市和乱码
COUNTRY_RULES = [
    (["新加坡", "singapore"], "新加坡"),
    (["马来西亚", "malaysia", "吉隆坡", "槟城", "雪兰"], "马来西亚"),
    (["印尼", "indonesia", "雅加达", "jakarta"], "印尼"),
    (["越南", "vietnam", "河内", "hanoi", "胡志明", "ho chi minh"], "越南"),
    (["泰国", "thailand", "曼谷", "bangkok", "春武里", "是拉差"], "泰国"),
    (["菲律宾", "philippines", "马尼拉", "manila", "卡拉巴松", "米沙鄢"], "菲律宾"),
]

# 售前 / 方案类：属技术评估与商务线，即使带 Consultant 也是目标
PRE_SALES = ["presales", "pre-sales", "solution architect", "solutions engineer",
             "solution engineer", "solutions architect", "solution consultant", "se@"]
TITLE_WORDS = ["director", "manager", "bdm", "engineer", "architect", "head", "lead",
               "vp", "vice president", "ceo", "cto", "coo", "procurement",
               "business development", "bd ", "president", "specialist", "sales"]
# 执行层：职级不够，不是名单要的人
EXEC_LEVEL = ["technician", "administrator", "clerk", "assistant", "support engineer"]


def normalize_location(loc):
    if not loc:
        return ""
    low = str(loc).lower()
    for keys, country in COUNTRY_RULES:
        if any(k in low for k in keys):
            return country
    return str(loc).strip()


def load_rows(results_path):
    r = json.load(open(results_path, encoding="utf-8"))
    rows = []
    for comp, lst in r.items():
        for x in (lst or []):
            rows.append({
                "company": comp,
                "name": x.get("name", ""),
                "headline": x.get("headline", ""),
                "location": normalize_location(x.get("location", "")),
                "tier": x.get("tier", ""),
                "matched": " / ".join(x.get("matched_titles") or []),
                "current": x.get("current", ""),
                "former": x.get("former", ""),
                "url": x.get("url", ""),
            })
    return rows


def auto_flag(x):
    """自动预判：只是提示，不是结论。"""
    flags = []
    if x["tier"] == "weak":
        flags.append("weak兜底")
    head = (x["headline"] or "") + " " + (x["matched"] or "")
    for w in SUSPECT_WORDS:
        if w.lower() in head.lower():
            flags.append(w)
            break
    if x["location"] and not any(k in x["location"] for k in SEA):
        flags.append("地点可疑")
    if not x["headline"]:
        flags.append("无headline")
    return "、".join(flags)


def auto_verdict(x):
    """规则预判，直接写进 J/K 列。地点不再参与判定——地点为空是解析问题，不是人的问题。

    注意：判断"这句话像不像职位"只能看 headline 本身，不能把 matched_titles 拼进来——
    matched_titles 是规则命中的类别名，天生带 Manager/Director，拼进来会让任何垃圾 headline 都通过。
    """
    hl = (x["headline"] or "").lower()
    cur = (x["current"] or "").lower()
    src = hl + " " + cur          # headline 常是营销话术，在职原话里有真职位，用它兜底
    head = src + " " + (x["matched"] or "").lower()

    # 售前 / 方案类先放行：它们属于技术评估与商务线
    if any(w in src for w in PRE_SALES):
        return "该进", ""
    if any(w in head for w in ("marketing", "product manager", "recruit", "talent", "human resource")):
        return "不该进", "完全无关"
    if any(w in src for w in EXEC_LEVEL):
        return "不该进", "职位不对"
    if any(w in src for w in ("consultant", "analyst", "coordinator", "intern", "account executive")):
        return "不该进", "职位不对"
    if x["tier"] == "weak":
        return "不该进", "职位不对"
    # headline 和在职原话里都没有职位词 → 抓到的是公司简介，判不了
    if not any(w in src for w in TITLE_WORDS):
        return "不该进", "信息不足"
    return "该进", ""


def make(results_path):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.worksheet.datavalidation import DataValidation
    from openpyxl.utils import get_column_letter

    rows = load_rows(results_path)
    wb = Workbook()
    ws = wb.active
    ws.title = "标注"

    headers = ["#", "公司", "姓名", "当前职位 / headline", "地点", "层级",
               "命中类别", "在职原话", "主页", "判定", "原因", "备注", "预判（可忽略）"]
    widths = [4, 18, 18, 40, 20, 8, 30, 40, 34, 10, 14, 18, 18]

    head_fill = PatternFill("solid", fgColor="2B579A")
    head_font = Font(color="FFFFFF", bold=True, size=10)
    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for c, (h, w) in enumerate(zip(headers, widths), 1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.fill = head_fill
        cell.font = head_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = "A2"
    ws.row_dimensions[1].height = 26

    # 预判为可疑的行整行浅黄底，方便先看
    warn_fill = PatternFill("solid", fgColor="FFF2CC")
    auto_fill = PatternFill("solid", fgColor="EAF1FB")   # 脚本预填的判定，需复核
    pre_n = 0
    for i, x in enumerate(rows, 2):
        flag = auto_flag(x)
        verdict, reason = auto_verdict(x)
        if verdict == "不该进":
            pre_n += 1
        vals = [i - 1, x["company"], x["name"], x["headline"], x["location"],
                x["tier"], x["matched"], x["current"], x["url"], verdict, reason, "", flag]
        for c, v in enumerate(vals, 1):
            cell = ws.cell(row=i, column=c, value=v)
            cell.border = border
            cell.alignment = Alignment(vertical="top", wrap_text=(c in (4, 7, 8)))
            cell.font = Font(size=9)
        ws.cell(row=i, column=10).fill = auto_fill
        if reason:
            ws.cell(row=i, column=11).fill = auto_fill
        # 主页做成可点链接
        link = ws.cell(row=i, column=9)
        if x["url"]:
            link.hyperlink = x["url"]
            link.font = Font(size=9, color="2458B8", underline="single")
        if flag:
            for c in (1, 2, 3):
                ws.cell(row=i, column=c).fill = warn_fill

    n = len(rows) + 1
    dv1 = DataValidation(type="list", formula1='"该进,不该进"', allow_blank=True)
    dv1.error = "只能填 该进 或 不该进"
    ws.add_data_validation(dv1)
    dv1.add(f"J2:J{n}")

    dv2 = DataValidation(
        type="list",
        formula1='"完全无关,职位不对,已离职,地点不对,公司不对,信息不足,其他"',
        allow_blank=True,
    )
    ws.add_data_validation(dv2)
    dv2.add(f"K2:K{n}")

    # 说明页
    ws2 = wb.create_sheet("怎么填")
    notes = [
        ["怎么填这张表", ""],
        ["", ""],
        ["判定列", "只填两个字：该进 / 不该进。没有中间选项。"],
        ["业务前提", "获客名单宁滥勿缺：漏人比多抓贵。所以只有「一眼看过去就不对」的才填不该进，" ],
        ["", "擦边的一律填「该进」，不用纠结。"],
        ["原因列", "只在「不该进」时填。重点是区分两种："],
        ["", "  完全无关 = 这人不该出现在任何获客名单里（职位、行业都不搭），纯噪音"],
        ["", "  职位不对 = 擦边，职级/职能偏了，但可能在决策链上"],
        ["预判列", "脚本的自动初判（weak 兜底、headline 出现 Consultant/Marketing/Product Manager 等）。"],
        ["", "黄色底的行是预判可疑的，先看这些，其余快速过。"],
        ["", ""],
        ["判的是什么", "这个人该不该出现在这份「液冷论坛目标联系人」名单里。"],
        ["不该判什么", "不用判他好不好联系、会不会回复、值不值得跟——只判「目标符合度」。"],
        ["", ""],
        ["填完运行", "python label_53.py --analyze"],
    ]
    for r, row in enumerate(notes, 1):
        for c, v in enumerate(row, 1):
            cell = ws2.cell(row=r, column=c, value=v)
            if r == 1:
                cell.font = Font(bold=True, size=12, color="2B579A")
            elif c == 1:
                cell.font = Font(bold=True, size=10)
            else:
                cell.font = Font(size=10)
    ws2.column_dimensions["A"].width = 18
    ws2.column_dimensions["B"].width = 88

    wb.save(OUT_XLSX)
    flagged = sum(1 for x in rows if auto_flag(x))
    print(f"已生成 {OUT_XLSX}")
    print(f"  共 {len(rows)} 条 · 脚本预判「不该进」{pre_n} 条（蓝色底的 J/K 列）")
    print(f"  其中预判可疑 {flagged} 条（1–3 列黄底），先看这些")
    print(f"  层级: {dict(collections.Counter(x['tier'] for x in rows))}")
    print("  你要做的：只复核蓝色底的判定，同意就过，不同意直接改下拉；其余不用看")


def analyze():
    from openpyxl import load_workbook

    if not os.path.exists(OUT_XLSX):
        print(f"[!] 没有找到 {OUT_XLSX}，先跑 --make")
        return 1
    ws = load_workbook(OUT_XLSX, data_only=True)["标注"]

    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[0] is None:
            continue
        rows.append({
            "company": row[1], "name": row[2], "tier": row[5],
            "verdict": (row[9] or "").strip(), "reason": (row[10] or "").strip(),
            "flag": row[12],
        })

    done = [r for r in rows if r["verdict"] in ("该进", "不该进")]
    if not done:
        print("还没填。J 列填「该进 / 不该进」之后再跑。")
        return 1

    good = sum(1 for r in done if r["verdict"] == "该进")
    prec = good / len(done) * 100
    noise = sum(1 for r in done if r["reason"] == "完全无关")
    noise_rate = noise / len(done) * 100
    edge = sum(1 for r in done if r["verdict"] == "不该进" and r["reason"] != "完全无关")

    print("=" * 58)
    print(f"★ 噪音率（主指标）: {noise_rate:.1f}%   {noise}/{len(done)} 条完全无关")
    print(f"  精确率           : {prec:.1f}%   {good}/{len(done)} 条判为「该进」")
    print(f"  擦边但已排除     : {edge} 条（职位/在职/地点偏了，但不是纯噪音）")
    print(f"  已完成标注: {len(done)}/{len(rows)} 条")
    print("-" * 58)
    print("  在「宁滥勿缺」的前提下，噪音率是唯一值得优化的指标：")
    print("  精确率降 10% 无所谓，噪音率涨 10% 就是在浪费销售时间。")
    print("-" * 58)

    for tier in ("strong", "weak"):
        sub = [r for r in done if r["tier"] == tier]
        if not sub:
            continue
        g = sum(1 for r in sub if r["verdict"] == "该进")
        print(f"  {tier:7s}  {g}/{len(sub):<3} {g / len(sub) * 100:5.1f}%")

    bad = [r for r in done if r["verdict"] == "不该进"]
    if bad:
        print("-" * 56)
        print("  错误归因：")
        for k, v in collections.Counter(r["reason"] or "未填原因" for r in bad).most_common():
            print(f"    {k:10s} {v}")

    flagged = [r for r in done if r["flag"]]
    if flagged:
        hit = sum(1 for r in flagged if r["verdict"] == "不该进")
        print("-" * 56)
        print(f"  预判有效性：标记 {len(flagged)} 条，其中 {hit} 条确实是误判"
              f"（命中率 {hit / len(flagged) * 100:.0f}%）")

    print("=" * 56)

    out = os.path.join(HERE, "eval_result.json")
    json.dump({
        "total": len(rows), "labeled": len(done),
        "noise_rate": round(noise_rate, 1), "precision": round(prec, 1),
        "noise_count": noise, "edge_count": edge,
        "by_tier": {t: {"n": len([r for r in done if r["tier"] == t]),
                        "good": len([r for r in done if r["tier"] == t and r["verdict"] == "该进"]),
                        "noise": len([r for r in done if r["tier"] == t and r["reason"] == "完全无关"])}
                    for t in ("strong", "weak")},
        "reasons": dict(collections.Counter(r["reason"] for r in bad)),
    }, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"结果写入 {out}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--make", action="store_true")
    ap.add_argument("--results", default=DEFAULT_RESULTS, help="results.json 路径，默认用仓库里的脱敏样例")
    ap.add_argument("--analyze", action="store_true")
    a = ap.parse_args()
    if a.make:
        make(a.results)
    elif a.analyze:
        raise SystemExit(analyze())
    else:
        print(__doc__)
