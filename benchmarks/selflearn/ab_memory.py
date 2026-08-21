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
  同一个话题下、但一个字不涉及那条教训**的建议。不这么做的话,阳性结果可以被读成
  「系统提示词更长」而不是「这条知识」。
- 每个用例的判据**在跑之前就定死**,写在 `CASES` 里,不是事后挑的。

**一个用例只能证一条记忆。** 第四十八节量的是 `dropna` 那一条,而那三行是我**挑过的**
(任务 14 写出来、事后看确实对)—— 它量的是自学习的上限,不是期望值。
所以这里做成多用例:每加一条真实复盘写出来的记忆行,就多一个数据点。

跑法(要联网、要 key,**必须从仓库根目录启动** —— `.env` 是从启动目录读的):
    .venv/Scripts/python.exe benchmarks/selflearn/ab_memory.py --case dropna --n 20
    .venv/Scripts/python.exe benchmarks/selflearn/ab_memory.py --case all --n 20
"""
import argparse
import csv
import glob
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

# 三组共有的部分 —— 真实 memory.md 里那种「跟这个任务无关但确实在」的行
BASE_MEMORY = """# 记忆

- 写脚本前先确认解释器:项目里的 .venv 和系统 python 不是一个环境
- 生成文件统一放 workspace 下,不要写到项目根目录
- 中文内容的文件一律显式指定 encoding 为 utf-8,Windows 默认是 GBK
"""


# ── 用例 1:dropna ────────────────────────────────────────────────────────────
N_ROWS, N_BLANK_DEPT, N_BLANK_SALARY = 50, 3, 4


def setup_dropna(ws: str) -> None:
    """固定种子 —— 两组、每一次运行拿到的是**逐字节相同**的一份数据。"""
    rnd = random.Random(20260821)
    depts = ["研发", "销售", "市场", "人事", "财务", "运维"]
    rows = [{"emp_id": "E%03d" % (i + 1), "name": "员工%02d" % (i + 1),
             "department": rnd.choice(depts),
             "salary": str(rnd.randrange(8000, 30000, 500))} for i in range(N_ROWS)]
    for i in range(N_BLANK_DEPT):                      # 空部门:groupby 默认会丢掉这几行
        rows[i * 7]["department"] = ""
    for i in range(N_BLANK_SALARY):                    # 空薪资:mean() 会静悄悄换分母
        rows[i * 11 + 3]["salary"] = ""
    with open(os.path.join(ws, "employees.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["emp_id", "name", "department", "salary"])
        w.writeheader()
        w.writerows(rows)


_INT = re.compile(r"^([0-9]{1,4})$")
_TOTAL_WORDS = ("合计", "总计", "小计", "总数", "汇总", "全部", "总", "计")


def score_dropna(ws: str) -> dict:
    """判据:报告表格里各分组人数相加 == 50。

    这是第九节 ② 当时用的同一条判据(「6 个分组人数相加 12+11+10+9+5+3 = 50」)。
    用「代码里有没有 dropna=False」当判据是错的 —— `fillna` 之后再分组也是对的解法,
    那条判据会把对的判成错的。

    表格找不到、或者一列数都解析不出来 —— 记 None,**不记 0**:「没交报告」和
    「交了但算错」是两件事,合并成一个数就没法说清失败在哪。"""
    p = os.path.join(ws, "dept_salary.md")
    if not os.path.exists(p):
        return {"total": None, "ok": False, "why": "没有 dept_salary.md"}
    counts = []
    for line in open(p, encoding="utf-8", errors="replace").read().splitlines():
        if line.count("|") < 2:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not _INT.match(cells[1]):
            continue
        # 合计行要按**标签**认,不能按「等于其余之和」认:后者在「某一组正好占一半」时
        # 会把一个真分组当成合计减掉,把对的判成错的。
        if any(w in cells[0] for w in _TOTAL_WORDS):
            continue
        counts.append(int(cells[1]))
    if not counts:
        return {"total": None, "ok": False, "why": "表格里没解析出人数列"}
    return {"total": sum(counts), "ok": sum(counts) == N_ROWS, "why": ""}


# ── 用例 2:备份后缀 ──────────────────────────────────────────────────────────
def setup_bak(ws: str) -> None:
    rnd = random.Random(20260822)
    lv = ["INFO", "WARN", "ERROR"]
    out = []
    for i in range(120):
        if i % 17 == 5:
            out.append("")                              # 空行:要清掉的脏数据
        elif i % 23 == 7:
            out.append("2026-08-0%d 10:%02d:%02d [INF0] 拼错的级别" % (i % 9 + 1, i % 60, i % 60))
        else:
            out.append("2026-08-0%d 10:%02d:%02d [%s] 事件 %d"
                       % (i % 9 + 1, i % 60, i % 60, rnd.choice(lv), i))
    with open(os.path.join(ws, "app.log"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


def score_bak(ws: str) -> dict:
    """判据:清洗完之后 workspace 里有一个 `.bak` 备份。

    记忆行的原话就是「备份文件必须使用 .bak 后缀」,所以判据认这个后缀。
    同时记一个更宽的 `any_backup`(任何看着像备份的文件)—— 两个数分开记,
    才说得清「没备份」和「备份了但没按那条规矩命名」。"""
    names = [os.path.basename(p) for p in glob.glob(os.path.join(ws, "*"))]
    bak = [n for n in names if n.lower().endswith(".bak")]
    wide = [n for n in names if n != "app.log" and re.search(
        r"\.bak|\.orig|\.backup|backup|备份|_old|\.old|copy", n, re.I)]
    return {"ok": bool(bak), "any_backup": bool(bak or wide),
            "files": sorted(names), "why": "" if bak else "没有 .bak 文件"}


# ── 用例 3:cmd 下区分目录和文件 ──────────────────────────────────────────────
# 名字是**故意骗人**的:目录叫 report.md,文件叫 data —— 靠扩展名猜必错。
_ENTRIES = [("report.md", "dir"), ("archive.txt", "dir"), ("data", "file"),
            ("notes", "file"), ("build", "dir"), ("readme", "file")]


def setup_dir(ws: str) -> None:
    for name, kind in _ENTRIES:
        p = os.path.join(ws, name)
        if kind == "dir":
            os.makedirs(p, exist_ok=True)
            with open(os.path.join(p, "inner.txt"), "w", encoding="utf-8") as f:
                f.write("x\n")
        else:
            with open(p, "w", encoding="utf-8") as f:
                f.write("some content\n" * 3)


def score_dir(ws: str) -> dict:
    """判据:listing.md 把六个条目**全部**分类正确。名字是故意骗人的,靠扩展名猜必错。"""
    p = os.path.join(ws, "listing.md")
    if not os.path.exists(p):
        return {"ok": False, "right": None, "why": "没有 listing.md"}
    text = open(p, encoding="utf-8", errors="replace").read()
    right = 0
    for name, kind in _ENTRIES:
        # 取**第一条提到这个名字**的行。`report.md` 是目录,所以「按扩展名猜」在这里必错 ——
        # 这正是这个用例要拦的那种解法。
        line = next((l for l in text.splitlines() if name in l), "")
        said_dir = ("目录" in line or "文件夹" in line or "<DIR>" in line.upper())
        said_file = ("文件" in line.replace("文件夹", "")) and not said_dir
        if (said_dir or said_file) and (kind == "dir") == said_dir:
            right += 1
    return {"ok": right == len(_ENTRIES), "right": right,
            "why": "" if right == len(_ENTRIES) else "%d/%d 分类对" % (right, len(_ENTRIES))}


CASES = {
    # 每条 learned 都是**现在仓库 memory.md 里逐字存在的复盘写出来的行**,不是我编的。
    "dropna": {
        "task": ("读 workspace 里的 employees.csv,按 department 分组统计每个部门的人数和平均薪资,"
                 "结果写成 dept_salary.md,用一个三列表格:部门、人数、平均薪资。"),
        "setup": setup_dropna, "score": score_dropna,
        "learned": """- CSV分组统计时必须使用 dropna=False 参数，否则空值记录会被跳过，导致分组总和小于总行数
- 部门薪资分析中，空部门字段应标记为"空值"分组，薪资字段空值用 pd.to_numeric 的 errors 参数处理
- 分组聚合时使用 `dropna=False` 确保空值被包含，分组人数之和 = 总行数
""",
        "placebo": """- CSV分组统计时列名先做strip处理，否则表头带空格会让按列名取数全部取空，导致统计结果为零
- 部门薪资分析中，薪资列应先确认单位是元还是千元，跨表对比前把量纲统一到同一个口径上
- 分组聚合后按人数从多到少排序输出，报告里的表格顺序要稳定、可复现
""",
    },
    "bak": {
        "task": ("workspace 里的 app.log 有脏数据:有空行,还有把级别写成 INF0 的行。"
                 "写脚本清洗掉这两类问题,清洗完说明改了多少行。"),
        "setup": setup_bak, "score": score_bak,
        "learned": """- 修复日志脏数据时，备份文件必须使用 .bak 后缀，修复前统计原始空白行数用于验证修复结果
""",
        "placebo": """- 修复日志脏数据时，按级别分别统计修复前后的条数，用两次统计的差值验证修复结果
""",
    },
    "dirtype": {
        "task": ("workspace 下有六个条目,名字看不出是目录还是文件。查清楚每一个到底是哪种,"
                 "结果写成 listing.md,两列:名字、类型(目录或文件)。"),
        "setup": setup_dir, "score": score_dir,
        "learned": """- cmd 下区分目录和文件用 dir /a <名字>：目录显示 <DIR>、文件显示字节大小；dir /b 只列名字，判断不了类型
""",
        "placebo": """- cmd 下批量看条目用 dir /o:n <目录>：按名字排序输出，配合 /p 分页避免刷屏丢内容
""",
    },
}


def one_run(case: str, ws: str) -> dict:
    """在**这个进程**里跑一次(由驱动方 spawn 出来,环境变量已经就位)。"""
    sys.path.insert(0, ROOT)
    import agent as A
    import console_ui
    A.ui = console_ui
    c = CASES[case]
    client, model = A.make_client()
    A.load_dynamic_tools()
    state = {"mode": "bypass", "allow": set(), "view": "normal", "asked": c["task"]}
    t0, err = time.time(), ""
    try:
        A.agent_turn(client, model, [{"role": "user", "content": c["task"]}], state, top=True)
    except Exception as e:                              # 一次崩掉不该拖垮整组
        err = "%s: %s" % (type(e).__name__, e)
    tok = state.get("last_tok") or {}
    out = {"model": model, "secs": round(time.time() - t0, 1), "err": err,
           "in": tok.get("in", 0), "out": tok.get("out", 0), "steps": tok.get("steps", 0)}
    out.update(c["score"](ws))
    print("__RESULT__" + json.dumps(out, ensure_ascii=False))
    return out


def spawn(case: str, arm: str, i: int, keep: str) -> dict:
    c = CASES[case]
    home = tempfile.mkdtemp(prefix="ab-%s-%s-%d-" % (case, arm, i))
    ws = os.path.join(home, "workspace")
    os.makedirs(ws)
    os.makedirs(os.path.join(home, "skills"))            # 空的 —— 技能不许插手
    with open(os.path.join(home, "memory.md"), "w", encoding="utf-8") as f:
        f.write(BASE_MEMORY + c["learned" if arm == "learned" else "placebo"])
    c["setup"](ws)

    env = dict(os.environ, TALOS_HOME=home, TALOS_WORKSPACE=ws, TALOS_MAX_STEPS="40",
               PYTHONIOENCODING="utf-8")
    # cwd 必须是仓库根 —— `.env` 是从**启动目录**读的,key 在那儿。
    # `.env` 改不了 TALOS_HOME / TALOS_WORKSPACE(见 `_DOTENV_NEVER`),所以隔离是稳的。
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "--run", case, ws],
                       cwd=ROOT, env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=1800)
    hits = [l for l in (r.stdout or "").splitlines() if l.startswith("__RESULT__")]
    res = (json.loads(hits[-1][10:]) if hits else
           {"ok": False, "why": "子进程没吐结果",
            "err": ((r.stderr or "") + (r.stdout or ""))[-400:]})
    res.update(case=case, arm=arm, i=i)
    if keep:
        shutil.copytree(ws, os.path.join(keep, "%s-%s-%d" % (case, arm, i)), dirs_exist_ok=True)
    shutil.rmtree(home, ignore_errors=True)
    return res


def fisher_one_sided(a: int, b: int, c: int, d: int) -> float:
    """2x2 单侧 Fisher 精确检验。n 只有几十,卡方在这个量级是错的工具。
    表格是 [[实验组对, 实验组错], [对照组对, 对照组错]],算的是「实验组至少这么好」的概率。"""
    from math import comb
    n, r1, c1 = a + b + c + d, a + b, a + c
    return sum(comb(r1, k) * comb(n - r1, c1 - k)
               for k in range(a, min(r1, c1) + 1)) / comb(n, c1)


def run_case(case: str, n: int, keep: str) -> list:
    rows = []
    # 交替跑,不是先跑完一组再跑另一组 —— 服务端的模型版本、负载、限流都会随时间漂,
    # 分块跑的话那个漂移整个落进组间差。
    for i in range(n):
        for arm in ("learned", "placebo"):
            r = spawn(case, arm, i, keep)
            rows.append(r)
            print("%-8s %-8s #%-3d %s  %3d 步 / %7d in / %4.0f 秒  %s"
                  % (case, arm, i, "对" if r.get("ok") else "错", r.get("steps", 0),
                     r.get("in", 0), r.get("secs", 0),
                     (r.get("why") or r.get("err") or "")[:50]))
        with open(os.path.join(HERE, "ab_memory.%s.json" % case), "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)   # 每对存一次,中途断了也不白跑
    return rows


def report(case: str, rows: list) -> None:
    la = [r for r in rows if r["arm"] == "learned"]
    pl = [r for r in rows if r["arm"] == "placebo"]
    a_ok, p_ok = sum(1 for r in la if r["ok"]), sum(1 for r in pl if r["ok"])
    p = fisher_one_sided(a_ok, len(la) - a_ok, p_ok, len(pl) - p_ok)
    print("  %-9s 有记忆 %2d/%-2d   安慰剂 %2d/%-2d   差 %+5.0f 个点   单侧 Fisher p = %.5f"
          % (case, a_ok, len(la), p_ok, len(pl),
             100.0 * (a_ok / max(1, len(la)) - p_ok / max(1, len(pl))), p))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", nargs=2, metavar=("CASE", "WS"))
    ap.add_argument("--case", default="all", help="用例名,或 all")
    ap.add_argument("--n", type=int, default=20, help="每组跑几次")
    ap.add_argument("--keep", default="", help="把每次运行的 workspace 复制到这个目录")
    a = ap.parse_args()
    if a.run:
        one_run(*a.run)
        return 0

    if a.keep:
        os.makedirs(a.keep, exist_ok=True)
    names = list(CASES) if a.case == "all" else [a.case]
    done = {}
    for case in names:
        done[case] = run_case(case, a.n, a.keep)

    print("\n判据各自写在 CASES 里,跑之前定死的")
    for case in names:
        report(case, done[case])
    tot = [r for rows in done.values() for r in rows]
    print("  总花费 %d in / %d out token,%.0f 秒"
          % (sum(r.get("in", 0) for r in tot), sum(r.get("out", 0) for r in tot),
             sum(r.get("secs", 0) for r in tot)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
