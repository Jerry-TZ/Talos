#!/usr/bin/env python
"""上下文重发的账:一轮里多走一步 vs 多读一段,各自要重发多少字符。

**为什么要有这个脚本。** FINDINGS「token 由步数决定,不由读了多少决定」原来的证据是
4 轮真实任务(200 步 → 5.96M,30 步 → 845K)。第四十三节指出那 4 轮里**步数、读取次数、
历史长度、失败路径同时变** —— 从那种数据里读不出「哪个变量在推动花费」。
这里把两个变量拆开:**读取量固定只动步数,步数固定只动读取量。**

**模型换成脚本化的假客户端。** 要控制步数就不能让模型自己决定走几步 —— 那正是原来
那份数据的病。被测的仍是真的 `agent_turn`:真的 `_prune_old_tool_results`、
真的 `maybe_compact`、真的消息累积、真的 `_with_recall` 插点。

**量的是字符不是 token。** 每一步实际发出去的 payload(system + 全部消息 + tool_calls
+ 工具表)有多少字符,精确可数;token 要么得联网、要么得估。同一份内容的字符/token 比
在各格之间是常数,而这个实验全部结论都是**格与格之比**,常数会约掉。

跑法(不联网、不要 key,几秒跑完):
    .venv/Scripts/python.exe benchmarks/context/resend_cost.py
"""
import json
import os
import shutil
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="resend-")
os.environ["TALOS_HOME"] = _TMP                 # 必须在 import agent 之前 —— 路径常量在模块级算好
os.environ["TALOS_WORKSPACE"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import agent as A                                                          # noqa: E402
import console_ui                                                          # noqa: E402


# ---- 假模型:够 agent_turn 用的最小响应壳 -------------------------------------
class _F:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _C:
    def __init__(self, i, name, arguments):
        self.id, self.type, self.function = "call_%d" % i, "function", _F(name, arguments)


class _M:
    def __init__(self, content, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls


class _Ch:
    def __init__(self, m):
        self.message = m


class _U:
    def __init__(self, n):
        self.prompt_tokens, self.completion_tokens = n, 0
        self.prompt_tokens_details = None


class _R:
    def __init__(self, m, n):
        self.choices, self.usage = [_Ch(m)], _U(n)


def _payload_chars(kw: dict) -> int:
    """一次调用真正发出去多少字符。**tool_calls 和工具表也算** —— 它们每步照发不误,
    只数 content 会把「步数」这一侧系统性地少算,而这个实验就是在比这两侧。"""
    n = sum(len(str(m.get("content") or "")) for m in kw["messages"])
    n += sum(len(json.dumps(m["tool_calls"], ensure_ascii=False))
             for m in kw["messages"] if m.get("tool_calls"))
    return n + len(json.dumps(kw.get("tools") or [], ensure_ascii=False))


def run_cell(steps: int, read_bytes: int) -> dict:
    """跑一轮:走 `steps` 步,每步 read_file 一个 `read_bytes` 大的**新**文件。

    每步换一个文件是有意的 —— `_read_guard` 按路径计数(同一文件 max(6, 2*页数) 次封顶),
    读同一个会在第 6 步被拦下,那就成了「测守卫」而不是「测重发」。"""
    ws = tempfile.mkdtemp(prefix="cell-", dir=_TMP)
    # 行长封顶 500:`READ_MAX_LINES = 250` 是按**行**封顶的,行太短一次读不完整份文件,
    # 「读取量」这个自变量就被分页悄悄改回去了 —— 那就成了「测分页」。
    w = min(500, read_bytes)
    for i in range(steps):
        with open(os.path.join(ws, "f%d.txt" % i), "w", encoding="utf-8") as f:
            f.write(("x" * (w - 1) + "\n") * max(1, read_bytes // w))

    calls = []
    box = {"n": 0}
    got: list = []          # 操作检查:read_file **实际**回了多少字符 —— 自变量要量,不能假设

    def _chat(client, **kw):
        n = _payload_chars(kw)
        if "tools" not in kw:                       # maybe_compact 的摘要调用,不带工具表
            # 假摘要多长是这个实验唯一一个「我拍的数」,而它只进 20 KB 那一格(别的格
            # 压根不触发压缩)。查过敏感度:200 / 800 / 2000 字符 → 567,663 / 595,263 /
            # 650,463,**四个结论一个都不翻**(门槛反常那两格连压缩都没碰到,
            # 4 KB 那格仍然是全场最贵)。
            calls.append(("compact", n))
            return _R(_M("【摘要】" + "。" * 200), n)
        i = box["n"]
        box["n"] += 1
        calls.append(("step", n))
        if kw["messages"][-1].get("role") == "tool":
            got.append(len(str(kw["messages"][-1].get("content") or "")))
        if i < steps:
            return _R(_M("", [_C(i, "read_file",
                                 json.dumps({"path": os.path.join(ws, "f%d.txt" % i)}))]), n)
        return _R(_M("做完了。"), n)

    old_chat, old_ws, A._chat, A.WORKSPACE = A._chat, A.WORKSPACE, _chat, os.path.realpath(ws)
    quiet, sys.stdout = sys.stdout, open(os.devnull, "w", encoding="utf-8")
    try:
        state = {"mode": "bypass", "allow": set(), "view": "normal", "asked": "读文件"}
        A.agent_turn(None, "fake", [{"role": "user", "content": "读文件"}], state)
    finally:
        A._chat, A.WORKSPACE = old_chat, old_ws
        sys.stdout.close()
        sys.stdout = quiet

    return {"steps": steps, "read_bytes": read_bytes,
            "read_chars": got[0] if got else 0,       # 一次 read_file 实际回来多少字符
            "sent": sum(n for _, n in calls),
            "model_calls": len(calls),
            "compactions": sum(1 for k, _ in calls if k == "compact"),
            "compact_sent": sum(n for k, n in calls if k == "compact")}


def main() -> int:
    A.ui = console_ui
    # 500 和 1000 这两格是**冲着 `_prune_old_tool_results` 的 600 字符门槛去的** ——
    # 门槛底下的工具输出永远不会被换成「已省略」,于是它整轮都在被重发。
    grid = [4, 8, 16, 32], [200, 500, 1000, 4000, 20000]
    rows = [run_cell(s, r) for r in grid[1] for s in grid[0]]

    print("每格 = 一整轮实际发出去的字符总数(含 system / 工具表 / tool_calls)\n")
    print("%-12s %s" % ("读取量\步数", "".join("%12d" % s for s in grid[0])))
    for r in grid[1]:
        cells = [x for x in rows if x["read_bytes"] == r]
        print("%-12s %s" % ("%d B" % r, "".join("%12s" % "{:,}".format(c["sent"]) for c in cells)))

    print("\n压缩次数(maybe_compact 真的叫了模型的次数)")
    print("%-12s %s" % ("读取量\步数", "".join("%12d" % s for s in grid[0])))
    for r in grid[1]:
        cells = [x for x in rows if x["read_bytes"] == r]
        print("%-12s %s" % ("%d B" % r, "".join("%12d" % c["compactions"] for c in cells)))

    print("\n单因子边际:")
    for r in grid[1]:
        a = next(x for x in rows if x["read_bytes"] == r and x["steps"] == 4)
        b = next(x for x in rows if x["read_bytes"] == r and x["steps"] == 32)
        print("  读取量固定 %6d B,步数 4 → 32(8x):发送量 %.2fx" % (r, b["sent"] / a["sent"]))
    for s in grid[0]:
        a = next(x for x in rows if x["read_bytes"] == 200 and x["steps"] == s)
        b = next(x for x in rows if x["read_bytes"] == 20000 and x["steps"] == s)
        print("  步数固定 %2d 步,读取量 200 B → 20 KB(100x):发送量 %.2fx" % (s, b["sent"] / a["sent"]))
    # 弹性 = log(产出倍数) / log(投入倍数)。两个自变量量纲不同(步 vs 字节),
    # 直接比「涨了多少倍」不成立;弹性是无量纲的,可以并排放。
    import math
    lo, hi = grid[1][0], grid[1][-1]
    e_step = [math.log(next(x["sent"] for x in rows if x["read_bytes"] == r and x["steps"] == 32)
                       / next(x["sent"] for x in rows if x["read_bytes"] == r and x["steps"] == 4))
              / math.log(8) for r in grid[1]]
    e_read = [math.log(next(x["sent"] for x in rows if x["read_bytes"] == hi and x["steps"] == s)
                       / next(x["sent"] for x in rows if x["read_bytes"] == lo and x["steps"] == s))
              / math.log(hi / lo) for s in grid[0]]
    print("\n弹性(无量纲,可以并排比):")
    print("  步数弹性   %.2f ~ %.2f   (>1 = 超线性:每步都要把之前的全重发一遍)"
          % (min(e_step), max(e_step)))
    print("  读取量弹性 %.2f ~ %.2f" % (min(e_read), max(e_read)))
    print("  → 步数的弹性是读取量的 %.1f ~ %.1f 倍"
          % (min(e_step) / max(e_read), max(e_step) / min(e_read)))

    print("\n600 字符门槛的反常:同样 32 步,读得少反而发得多")
    for r in grid[1]:
        c = next(x for x in rows if x["read_bytes"] == r and x["steps"] == 32)
        print("  单次读 %6d 字符 → 整轮发送 %11s  %s"
              % (c["read_chars"], "{:,}".format(c["sent"]),
                 "(门槛底下,永不省略)" if c["read_chars"] <= 600 else ""))

    print("\n操作检查 —— 一次 read_file 实际回来的字符数(自变量真的动了吗):")
    for r in grid[1]:
        print("  标称 %6d B → 实际 %6d 字符" % (r, next(x["read_chars"] for x in rows if x["read_bytes"] == r)))

    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "resend_cost.json"),
              "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
