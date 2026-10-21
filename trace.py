"""结构化 trace：每一步一行 JSONL，可回放、可统计、可直接作为评测输入。

为什么不用日志打印：评测要能"拿一条 case 逐帧回看"，日志是给人看的，trace 是给程序用的。
"""
import json
import os
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
TRACE_DIR = os.path.join(_HERE, "traces")


class Tracer:
    def __init__(self, case_id, path=None, echo=False):
        self.case_id = case_id
        self.echo = echo
        self.t0 = time.time()
        self.steps = 0
        os.makedirs(TRACE_DIR, exist_ok=True)
        self.path = path or os.path.join(TRACE_DIR, f"{case_id}.jsonl")
        self._fh = open(self.path, "w", encoding="utf-8")

    def log(self, kind, **kw):
        rec = {"case": self.case_id, "ts": round(time.time() - self.t0, 3), "type": kind}
        rec.update(kw)
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()
        if self.echo:
            brief = {k: v for k, v in rec.items() if k in ("ts", "type", "step", "name", "reason", "is_error")}
            print("   ", json.dumps(brief, ensure_ascii=False))
        return rec

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass
