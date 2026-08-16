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
        {"q": "same", "out": {"calls": 3, "bodies": 1, "capped": False}},
        {"q": "same", "picked": [{"key": "s", "score": 0.1, "body": False}]},
        {"q": "same", "out": {"calls": 30, "bodies": 0, "capped": False}},
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
    _write(ws, "recall_trace.jsonl", [
        {"q": "a", "out": {"calls": 4, "bodies": 1, "capped": False}},
        {"q": "b", "out": {"calls": 999, "bodies": 1, "capped": True}},
        {"q": "c", "out": {"calls": 6, "bodies": 0, "capped": False}},
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
        {"q": "a", "out": {"calls": 5, "bodies": 0, "capped": False}},
    ])
    assert "1 轮检索" in MR.build(), "结果行被当成了一次检索"


def test_the_report_survives_no_data_and_bad_lines(ws, monkeypatch):
    """报告是只读观测,**它自己崩掉不许比它要观测的东西还脆**。
    一行脏数据、缺字段、类型不对,都得照常出页面 —— 而不是 median 拿到空列表抛异常。"""
    import memory_report as MR
    monkeypatch.setattr(MR, "D", ws)
    assert "<h1>" in MR.build()                         # 一个文件都没有

    with open(os.path.join(ws, "recall_trace.jsonl"), "w", encoding="utf-8") as f:
        f.write('{"q": "a", "out": {"calls": 5, "bodies": 1, "capped": false}}\n')
        f.write("这不是 JSON\n")
        f.write('{"q": "b", "out": {"calls": "五", "bodies": 1}}\n')      # 类型不对
        f.write('{"q": "c", "out": null}\n')                              # out 不是对象
        f.write('{"q": "d"}\n')                                           # 什么都没有
    html = MR.build()
    assert "注入过技能正文</b> n=1" in html, f"脏行把好行也带走了:{html[-600:]}"
