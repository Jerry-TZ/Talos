#!/usr/bin/env python
"""自学习到底值不值钱:同一个任务、同一个模型,memory 里有没有那几行教训,各跑 n 次。

**为什么要有这个脚本。** FINDINGS 第九节 ② 写着「自学习第一次拿出可验证的收益」——
任务 14 掉进 pandas `groupby` 默认 `dropna=True` 的坑(分组和 48 != 总数 50),复盘把教训
写进 `memory.md`,任务 15 面对同一个坑第一次就写对了。第四十三节指出这是 **n=1 且无对照**,
而且同一页的 ① 刚用同一种证据结构写了「这不足以归因」—— **判据用了两套。**

**这个脚本只做一件事:把「有没有那几行记忆」变成唯一的自变量。**

- 每一次运行都在**全新的临时 `TALOS_HOME`** 里:`skills/` 空的、`tools/` 空的、
  会话历史空的。技能会被检索进上下文,不清空的话它可能自己就把答案教了 ——
  那样测的就不是记忆。
- 两组的 `memory.md` **行数和字数接近**:对照组把那几行换成**同样长、同样像样、
  但一个字不涉及空值**的 CSV 建议。不这么做的话,阳性结果可以被读成「系统提示词更长」
  而不是「这条知识」。
- 判据在跑之前就定死:**报告里各分组人数相加 == 总行数**。这是第九节 ② 当时用的同一条
  判据(「6 个分组人数相加 12+11+10+9+5+3 = 50」),不是事后挑的。
  用「代码里有没有 dropna=False」当判据是错的 —— `fillna` 之后再分组也是对的解法,
  那条判据会把对的判成错的。

跑法(要联网、要 key,**必须从仓库根目录启动** —— `.env` 是从启动目录读的):
    .venv/Scripts/python.exe benchmarks/selflearn/ab_dropna.py --n 8
"""
import argparse
import csv
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

TASK = ("读 workspace 里的 employees.csv,按 department 分组统计每个部门的人数和平均薪资,"
        "结果写成 dept_salary.md,用一个三列表格:部门、人数、平均薪资。")

N_ROWS = 50
N_BLANK_DEPT = 3          # 就是这三行会被 groupby 默默丢掉
N_BLANK_SALARY = 4

# 两组共有的部分 —— 真实 memory.md 里那种「跟这个任务无关但确实在」的行
BASE_MEMORY = """# 记忆

- 写脚本前先确认解释器:项目里的 .venv 和系统 python 不是一个环境
- 生成文件统一放 workspace 下,不要写到项目根目录
- 中文内容的文件一律显式指定 encoding 为 utf-8,Windows 默认是 GBK
"""

# 实验组:任务 14 复盘真的写进 memory.md 的那几行(逐字照抄现在仓库里的 memory.md)
LEARNED = """- CSV分组统计时必须使用 dropna=False 参数，否则空值记录会被跳过，导致分组总和小于总行数
- 部门薪资分析中，空部门字段应标记为"空值"分组，薪资字段空值用 pd.to_numeric 的 errors 参数处理
- 分组聚合时使用 `dropna=False` 确保空值被包含，分组人数之和 = 总行数
"""

# 对照组:同样三行、长度相当、同样是「CSV 分组统计」这个话题下的具体建议,
# **但一个字都不涉及空值。** 安慰剂,不是空白 —— 否则测的是「系统提示词长短」。
PLACEBO = """- CSV分组统计时列名先做strip处理，否则表头带空格会让按列名取数全部取空，导致统计结果为零
- 部门薪资分析中，薪资列应先确认单位是元还是千元，跨表对比前把量纲统一到同一个口径上
- 分组聚合后按人数从多到少排序输出，报告里的表格顺序要稳定、可复现
"""


def make_csv(path: str) -> None:
    """固定种子 —— 两组、每一次运行拿到的是**逐字节相同**的一份数据。"""
    rnd = random.Random(20260821)
    depts = ["研发", "销售", "市场", "人事", "财务", "运维"]
    rows = []
    for i in range(N_ROWS):
        rows.append({"emp_id": "E%03d" % (i + 1),
                     "name": "员工%02d" % (i + 1),
                     "department": rnd.choice(depts),
                     "salary": str(rnd.randrange(8000, 30000, 500))})
    for i in range(N_BLANK_DEPT):                      # 空部门:groupby 默认会丢掉这几行
        rows[i * 7]["department"] = ""
    for i in range(N_BLANK_SALARY):                    # 空薪资:mean() 会静悄悄换分母
        rows[i * 11 + 3]["salary"] = ""
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["emp_id", "name", "department", "salary"])
        w.writeheader()
        w.writerows(rows)


_INT = re.compile(r"^([0-9]{1,4})$")


def score(ws: str) -> dict:
    """判据:报告表格里各分组人数相加 == 50。

    只认 markdown 表格的**第二列**。表格找不到、或者一列数都解析不出来 —— 记 None,
    **不记 0**:「没交报告」和「交了但算错」是两件事,合并成一个数就没法说清失败在哪。"""
    p = os.path.join(ws, "dept_salary.md")
    if not os.path.exists(p):
        return {"total": None, "ok": False, "why": "没有 dept_salary.md"}
    text = open(p, encoding="utf-8", errors="replace").read()
    counts = []
    for line in text.splitlines():
        if line.count("|") < 2:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        m = _INT.match(cells[1])
        if not m:                                      # 人数是整数;平均薪资那列带小数或逗号
            continue
        # 合计行要按**标签**认,不能按「等于其余之和」认:后者在「某一组正好占一半」时
        # 会把一个真分组当成合计减掉,把对的判成错的。
        if any(w in cells[0] for w in ("合计", "总计", "小计", "总数", "汇总", "全部", "总", "计")):
            continue
        counts.append(int(m.group(1)))
    if not counts:
        return {"total": None, "ok": False, "why": "表格里没解析出人数列"}
    return {"total": sum(counts), "ok": sum(counts) == N_ROWS, "why": "", "cells": counts}


def one_run(home: str, ws: str) -> dict:
    """在**这个进程**里跑一次(由驱动方 spawn 出来,环境变量已经就位)。"""
    sys.path.insert(0, ROOT)
    import agent as A
    import console_ui
    A.ui = console_ui
    client, model = A.make_client()
    A.load_dynamic_tools()
    state = {"mode": "bypass", "allow": set(), "view": "normal", "asked": TASK}
    t0, err = time.time(), ""
    try:
        A.agent_turn(client, model, [{"role": "user", "content": TASK}], state, top=True)
    except Exception as e:                              # 一次崩掉不该拖垮整组
        err = "%s: %s" % (type(e).__name__, e)
    tok = state.get("last_tok") or {}
    out = {"model": model, "secs": round(time.time() - t0, 1), "err": err,
           "in": tok.get("in", 0), "out": tok.get("out", 0), "steps": tok.get("steps", 0),
           "calls": tok.get("calls", 0)}
    out.update(score(ws))
    print("__RESULT__" + json.dumps(out, ensure_ascii=False))
    return out


def spawn(arm: str, i: int, keep: str) -> dict:
    home = tempfile.mkdtemp(prefix="ab-%s-%d-" % (arm, i))
    ws = os.path.join(home, "workspace")
    os.makedirs(ws)
    os.makedirs(os.path.join(home, "skills"))            # 空的 —— 技能不许插手
    with open(os.path.join(home, "memory.md"), "w", encoding="utf-8") as f:
        f.write(BASE_MEMORY + (LEARNED if arm == "learned" else PLACEBO))
    make_csv(os.path.join(ws, "employees.csv"))

    env = dict(os.environ, TALOS_HOME=home, TALOS_WORKSPACE=ws, TALOS_MAX_STEPS="40",
               PYTHONIOENCODING="utf-8")
    # cwd 必须是仓库根 —— `.env` 是从**启动目录**读的,key 在那儿。
    # `.env` 改不了 TALOS_HOME / TALOS_WORKSPACE(见 `_DOTENV_NEVER`),所以隔离是稳的。
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "--run", home, ws],
                       cwd=ROOT, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=1800)
    hits = [l for l in (r.stdout or "").splitlines() if l.startswith("__RESULT__")]
    res = (json.loads(hits[-1][10:]) if hits else
           {"ok": False, "total": None, "why": "子进程没吐结果",
            "err": ((r.stderr or "") + (r.stdout or ""))[-400:]})
    res.update(arm=arm, i=i)
    if keep:
        shutil.copytree(ws, os.path.join(keep, "%s-%d" % (arm, i)), dirs_exist_ok=True)
    shutil.rmtree(home, ignore_errors=True)
    return res


def fisher_one_sided(a: int, b: int, c: int, d: int) -> float:
    """2x2 单侧 Fisher 精确检验。n 只有十几,卡方在这个量级是错的工具。
    表格是 [[实验组对, 实验组错], [对照组对, 对照组错]],算的是「实验组至少这么好」的概率。"""
    from math import comb
    n, r1, c1 = a + b + c + d, a + b, a + c
    return sum(comb(r1, k) * comb(n - r1, c1 - k)
               for k in range(a, min(r1, c1) + 1)) / comb(n, c1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", nargs=2, metavar=("HOME", "WS"))
    ap.add_argument("--n", type=int, default=8, help="每组跑几次")
    ap.add_argument("--keep", default="", help="把每次运行的 workspace 复制到这个目录")
    a = ap.parse_args()
    if a.run:
        one_run(*a.run)
        return 0

    if a.keep:
        os.makedirs(a.keep, exist_ok=True)
    rows = []
    # 交替跑,不是先跑完一组再跑另一组 —— 服务端的模型版本、负载、限流都会随时间漂,
    # 分块跑的话那个漂移整个落进组间差。
    for i in range(a.n):
        for arm in ("learned", "placebo"):
            r = spawn(arm, i, a.keep)
            rows.append(r)
            print("%-8s #%d  分组和=%-5s %s  %3d 步 / %8d in / %5.0f 秒  %s"
                  % (arm, i, r.get("total"), "对" if r.get("ok") else "错",
                     r.get("steps", 0), r.get("in", 0), r.get("secs", 0),
                     (r.get("why") or r.get("err") or "")[:60]))
        with open(os.path.join(HERE, "ab_dropna.json"), "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)   # 每对存一次,中途断了也不白跑

    la = [r for r in rows if r["arm"] == "learned"]
    pl = [r for r in rows if r["arm"] == "placebo"]
    a_ok, p_ok = sum(1 for r in la if r["ok"]), sum(1 for r in pl if r["ok"])
    print("\n判据:报告里各分组人数相加 == %d" % N_ROWS)
    print("  有那三行记忆  %d/%d" % (a_ok, len(la)))
    print("  安慰剂三行    %d/%d" % (p_ok, len(pl)))
    print("  单侧 Fisher 精确检验 p = %.4f"
          % fisher_one_sided(a_ok, len(la) - a_ok, p_ok, len(pl) - p_ok))
    print("  总花费 %d in / %d out token,%.0f 秒"
          % (sum(r.get("in", 0) for r in rows), sum(r.get("out", 0) for r in rows),
             sum(r.get("secs", 0) for r in rows)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
