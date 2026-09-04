"""摘掉一条规则,自检必须红 —— 红不了的规则等于没写。

这个文件不测任何功能,它测的是**别的判据在不在承重**。

为什么需要它:外部审计逐条把 `drawiocheck.py` 的规则换成 no-op,发现 21 条里有 5 条
删掉之后 `--selfcheck` 照样绿(含「孤立块」「悬空边」这种最基本的)。更难看的是,
上一轮审计发现 `MAX_FOLD_EDGES` 那个洞、当场补了检查,**但没补 case** —— 所以那条检查
随时可以原样烂回去,而没有任何人会知道。

**手工补 21 个 case 解决不了这件事**:下一条新规则照样可能没有 case。所以这里做的是
一条机检不变量 —— 用 AST 找出每一处报问题的出口,逐个摘掉、跑自检、要求它红。
新增规则自动被这条管住,不需要谁记得。

两个刻意的设计:
- **用 AST 摘,不用字符串替换。** 多行的 `problems.append(f"…"\n  f"…")` 用正则摘不干净,
  而且摘一半会变成语法错误 —— 那也会让自检"红",于是变异测试自己给出假绿。
- **变异体写到临时目录里跑,绝不改仓库里的文件。** 同一天里我已经两次把源文件留在
  变异态(一次是子进程路径写错,一次是探针脚本炸在 GBK 控制台的 emoji 上)。
  凡是"改一下再改回来"的流程,总有一次改不回来。
"""
import ast
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_report(node) -> bool:
    """`problems.append(...)` 语句 —— 判官报出一条问题的唯一出口。"""
    return (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "append"
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "problems")


class _Drop(ast.NodeTransformer):
    """把第 target 处出口换成 pass,其余原样。"""

    def __init__(self, target):
        self.target, self.seen = target, 0

    def visit_Expr(self, node):
        if _is_report(node):
            hit, self.seen = self.seen, self.seen + 1
            if hit == self.target:
                return ast.copy_location(ast.Pass(), node)
        return node


def _label(node) -> str:
    """给人看的规则名:取报文里第一段字面量,截短。"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value.strip():
            return sub.value.strip().split("{")[0].strip(" :,")[:28] or "?"
    return ast.unparse(node)[:28]


def _run(src: str, script: str, d: str) -> bool:
    """把这份源码写进临时目录跑一遍自检,返回「是不是绿的」。"""
    path = os.path.join(d, script)
    with open(path, "w", encoding="utf-8") as f:
        f.write(src)
    return subprocess.run([sys.executable, path, "--selfcheck"],
                          capture_output=True, cwd=HERE).returncode == 0


def _sweep(script: str):
    """逐条摘掉规则跑自检。返回 [(规则名, 摘掉后自检是否还绿)]。"""
    src = open(os.path.join(HERE, script), encoding="utf-8").read()
    # **必须按源码顺序排。** `ast.walk` 是广度优先,而 `NodeTransformer` 是深度优先 ——
    # 不排的话第 i 个变异体和第 i 个标签根本不是同一条规则,于是「几条没 case」是对的、
    # 「哪几条」全是错的。第一版就这么错了,而且它和外部审计的结论只重合两条,
    # **是那个分歧把它暴露出来的**,不是我自己看出来的。
    sites = sorted((n for n in ast.walk(ast.parse(src)) if _is_report(n)),
                   key=lambda n: (n.lineno, n.col_offset))
    assert sites, f"{script} 里一处 problems.append 都没找到 —— 这个扫描失效了"
    out = []
    with tempfile.TemporaryDirectory() as d:
        # 控制组:不摘任何东西,只走一遍 unparse。**这一步必须是绿的。**
        # 否则 `ast.unparse` 本身就把脚本弄坏了,后面每一个变异体都会因为同一个理由
        # 变红,于是整张表变成「条条都有 case」—— 一个永远报平安的扫描,比没有更糟。
        assert _run(ast.unparse(ast.parse(src)), script, d), (
            f"控制组就红了:`ast.unparse` 改变了 {script} 的行为,这个扫描的结论全部无效")
        for i, site in enumerate(sites):
            mutant = _Drop(i).visit(ast.parse(src))
            ast.fix_missing_locations(mutant)
            out.append((f"{script}:{site.lineno} {_label(site)}",
                        _run(ast.unparse(mutant), script, d)))
    return out


def test_every_rule_in_drawiocheck_has_a_case_that_dies_without_it():
    """21 条规则,摘掉任何一条,`--selfcheck` 都必须红。"""
    survivors = [name for name, still_green in _sweep("drawiocheck.py") if still_green]
    assert not survivors, (
        f"{len(survivors)} 条规则摘掉之后自检照样绿 —— 它们没有 case,等于没测:\n  "
        + "\n  ".join(survivors))


# ── 排版那一侧:旋钮拧了,判官得有反应 ────────────────────────────────────────
# drawiocheck 的规则是**语句**,摘掉就行;drawio_layout 没有"规则"这种东西,它的
# 行为藏在一组常数里。所以这边换个测法:把常数拧到一个错值,自检必须红。
# 不用改源码 —— 这些常数是模块全局,函数在调用时才查,`setattr` 就够了。
_KNOBS = {"GRID": 5, "VGAP": 20, "HGAP": 20, "COLGAP": 40, "MAX_ASPECT": 99.0,
          "BARYCENTER_ROUNDS": 0, "WIDTHS": (207, 287, 367), "PAD": 3, "LINE_H": 5}

# **判官没有对应规则的旋钮。** 这不是"样本还不够狠",硬把样本拧到能红是在造假绿,
# 所以写下来锁住:少一条是回归,多一条说明判官变强了,两种都该被这条测试拦下来问一句。
_BLIND = {
    "HGAP": "判官没有「同层块之间最小间距」这条规则 —— 任何样本都测不出来",
    "COLGAP": "栏距缩到 COL_GAP_MIN 以下时,判官认不出这是两栏,分栏那几条规则跟着一起"
              "失效(而不是报错)。**这是判官的设计缺口,不是样本的**:一条规则在输入退化时"
              "静默消失,比它不存在更糟",
    "PAD": "它只改 `_fit` 算出的行数,而判官用自己的 PAD 重算一遍 —— 两边同时变,差不出来",
    "LINE_H": "同上。判官的「文字竖着放不下」要 3 行才触发,而这些标签在判官眼里是 2 行,"
              "余量吃不掉这个差",
}


def _knob_sweep():
    """返回 {旋钮: 自检有没有注意到}。"""
    import contextlib, importlib, io as _io
    out = {}
    for name, bad in _KNOBS.items():
        for m in ("drawio_layout", "drawiocheck"):
            sys.modules.pop(m, None)
        sys.path.insert(0, HERE)
        layout = importlib.import_module("drawio_layout")
        setattr(layout, name, bad)
        try:
            with contextlib.redirect_stdout(_io.StringIO()):
                layout._selfcheck()
            out[name] = False
        except AssertionError:
            out[name] = True
        finally:
            for m in ("drawio_layout", "drawiocheck"):
                sys.modules.pop(m, None)
    return out


def test_the_layout_sample_is_hard_enough_to_notice_its_own_knobs():
    """排版的自检样本必须难到:任何一个旋钮拧错,判官都会叫。

    原来那个 6 块样本 9 个旋钮里 8 个没反应 —— 只有 4 层、标签全是两三个字的单行,
    `_fit` 永远停在第一档、`_snap` 永远无事可做、`_columns` 永远不用折栏、
    层内最多 2 个块所以没有交叉可减。**样本软的时候,后面所有断言都是摆设。**
    加了「会折行的长标签」和「又高又窄的长链」两个样本之后是 5/9,剩下四个见 `_BLIND`。"""
    got = _knob_sweep()
    blind = {k for k, noticed in got.items() if not noticed}
    assert blind == set(_BLIND), (
        f"盲区变了。现在盲的: {sorted(blind)};记录在案的: {sorted(_BLIND)}\n"
        f"  多出来的 = 回归(样本变软了);少掉的 = 判官变强了,把它从 _BLIND 里删掉")


# ── agent.py 那一侧:守卫是 `raise`,判官是这 270 多条判据本身 ──────────────────
# **这个仓库最贵的东西没有这条不变量顶着。** 上面两支扫的是 drawio 那两个脚本,
# 而 `agent.py` 的每一道闸,一直只靠我每次手工摘一个部件、看一眼红不红 —— 而
# FINDINGS 自己写着「变异体这条路有效,**而它的覆盖面是我手写的**」。手写的覆盖面
# 就是这份记录里所有结论共同的地基,也是唯一没人查的那一块。
#
# 三处跟上面不一样,每一处都是被逼出来的:
# ① 判官不是 `--selfcheck` 而是**整套判据**。agent 的 selfcheck 只覆盖工具/档位/
#    schema/工作区闸,拿它当判官会把「selfcheck 没覆盖」全报成「没有 case」。
# ② 变异体跑在**整个仓库的副本**里,不是单文件临时目录:conftest 里写死了
#    `sys.path.insert(0, HERE)`,PYTHONPATH 抢不过它,所以只能让 cwd 就是副本。
# ③ 副本里必须去掉两支判据:`test_mutation.py`(否则每个变异体里又套一轮全扫)和
#    `test_docs.py`(`ast.unparse` 会把注释全剥掉,行数表条条对不上 —— 那是**假红**,
#    会让每个变异体都"被抓到",整张表变成永远报平安)。控制组那一关就是查这个的。
_MUT_IGNORE = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", ".talos")
_MUT_ARGS = ["-m", "pytest", "tests", "-x", "-q", "-p", "no:cacheprovider",
             "--ignore=tests/test_mutation.py", "--ignore=tests/test_docs.py"]


class _DropRaise(ast.NodeTransformer):
    def __init__(self, target):
        self.target, self.seen = target, 0

    def visit_Raise(self, node):
        hit, self.seen = self.seen, self.seen + 1
        return ast.copy_location(ast.Pass(), node) if hit == self.target else node


def _suite_green(src: str, repo: str) -> bool:
    with open(os.path.join(repo, "agent.py"), "w", encoding="utf-8") as f:
        f.write(src)
    try:
        r = subprocess.run([sys.executable] + _MUT_ARGS, capture_output=True, cwd=repo,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=900)
    except subprocess.TimeoutExpired:
        return False              # 卡死也算被抓到:它至少不是"悄无声息地照常干活"
    return r.returncode == 0


def _agent_sweep():
    """agent.py 的每一处 raise 换成 pass,跑全套。返回 [(位置, 摘掉后是否还全绿)]。"""
    src = open(os.path.join(HERE, "agent.py"), encoding="utf-8").read()
    sites = sorted((n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Raise)),
                   key=lambda n: (n.lineno, n.col_offset))
    assert sites, "agent.py 里一处 raise 都没找到 —— 这个扫描失效了"
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        repo = os.path.join(tmp, "repo")
        shutil.copytree(HERE, repo, ignore=_MUT_IGNORE)
        # 控制组:只走一遍 unparse。这一关红过一次 —— 我当时把 benchmarks/ 也排除在
        # 副本外,于是 test_report 找不到基准脚本。**没有这一关,那次会变成"30 条全有 case"。**
        assert _suite_green(ast.unparse(ast.parse(src)), repo), (
            "控制组就红了:副本本身跑不过全套(unparse 改了行为,或者副本缺东西)——"
            "这个扫描的结论全部无效")
        for i, site in enumerate(sites):
            mutant = _DropRaise(i).visit(ast.parse(src))
            ast.fix_missing_locations(mutant)
            out.append((f"agent.py:{site.lineno} {_label(site)}", _suite_green(ast.unparse(mutant), repo)))
    return out


@pytest.mark.skipif(not os.environ.get("TALOS_SWEEP"),
                    reason="全扫要 4 分钟(30 个变异体 × 一遍全套)。CI 跑一格,本地 TALOS_SWEEP=1")
def test_every_guard_in_agent_has_a_case_that_dies_without_it():
    """摘掉 agent.py 里任何一处 `raise`,271 条判据必须有人红。

    **第一次跑就抓到 5 条**(30 条里):两条启动闸(provider 打错字、缺 key)、
    `read_file` 读到目录、以及两处 `raise KeyboardInterrupt`(人在权限框上按 Ctrl-C)。
    最后那两条尤其难看 —— 它们在真实会话里天天走,而摘掉之后没有任何判据会红。

    扫的是 `raise`,不是全部守卫:返回拒绝串的那些(`permission denied: ...`、
    `error: ...`)不在里面。**这是覆盖面的边界,写在这里免得有人把它读成"全查过了"。**"""
    survivors = [name for name, still_green in _agent_sweep() if still_green]
    assert not survivors, (
        f"{len(survivors)} 处守卫摘掉之后全套照绿 —— 它们没有判据,等于没写:\n  "
        + "\n  ".join(survivors))


if __name__ == "__main__":                      # 直接跑给人看:python tests/test_mutation.py
    for name, still_green in _sweep("drawiocheck.py"):
        print(f"  {'NO CASE ' if still_green else 'covered '} {name}")
    print()
    for name, noticed in _knob_sweep().items():
        print(f"  {'覆盖   ' if noticed else '盲区   '} {name:20s} {_BLIND.get(name, '')[:52]}")
