"""memory_report.py 的判据 —— 它一条都没有过,而下游反馈那套数据只有它一个出口。

「收集了没人看」是「判据没被执行」;**「看的那个人算错了」是同一个形状的下一层**,
而它更难发现:报告能生成、页面好看、数字是错的。这个文件今天刚被加了分组逻辑,
而外部复查刚证明我在这块地方确实会写错(按 `q` 分组那条就是它逮的)。

一律把 `D` 指到临时目录 —— 真实的 `.talos/` 里是明文会话日志,测试不许碰。
"""
import json
import os


def test_no_writable_path_still_points_at_the_real_project(ws):
    """`ws` 这个 fixture 的 docstring 写着「测试绝不碰真实项目文件」,而它的实现是**一张
    手写的重定向清单** —— 枚举永远落后一步,今天就落后了一次:`_log_cache` 直接写
    `agent.CACHE_TRACE`,清单里没有它,一条新测试跑一次就往**真实的**
    `.talos/cache_trace.jsonl` 里落了两行合成数据 —— 而那份文件正是用来判断缓存优化
    有没有效的样本。(已清掉;这条测试是那次的判据。)

    所以别再枚举「记得重定向哪些」,改成断言「一条都没漏」:任何模块级的绝对路径常量,
    只要还指着真实项目目录,就是一条能被测试写脏的路。`HOME` 和 `_SELF` 例外 ——
    前者是源码目录(只读,而且工具就是靠它判断能不能读),后者是解释器路径。"""
    import os
    import agent
    import recall
    import session
    real = os.path.dirname(os.path.abspath(agent.__file__))
    allow = {"HOME", "_SELF"}
    leaks = []
    for mod in (agent, recall, session):
        for name in dir(mod):
            if not name.isupper() or name in allow:
                continue
            v = getattr(mod, name, None)
            if (isinstance(v, str) and os.path.isabs(v)
                    and os.path.normcase(v).startswith(os.path.normcase(real))):
                leaks.append(f"{mod.__name__}.{name}")
    assert not leaks, (
        "这些路径在测试里仍然指着真实项目 —— 谁写它们谁就污染真实数据,"
        f"而且不会有任何地方报错:{leaks}\n把它们加进 tests/conftest.py 的 ws fixture")


def _write(d, name, rows):
    with open(os.path.join(d, name), "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_the_benchmark_measures_the_same_gate_the_agent_runs(monkeypatch):
    """benchmark 的正文闸门必须和 `recall.recall()` 逐字一致,否则它量的不是这个 agent。

    `recall.py` 2026-08-10 (b367126) 把竞争者从 `top`(= `ranked[:k]`)改成了完整排名,
    注释里写清了原因:挂在 `top` 上,「这条技能够不够可信」就取决于一个跟它无关的参数 ——
    展示几条。**那次修复没落进 benchmark。** 于是同一份排名,正式代码 abstain、
    尺子却报「注入了」—— 一把量错的尺子会让任何后续改动的 A/B 结论都不可信。

    这里复现的正是 recall.py 那段注释里写的场景:两条技能 0.60 / 0.59(落差 1.02,
    远低于 `BODY_LEAD`),中间隔着四条事实。第二名被 `k=5` 截断挤掉时
    `len(skill_rank) < 2` 成立,退化成 `BODY_FLOOR` 绝对门槛,0.60 >= 0.35 → 注入。
    断言的是**行为**(注入了几条),不是「代码里写的是 ranked 还是 top」。

    是外部复核对着公开仓库读出来的 —— 我自己写了那段注释,却没去看尺子那一侧。"""
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "benchmarks", "recall"))
    import recall
    import recall_benchmark as B

    nodes = [{"kind": "技能", "text": "A", "body": "A body", "path": "skills/a.md"}]
    nodes += [{"kind": "事实", "text": f"f{i}"} for i in range(4)]
    nodes += [{"kind": "技能", "text": "B", "body": "B body", "path": "skills/b.md"}]
    ranked = [(0.60, 0), (0.58, 1), (0.57, 2), (0.56, 3), (0.55, 4), (0.59, 5)]
    monkeypatch.setattr(recall, "_load_nodes", lambda *a, **k: nodes)
    monkeypatch.setattr(B, "_rank", lambda *a, **k: ranked)

    got = B._predict(recall, "q", "current", k=5)["injected_skills"]
    assert got == [], (
        f"第二名技能被 k=5 挤出展示范围,尺子就放行了正文({got})—— "
        "而 recall.recall() 在同一份排名上会 abstain(0.60 / 0.59 = 1.02 < BODY_LEAD)。"
        "落差判据的全部价值在于它跟「展示几条」无关。")


def test_the_report_groups_by_the_turn_not_by_the_question(ws, monkeypatch):
    """外部复查逮到的那条:上一版用 `q` 去跟检索行对,而 `q` 是**问题**的哈希不是**轮**的。

    同一个问题问两次、或者复盘拿同一个 query 再检索一遍(`reflect` 里 `query=task`,
    这是常态),只要其中任何一轮注过正文,这个问题的**所有**轮都被算进「注入过」组 ——
    「只给了描述」那组直接空掉,而对比的全部意义就在这两组之间。

    现在按结果行自己带的 `bodies` 分组。这条测试就摆一个 `q` 相同、两轮结果相反的样本:
    按问题分组会把 30 次那轮也算进「注入过」,按轮分组才分得开。"""
    import memory_report as MR
    monkeypatch.setattr(MR, "D", ws)
    _write(ws, "recall_trace.jsonl", [
        {"q": "same", "picked": [{"key": "s", "score": 0.6, "body": True}]},
        {"q": "same", "picked": [{"key": "s", "score": 0.1, "body": False}]},
    ])
    _write(ws, "cache_trace.jsonl", [        # 一次顶层请求一行,不再跟检索行混在一个文件里
        {"q": "same", "calls": 3, "bodies": 1, "capped": False},
        {"q": "same", "calls": 30, "bodies": 0, "capped": False},
    ])
    html = MR.build()
    assert "注入过技能正文</b> n=1" in html, f"注入过那组数错了:{html[-900:]}"
    assert "只给了描述</b> n=1" in html, \
        "「只给了描述」那组是空的 —— 又按问题分组了,对比的全部意义就在这两组之间"
    assert "中位数 3.0" in html and "中位数 30.0" in html


def test_a_turn_that_hit_the_step_cap_is_not_counted_as_expensive(ws, monkeypatch):
    """撞了步数上限的轮是「卡住了」,不是「花得多」。把它算进去,任何一次空转
    都会把那一组的中位数拉飞,而它跟捞没捞到技能没关系。"""
    import memory_report as MR
    monkeypatch.setattr(MR, "D", ws)
    _write(ws, "cache_trace.jsonl", [
        {"q": "a", "calls": 4, "bodies": 1, "capped": False},
        {"q": "b", "calls": 999, "bodies": 1, "capped": True},
        {"q": "c", "calls": 6, "bodies": 0, "capped": False},
    ])
    html = MR.build()
    assert "注入过技能正文</b> n=1" in html, "撞上限那轮被算进来了"
    assert "999" not in html.split("<h2>④")[1].split("<h2>")[0], "撞上限那轮的数字进了对比"
    assert "撞了步数上限的 1 轮不计入" in html, "排除了却没说,读的人会以为样本是全部"


def test_result_rows_are_not_counted_as_retrievals(ws, monkeypatch):
    """同一个文件里两种行:检索那一刻写的(带 `picked`)和跑完回填的(带 `out`)。
    混在一起数,页头那句「N 轮检索」凭空翻倍 —— 这条也是外部复查逮到的。"""
    import memory_report as MR
    monkeypatch.setattr(MR, "D", ws)
    _write(ws, "recall_trace.jsonl", [
        {"q": "a", "picked": [{"key": "s", "score": 0.4, "body": False}]},
        {"q": "a", "out": {"calls": 5, "bodies": 0}},    # 历史遗留的结果行,现在不再产生
    ])
    assert "1 轮检索" in MR.build(), "老文件里遗留的结果行被当成了一次检索"


def test_the_report_splits_cross_turn_from_within_turn_cache(ws, monkeypatch):
    """一轮一个命中率答不了「谁在漏」:第 1 次调用吃的是**跨轮**前缀,第 2..N 次吃的是
    **轮内**前缀,两者性质完全不同 —— 而 `_prune_old_tool_results` 每步改写旧工具输出,
    只会伤后者。混着记就永远查不出它赔了多少。

    这条钉两件事:两组必须分开显示,而且**只有一次调用的那些轮不许污染「轮内」那一组**
    (它们根本没有轮内可言,拿 0 或 None 混进去会把中位数拉垮,而那正是要看的那个数)。"""
    import memory_report as MR
    monkeypatch.setattr(MR, "D", ws)
    _write(ws, "cache_trace.jsonl", [
        {"q": "a", "calls": 2, "hit_first": 0.30, "hit_rest": 0.99},
        {"q": "b", "calls": 2, "hit_first": 0.40, "hit_rest": 0.97},
        {"q": "c", "calls": 0, "hit_first": 0.35, "hit_rest": None},   # 单次调用,没有轮内
    ])
    html = MR.build()
    assert "第 1 次调用(跨轮前缀)</b> n=3" in html, f"跨轮那组数错了:{html[-800:]}"
    assert "第 2..N 次(轮内前缀)</b> n=2" in html, \
        "单次调用的轮被算进了「轮内」—— 它没有轮内,会把要看的那个数拉垮"
    assert "中位数 98%" in html, "轮内中位数算错了"


def test_the_report_survives_no_data_and_bad_lines(ws, monkeypatch):
    """报告是只读观测,**它自己崩掉不许比它要观测的东西还脆**。
    一行脏数据、缺字段、类型不对,都得照常出页面 —— 而不是 median 拿到空列表抛异常。"""
    import memory_report as MR
    monkeypatch.setattr(MR, "D", ws)
    assert "<h1>" in MR.build()                         # 一个文件都没有

    with open(os.path.join(ws, "cache_trace.jsonl"), "w", encoding="utf-8") as f:
        f.write('{"q": "a", "calls": 5, "bodies": 1, "capped": false}\n')
        f.write("这不是 JSON\n")
        f.write('{"q": "b", "calls": "五", "bodies": 1}\n')               # 类型不对
        f.write('{"q": "c"}\n')                                          # 什么都没有
    html = MR.build()
    assert "注入过技能正文</b> n=1" in html, f"脏行把好行也带走了:{html[-600:]}"


def test_the_rot_checker_cannot_be_fooled_by_a_nearby_unrelated_name():
    """`memory_rot.py` 判「这条行号今天还对吗」,候选名字**只能取紧贴括号前面那一小段**。

    第一版拿整行的标识符去比。而它要查的那两条记忆各提了十几个名字
    (`check_permission` / `_policy` / `bypass` / `acceptEdits` / `run_bash` / …),
    在一个 3100 行的文件里,**任何一个数字附近总能找到其中之一** —— 于是两条
    确实已经失效 396~538 行的声明,被它判成了「成立」。手查的结果对不上才发现。

    **一条没有区分力的判据,和没有判据是一回事,而它看起来像有。** 这是这个项目
    第三次栽在同一个形状上(第三十九、四十二节:代码没错,关于数据的那句话错了)。

    判据构造成最容易骗过它的样子:一条**声明错行号**的记忆,里面塞满真实存在的名字,
    其中一个恰好就在被声明的那一行附近。宽进的那一版会说「成立」。"""
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "benchmarks", "selflearn"))
    import memory_rot as M

    lines = M._src("agent.py")
    assert lines, "agent.py 读不到,这条判据没法成立"
    # 找一个真实存在的调用点,把它的行号安到**另一个**名字头上 —— 经典的「腐烂」形状:
    # 名字是真的、行号是真的,只是它们不再是同一回事。
    real = next(i for i, ln in enumerate(lines, 1) if "_prune_old_tool_results(" in ln)
    fake = ("agent.py 权限判定:check_permission(约%d行)—— 附近还有 _prune_old_tool_results、"
            "maybe_compact、run_bash、spawn_subagent、_policy 一起工作" % real)
    got = M.check_line_claims(fake)
    assert got, "没解析出行号声明"
    n, name, fname, actual, ok = got[0]
    assert name == "check_permission", f"取错了名字:{name}(该取紧贴括号前面那个)"
    assert not ok, (f"把一条错的行号判成了成立 —— 它拿附近**别的**名字交了差。"
                    f"声明 {n},而 check_permission 实际在 {actual}")

def test_an_unattended_run_is_not_a_user_query(tmp_path):
    """跑批会话的"用户消息"是**我自己写的任务串**,不能进冻结验证集的候选池。

    2026-09-04 量出来的:cutoff 之后 9 条"合格"查询,**9 条全是这个** ——
    五个数据解析陷阱加三次几乎一样的 hello.txt,真实用户查询 0 条,而汇总里写着 9。

    这个验证集存在的全部理由,是拿**未参与调参的真实查询**去裁决检索该不该留。
    拿我自己写的任务去填,等于把「测试数据全是 agent 自己造的」——这份记录里最大的
    那处系统性偏差 —— 烤进那把唯一的尺子里。而且它**看起来在推进**:
    燃尽表上的数字每跑一次批就涨。这条判据就是拦那个「看起来在推进」的。"""
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "benchmarks", "recall"))
    import recall_benchmark as B
    from pathlib import Path

    assert B._is_batch(Path("20260903-084801__batch-在工作区建一个-hello-txt.jsonl"))
    assert not B._is_batch(Path("20260903-084801__在工作区建一个-hello-txt.jsonl"))
    # 前缀认的是 slug 那一段,不是整个文件名 —— 时间戳里不会有,但别让它在别处误命中
    assert not B._is_batch(Path("20260903-084801__batching-作业-怎么写.jsonl"))
    # 常数跟 session.py 共用,不许两边各写各的(同一条规则两处实现,这仓库记过三次)
    import session
    assert B._BATCH_PREFIX == session.BATCH
