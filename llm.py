"""DeepSeek 客户端（OpenAI 兼容）。零第三方依赖，只用标准库。

key 读取顺序：
  1. 环境变量 DEEPSEEK_API_KEY      ← 唯一推荐方式
  2. 同目录 key.txt（一行，不要加引号）

⚠️ key.txt 只是为了本地双击 bat 时省事，**不要提交到版本控制**（已在 .gitignore 里排除）。
   生产环境必须走密钥管理（Vault / AWS Secrets Manager / K8s Secret + RBAC），
   不要用文件、不要把 key 写进代码或镜像。这个项目的 key 处理是原型级，不是生产级。

用法：
  from llm import chat
  resp = chat(messages, tools=TOOLS)
"""
import json
import os
import time
import urllib.error
import urllib.request

API_BASE = os.environ.get("DEEPSEEK_BASE", "https://api.deepseek.com")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
_HERE = os.path.dirname(os.path.abspath(__file__))


class LLMError(Exception):
    """网络 / 鉴权 / 服务端错误。调用方应把它当 is_error 处理，而不是静默重试。"""


def get_key():
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        p = os.path.join(_HERE, "key.txt")
        if os.path.exists(p):
            key = open(p, encoding="utf-8").read().strip()
    if not key:
        raise LLMError(
            "没有找到 key。设置环境变量 DEEPSEEK_API_KEY，或在 agent-eval/key.txt 里写一行 key。"
        )
    return key


def chat(messages, tools=None, temperature=0.0, timeout=180, max_retries=2):
    """返回 (message, meta)。message 是 assistant 消息 dict，meta 含 usage / latency。

    重试只覆盖网络抖动与 5xx；4xx 直接抛，不掩盖。
    """
    payload = {"model": MODEL, "messages": messages, "temperature": temperature}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + get_key(),
    }
    url = API_BASE.rstrip("/") + "/chat/completions"

    last_err = None
    for attempt in range(max_retries + 1):
        started = time.time()
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            if e.code >= 500 and attempt < max_retries:
                last_err = f"HTTP {e.code}: {detail}"
                time.sleep(1.5 * (attempt + 1))
                continue
            raise LLMError(f"HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < max_retries:
                last_err = f"network: {e}"
                time.sleep(1.5 * (attempt + 1))
                continue
            raise LLMError(f"network: {e}") from None

        latency_ms = int((time.time() - started) * 1000)
        usage = data.get("usage") or {}
        meta = {
            "latency_ms": latency_ms,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "model": data.get("model", MODEL),
            "attempt": attempt,
        }
        return data["choices"][0]["message"], meta

    raise LLMError(last_err or "unknown error")
