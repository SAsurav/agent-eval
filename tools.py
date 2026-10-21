"""三个工具 + finish。设计要点写在每个 schema 的 description 里：

  - 描述写「什么时候用 / 什么时候不该用」，不只是「是什么」——工具描述质量 = 调用准确率
  - 返回值结构化；失败返回 is_error=true + 原因 + 可修正建议，让模型自己修，而不是抛异常
  - 有副作用的工具（save_note）必须幂等：同名覆盖，并回报 existed
  - 工具总数控制在 4 个（含 finish）：多了会占上下文并拉低选择准确率
"""
import ast
import json
import os
import re

import retrieval

_HERE = os.path.dirname(os.path.abspath(__file__))
NOTES_DIR = os.path.join(_HERE, "notes")

_SEARCH = None


def set_index(bm25):
    global _SEARCH
    _SEARCH = bm25


def ok(data):
    return {"is_error": False, "data": data}


def err(etype, message, hint=""):
    return {"is_error": True, "error": {"type": etype, "message": message, "hint": hint}}


# ---------------------------------------------------------------- search_docs
def search_docs(query, top_k=5):
    if _SEARCH is None:
        return err("index_missing", "检索索引还没加载", "调用方需要先 set_index()")
    if not str(query).strip():
        return err("empty_query", "query 是空的", "给出具体关键词，例如「BM25 参数」")
    try:
        top_k = max(1, min(int(top_k), 8))
    except (TypeError, ValueError):
        top_k = 5
    hits = _SEARCH.search(query, top_k=top_k)
    if not hits:
        return err(
            "no_hit",
            f"语料里没有和「{query}」相关的内容",
            "换关键词再试一次；仍然没有就说明文档里确实没写，应当直接说明找不到，而不是猜。",
        )
    return ok({"query": query, "hits": hits})


# ---------------------------------------------------------------- calc
_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.List, ast.Tuple,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod, ast.FloorDiv,
    ast.USub, ast.UAdd, ast.Load,
)
_ALLOWED_CALLS = {
    "sum": lambda xs: sum(xs),
    "mean": lambda xs: sum(xs) / len(xs),
    "min": min,
    "max": max,
    "round": round,
    "len": len,
    "abs": abs,
}


def calc(expression):
    """只允许算术与少数统计函数，不用 eval。"""
    if not str(expression).strip():
        return err("empty_expression", "expression 是空的", "例如 (1+2)*3 或 mean([1,2,3])")
    try:
        tree = ast.parse(str(expression), mode="eval")
    except SyntaxError as e:
        return err("syntax", f"表达式解析失败: {e.msg}", "检查括号与运算符，例如 mean([1,2,3])")

    def ev(node):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(f"不允许的语法: {type(node).__name__}")
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError("只允许数字常量")
        if isinstance(node, ast.List):
            return [ev(x) for x in node.elts]
        if isinstance(node, ast.Tuple):
            return tuple(ev(x) for x in node.elts)
        if isinstance(node, ast.UnaryOp):
            v = ev(node.operand)
            return -v if isinstance(node.op, ast.USub) else +v
        if isinstance(node, ast.BinOp):
            a, b = ev(node.left), ev(node.right)
            op = node.op
            if isinstance(op, ast.Add):
                return a + b
            if isinstance(op, ast.Sub):
                return a - b
            if isinstance(op, ast.Mult):
                return a * b
            if isinstance(op, ast.Div):
                if b == 0:
                    raise ValueError("除数为 0")
                return a / b
            if isinstance(op, ast.FloorDiv):
                if b == 0:
                    raise ValueError("除数为 0")
                return a // b
            if isinstance(op, ast.Mod):
                return a % b
            if isinstance(op, ast.Pow):
                if abs(b) > 64:
                    raise ValueError("指数过大")
                return a ** b
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_CALLS:
                raise ValueError("只允许 sum/mean/min/max/round/len/abs")
            return _ALLOWED_CALLS[node.func.id](*[ev(a) for a in node.args])
        raise ValueError("不支持的表达式")

    try:
        value = ev(tree)
    except (ValueError, ZeroDivisionError, TypeError) as e:
        return err("eval_error", str(e), "把表达式改写成纯数字运算，例如 (3+4)/2")
    return ok({"expression": str(expression), "value": value})


# ---------------------------------------------------------------- save_note
def _slug(title):
    s = re.sub(r"[^\w\u4e00-\u9fff\- ]+", "", str(title)).strip().replace(" ", "-")
    return (s or "note")[:60]


def save_note(title, content):
    """幂等：同名覆盖，返回是否新增。用于把中间结论外置，避免依赖对话历史。"""
    if not str(title).strip():
        return err("empty_title", "title 是空的", "给这条笔记一个能检索的短标题")
    os.makedirs(NOTES_DIR, exist_ok=True)
    name = _slug(title) + ".md"
    path = os.path.join(NOTES_DIR, name)
    existed = os.path.exists(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(content))
    return ok(
        {
            "note_id": name,
            "path": path,
            "bytes": len(str(content).encode("utf-8")),
            "existed": existed,
            "idempotent": "同名覆盖，重复调用不会产生副本",
        }
    )


# ---------------------------------------------------------------- finish
def finish(answer, citations=None, confidence="high"):
    """显式终止信号。真正的实现由 agent.py 拦截，这里只做参数校验。"""
    if not str(answer).strip():
        return err("empty_answer", "answer 是空的", "给出最终回答，或改用 confidence=low 说明不确定")
    return ok(
        {
            "answer": str(answer),
            "citations": citations or [],
            "confidence": confidence if confidence in ("high", "low") else "low",
            "_terminal": True,
        }
    )


HANDLERS = {
    "search_docs": search_docs,
    "calc": calc,
    "save_note": save_note,
    "finish": finish,
}


def call(name, args):
    """统一入口。任何异常都转成 is_error，绝不让工具失败被静默吞掉。"""
    fn = HANDLERS.get(name)
    if fn is None:
        return err(
            "unknown_tool",
            f"没有名为 {name} 的工具",
            "可用工具：" + ", ".join(HANDLERS),
        )
    if not isinstance(args, dict):
        return err("bad_args", "参数必须是对象", "按 schema 传参")
    try:
        return fn(**args)
    except TypeError as e:
        return err("bad_args", f"参数不匹配: {e}", "检查必填字段与类型")
    except Exception as e:  # noqa: BLE001 — 工具内部任何崩溃都要变成可读错误
        return err("tool_crashed", f"{type(e).__name__}: {e}", "换一种参数再试，或换工具")


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": (
                "在本地技术文档语料里检索相关段落，返回带 id 的命中片段。\n"
                "何时用：需要事实、定义、步骤、参数等可引用依据时，先调用它。\n"
                "何时不用：问的是计算或统计（用 calc）；问的是文档里显然没有的内容——那就先搜一次确认没有，再直接说明找不到，不要凭常识补。\n"
                "返回里的 id（形如 SKILL.md#3）必须原样写进最终 answer 的引用里。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词，中文或英文，尽量具体"},
                    "top_k": {"type": "integer", "description": "返回条数，默认 5，最大 8"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calc",
            "description": (
                "做算术或简单统计（sum/mean/min/max/round/len/abs）。\n"
                "何时用：需要对已有数字做加减乘除、求平均、算比例时——不要心算，心算容易错且无法复核。\n"
                "何时不用：需要的是文档事实（用 search_docs）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "表达式，例如 (13/500)*100 或 mean([3,5,7])",
                    }
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": (
                "把中间结论写成本地 markdown 笔记（同名覆盖，幂等）。\n"
                "何时用：结论较长、或后面还要基于它继续推理时，先落盘再引用，避免长对话里信息被挤掉。\n"
                "何时不用：一句话就能答完的问题，不需要落盘。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "短标题，会作为文件名"},
                    "content": {"type": "string", "description": "笔记正文（markdown）"},
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "声明任务完成并给出最终回答。这是唯一的正常结束方式。\n"
                "何时用：已经拿到足够依据、可以回答时立即调用，不要为了多搜几次而拖着不结束。\n"
                "何时不用：还没检索过、或检索没命中时不要用它硬编答案；检索确实没有内容时，用 confidence=low 明确说明找不到。\n"
                "answer 里必须带引用（形如 [SKILL.md#3]），没有依据就明说。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {"type": "string", "description": "最终回答正文，含 [文档名#序号] 引用"},
                    "citations": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "用到的 chunk id 列表",
                    },
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "low"],
                        "description": "找不到依据时用 low，并在 answer 里说明",
                    },
                },
                "required": ["answer"],
            },
        },
    },
]
