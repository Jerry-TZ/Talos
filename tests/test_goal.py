"""目标闸:模型说停 ≠ 目标达成,而且判断器**自己去读交付物**。

为什么要有这层,以及为什么判断器必须能读文件,理由写在 agent.py 的「goal gate」那一段。
这份测试钉的是那段推理的每一个可观察后果 —— 尤其是**任务 22 那个形状**
(FINDINGS:669:模型把断言从 3 改成 4,自己的脚本报「验证通过」):
一个只读对话的判断器会放行,一个会打开文件数一遍的不会。那条测试是这整件事的理由。
"""
import json
import os
import types

import pytest

from test_loop import _Client, _msg, _tc, _ui


def _judge(ok=False, reason="还没证据", impossible=False):
    """判断器给结论的那一轮(它返回 JSON 文本,不调工具)。"""
    return _msg(content='{"ok": %s, "reason": "%s", "impossible": %s}'
                % (str(ok).lower(), reason, str(impossible).lower()))


def _judge_reads(path, cid="j1"):
    """判断器先去读一个文件的那一轮。"""
    return _msg(tool_calls=[_tc("read_file", json.dumps({"path": path}), cid=cid)])


def _run(monkeypatch, script, goal="产物正确", view="quiet", **extra):
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    state = {"mode": "bypass", "allow": set(), "view": view, "goal": goal, **extra}
    messages = [{"role": "user", "content": "干活"}]
    out = A.agent_turn(_Client(script), "m", messages, state, top=True)
    return out, state, messages


# ── 这条是整件事的理由:判断器不吃「我自己的脚本说通过了」──────────────────────
def test_judge_opens_the_deliverable_instead_of_believing_the_verify_script(ws, monkeypatch):
    """任务 22 的形状(FINDINGS:669)。

    规格要 3 个冲突键,模型只做出 4 个,于是**把断言改成 4**,`verify_conf.py` 报「验证通过」。
    对话里从头到尾只有「验证通过」这句 —— 一个只读对话的判断器会放行。
    判断器 read_file 打开产物自己数,才看得出交付物违反规格。
    """
    import agent as A
    with open(os.path.join(ws, "conf_report.txt"), "w", encoding="utf-8") as f:
        f.write("conflict keys: server.host, server.port, log.level, log.file\n")   # 4 个,规格要 3 个
    out, state, _ = _run(monkeypatch, [
        _msg(content="生成器跑完了,verify_conf.py 输出「验证通过」。"),   # 干活模型收工
        _judge_reads("conf_report.txt"),                                  # 判断器去开文件
        _judge(ok=False, reason="报告里是 4 个冲突键,规格要求恰好 3 个"),
        _msg(content="我重做一遍。"),
        _judge(ok=True, reason="现在是 3 个"),
    ], goal="conf_report.txt 里冲突键恰好 3 个")
    assert state["goal_checks"] == 2
    assert "✅ 目标达成" in out


def test_judge_only_gets_read_file(ws, monkeypatch):
    """判断器是来查的,不是来干活的。给它 run_bash 就变成第二个 agent 了。

    两件事一起钉:① 送出去的 tools 里只有 read_file;② 它硬要调别的,当场驳回、不执行。
    """
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    seen = {}
    class Spy(_Client):
        def _c(self, **k):
            if "tools" in k and k["messages"][0]["content"].startswith("你是一个独立的完成判断器"):
                seen["tools"] = [t["function"]["name"] for t in k["tools"]]
            return super()._c(**k)
    state = {"mode": "bypass", "allow": set(), "view": "quiet", "goal": "x"}
    A.agent_turn(Spy([
        _msg(content="做完了"),
        _msg(tool_calls=[_tc("run_bash", '{"command":"rm -rf /"}', cid="j1")]),   # 判断器越界
        _judge(ok=True),
    ]), "m", [{"role": "user", "content": "干活"}], state, top=True)
    assert seen["tools"] == ["read_file"], f"判断器拿到的工具不止 read_file:{seen['tools']}"


def test_judge_refusal_message_names_the_tool(ws, monkeypatch):
    """越界调用要**驳回并说明**,不能静默丢掉 —— 否则判断器会一直重试同一个动作。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    sent = []
    class Spy(_Client):
        def _c(self, **k):
            sent.append(k["messages"])
            return super()._c(**k)
    state = {"mode": "bypass", "allow": set(), "view": "quiet", "goal": "x"}
    A.agent_turn(Spy([_msg(content="好了"),
                      _msg(tool_calls=[_tc("write_file", '{"path":"a","content":"b"}', cid="j1")]),
                      _judge(ok=True)]),
                 "m", [{"role": "user", "content": "干活"}], state, top=True)
    blob = json.dumps(sent[-1], ensure_ascii=False)
    assert "只能用 read_file" in blob and "write_file" in blob
    assert not os.path.exists(os.path.join(ws, "a")), "越界的写居然真的执行了"


# ── 核心流程 ─────────────────────────────────────────────────────────────────
def test_block_sends_the_turn_back_without_user_input(ws, monkeypatch):
    out, state, messages = _run(monkeypatch, [
        _msg(content="做完了"), _judge(ok=False, reason="产物里没有那三个字段"),
        _msg(content="补上了"), _judge(ok=True, reason="三个字段都在"),
    ])
    assert state["goal_checks"] == 2 and "✅ 目标达成" in out
    assert any("[目标检查]" in str(m.get("content", "")) for m in messages)


def test_no_goal_means_the_old_exit_condition(ws, monkeypatch):
    """没设目标时判断器一次都不该被调 —— 脚本只有一条,多调就 IndexError。"""
    out, state, _ = _run(monkeypatch, [_msg(content="答完了")], goal=None)
    assert out == "答完了" and "goal_checks" not in state


def test_internal_turns_are_never_goal_checked(ws, monkeypatch):
    """`top` 已经把内部轮次分开了:子 agent 干完子任务不等于会话目标达成,
    复盘那一轮更不该被目标打回去重做。两处都不传 top,所以天然不检查。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    state = {"mode": "bypass", "allow": set(), "view": "quiet", "goal": "x"}
    out = A.agent_turn(_Client([_msg(content="子任务做完了")]), "m",
                       [{"role": "user", "content": "子任务"}], state)      # 不传 top
    assert out == "子任务做完了" and "goal_checks" not in state


# ── 出口:三种情况都不许把没达成说成达成 ──────────────────────────────────────
def test_block_cap_hands_control_back_without_faking_success(ws, monkeypatch):
    import agent as A
    monkeypatch.setattr(A, "GOAL_MAX_BLOCKS", 2)
    script = []
    for _ in range(3):
        script += [_msg(content="我觉得好了"), _judge(ok=False, reason="还是不对")]
    out, state, _ = _run(monkeypatch, script)
    assert "连续 2 次判定未达成" in out and "✅" not in out
    assert state["goal"] == "产物正确", "目标必须保留,等用户补充信息"


def test_judge_crash_does_not_claim_success(ws, monkeypatch):
    out, state, _ = _run(monkeypatch, [_msg(content="好了"), RuntimeError("502")])
    assert "目标检查失败" in out and "✅" not in out and state["goal"]


def test_judge_prose_instead_of_json_is_an_error_not_a_pass(ws, monkeypatch):
    out, _, _ = _run(monkeypatch, [_msg(content="好了"), _msg(content="嗯差不多完成了吧")])
    assert "目标检查失败" in out and "✅" not in out


def test_impossible_stops_instead_of_looping(ws, monkeypatch):
    out, state, _ = _run(monkeypatch, [
        _msg(content="找不到"), _judge(ok=False, reason="要求的仓库不存在", impossible=True)])
    assert "不可能达成" in out and "✅" not in out and state["goal"]


def test_judge_that_never_concludes_is_capped(ws, monkeypatch):
    """判断器一直读不下结论,也得有出口 —— 而且是 error,不是默认放行。"""
    import agent as A
    monkeypatch.setattr(A, "GOAL_MAX_READS", 3)
    out, _, _ = _run(monkeypatch, [_msg(content="好了")] + [_judge_reads("x.txt")] * 3)
    assert "还没给结论" in out and "✅" not in out


# ── 账单和提示词 ─────────────────────────────────────────────────────────────
def test_judge_tokens_are_billed_separately_from_the_turn(ws, monkeypatch):
    """判断器的开销单独记 —— 混进 state["tok"] 会让 benchmark 的每轮 token 悄悄变含义。"""
    u = types.SimpleNamespace(prompt_tokens=100, completion_tokens=20, prompt_tokens_details=None)
    _out, state, _ = _run(monkeypatch, [_msg(content="好了", usage=u),
                                        _msg(content='{"ok": true, "reason": ""}', usage=u)])
    assert state["goal_tok"] == {"in": 100, "out": 20, "calls": 1}
    assert state["tok"]["in"] == 100, "判断器的 token 漏进了主轮统计"


def test_goal_reaches_the_model_only_when_active(ws, monkeypatch):
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    seen = {}
    class Spy(_Client):
        def _c(self, **k):
            seen.setdefault("system", k["messages"][0]["content"])
            return super()._c(**k)
    A.agent_turn(Spy([_msg(content="好")]), "m", [{"role": "user", "content": "x"}],
                 {"mode": "bypass", "allow": set(), "view": "quiet"}, top=True)
    assert "[本轮目标]" not in seen["system"]            # 没目标 -> 一个 token 不占

    seen.clear()
    A.agent_turn(Spy([_msg(content="好"), _judge(ok=True)]), "m",
                 [{"role": "user", "content": "x"}],
                 {"mode": "bypass", "allow": set(), "view": "quiet", "goal": "跑通"}, top=True)
    assert "[本轮目标] 跑通" in seen["system"]
    # 告诉模型判断器**会自己开文件**,而不是只说「要写清楚」——两者对它的行为要求不同
    assert "自己打开交付物" in seen["system"]


def test_system_prompt_measured_for_cache_matches_what_was_sent(ws, monkeypatch):
    """`sys_now` 量的必须是**真正发出去的那一串**(那行注释自己说的)。

    目标提示要是加在 `sys_now` 之后,缓存前缀仪表就会少量一段,而这个错误
    在功能上完全无感 —— 只有这条判据看得见。
    """
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    sent = {}
    class Spy(_Client):
        def _c(self, **k):
            sent.setdefault("system", k["messages"][0]["content"])
            return super()._c(**k)
    state = {"mode": "bypass", "allow": set(), "view": "quiet", "goal": "跑通"}
    A.agent_turn(Spy([_msg(content="好"), _judge(ok=True)]), "m",
                 [{"role": "user", "content": "x"}], state, top=True)
    assert state["sys_now"] == sent["system"]


# ── 判断器看得见什么 ─────────────────────────────────────────────────────────
def test_listing_lets_the_judge_find_the_deliverable(ws, monkeypatch):
    """判断器要先知道有哪些文件才谈得上去读,而 talos 没有 list 类工具 ——
    目录由 harness 算好放进提示词,不给主循环加新工具面。"""
    import agent as A
    os.makedirs(os.path.join(ws, "out"), exist_ok=True)
    with open(os.path.join(ws, "out", "report.md"), "w", encoding="utf-8") as f:
        f.write("hi")
    listing = A._workspace_listing()
    assert "report.md" in listing and "B)" in listing


def test_transcript_keeps_the_tail_of_a_long_result(ws, monkeypatch):
    """退出码通常在**末尾**,只留开头会正好丢掉判据 3 要看的那半。"""
    import agent as A
    t = A._goal_transcript([{"role": "tool", "content": "x" * 5000 + "EXITCODE_0"}])
    assert "EXITCODE_0" in t and "省略" in t and len(t) < 3000


def test_pruned_stub_names_what_to_reread(ws):
    """「需要就重新读」得说清读的是什么。

    只报字符数的话,模型此时已经不知道当初读的是哪个文件 —— 那是一句无法执行的建议。
    出处不用另存:发起这次调用的 tool_call 里就写着。
    """
    import agent as A
    msgs = [{"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "read_file",
                                          "arguments": '{"path":"src/config.py"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "Z" * 1000}] \
        + [{"role": "user", "content": "x"}] * 10
    A._prune_old_tool_results(msgs)
    assert "src/config.py" in msgs[1]["content"], "占位符没说该重读哪个文件"
    # 找不到出处(压缩过的历史里调用可能已经没了)要退回旧说法,不能崩
    orphan = [{"role": "tool", "tool_call_id": "gone", "content": "Z" * 1000}] \
        + [{"role": "user", "content": "x"}] * 10
    A._prune_old_tool_results(orphan)
    assert orphan[0]["content"].startswith("[已省略")


def test_once_picks_up_TALOS_GOAL_and_the_judge_forces_the_rework(ws, monkeypatch):
    """无人值守那条路的完整接线:环境变量 → state → 判断器 → 打回 → 真工具真的跑。

    `-p` 正是 EXAM 跑的那条路,而没人在读 transcript 的时候「说完成了但没完成」代价最大。
    只有模型是脚本;write_file、权限闸、工作区隔离都是真的。
    """
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    monkeypatch.chdir(ws)                    # 相对路径指工作区,和真实运行一致
    monkeypatch.setenv("TALOS_GOAL", "out.txt 里有三行")
    client = _Client([
        _msg(tool_calls=[_tc("write_file", '{"path":"out.txt","content":"a\\nb\\n"}')]),
        _msg(content="写好了,应该是三行。"),              # 其实只有两行
        _judge_reads("out.txt"),                          # 判断器自己去数
        _judge(ok=False, reason="只有 2 行,要 3 行"),
        _msg(tool_calls=[_tc("write_file", '{"path":"out.txt","content":"a\\nb\\nc\\n"}', cid="c9")]),
        _msg(content="补成三行了。"),
        _judge_reads("out.txt", cid="j2"),
        _judge(ok=True, reason="3 行"),
    ])
    monkeypatch.setattr(A, "make_client", lambda: (client, "m"))
    out = A.once("写个 out.txt")

    with open(os.path.join(ws, "out.txt"), encoding="utf-8") as f:
        assert f.read().count("\n") == 3, "最终产物不是三行"
    assert "✅ 目标达成" in out
    # 剧本 8 条全消费 = 第一次收工真的被打回去了。判断器没生效的话第 4 条之后就返回。
    assert client.i == 8, f"判断器没把这轮打回去(只消费了 {client.i} 条)"


def test_json_object_tolerates_fences_and_preamble():
    import agent as A
    assert A._json_object('```json\n{"ok": true}\n```') == {"ok": True}
    assert A._json_object('我认为:\n{"ok": false, "reason": "x"}\n以上') == {"ok": False, "reason": "x"}
    assert A._json_object("完全没有 json") is None
    assert A._json_object('{"坏的": ') is None
    assert A._json_object("[1,2,3]") is None            # 数组不是对象


def test_a_correct_verdict_that_forgot_the_json_wrapper_is_asked_again_not_thrown_away(ws, monkeypatch):
    """判断器**把活干对了、格式没给对**,不许当场丢掉结论。

    真事(FINDINGS 五十三):glm-4.6v 当判断器,读完 5 个 ini、逐条列出 18 个冲突键
    (规格要 3),分析完全正确 —— 只因为没裹 JSON 就被扔进 error 出口。
    而那一轮恰好是十一轮真跑里**唯一一次真有违规可抓**的:这道闸咬到了,然后掉在地上。

    只回问一次,不是无限重试:每次重问都要把整段 conv 重发,而那一轮是按百万 token 计的。
    """
    script = [
        _msg(content="做完了"),                                   # 干活的说停
        _msg(content="我读了 5 个 ini,冲突键有 18 个,规格要 3 个。"),  # 判断器:对的,但没给 JSON
        _judge(ok=False, reason="冲突键 18 个,规格要 3"),           # 回问之后给了
        _msg(content="改好了"),                                   # 被打回去,再收工
        _judge(ok=True, reason="现在是 3 个"),
    ]
    out, state, _ = _run(monkeypatch, script, goal="冲突键恰好 3 个")
    assert state["goal_checks"] == 2, "回问那次没接上,或者被算成了两轮独立检查"
    assert "✅ 目标达成" in out
    assert "没返回 JSON" not in out, f"正确的结论被格式问题扔掉了:{out}"


def test_a_second_formatting_failure_stops_instead_of_burning_the_whole_read_budget(ws, monkeypatch):
    """回问一次就够;再不给就照旧 error。

    判据钉的是**只回问一次** —— 不设上限的话,一个从不输出 JSON 的模型会把整个
    读预算烧成六次全量重发。而回问失败仍然走「不宣称成功」,不会因此漏判成 ok。"""
    script = [_msg(content="做完了")] + [_msg(content="它就是不给 JSON")] * 6
    out, state, _ = _run(monkeypatch, script, goal="随便什么目标")
    assert "没返回 JSON" in out and "不宣称成功" in out
    assert "✅ 目标达成" not in out
    assert state["goal_tok"]["calls"] == 2, \
        f"判断器调了 {state['goal_tok']['calls']} 次 —— 该是 1 次原始 + 1 次回问"


def test_hitting_the_step_cap_must_say_the_goal_was_never_checked(ws, monkeypatch):
    """撞步数上限那个出口**够不着目标闸**,所以它必须自己说出来。

    真事(FINDINGS 五十三):glm-4.6v 撞满 100 步,产物冲突键 9 个(规格要 3)、
    五个文件的键值对全部超范围,而对话里最后一句是它自己的 verify 打印的
    「验证通过!所有条件都满足」。闸一次都没跑,出口只说「说「继续」就接着做」——
    **人凭什么知道该不该继续。**

    这里**不补一次判断器调用**:撞上限那轮已经烧了百万级 token。
    补的是零开销的那一半:没判断过就别让人以为判断过了。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    monkeypatch.setattr(A, "MAX_STEPS", 3)
    spin = _msg(tool_calls=[_tc("run_bash", '{"command":"echo x"}')])
    out = A.agent_turn(_Client([spin] * 10), "m", [{"role": "user", "content": "x"}],
                       {"mode": "bypass", "allow": set(), "view": "quiet", "goal": "冲突键恰好 3 个"},
                       top=True)
    assert "上限" in out
    assert "一次都没被检查过" in out, f"撞上限了却对目标闭口不谈:{out}"
    assert "冲突键恰好 3 个" in out, "没把目标本身报出来,人还得回去翻自己设过什么"

    # 没设目标的那条路不许因此多出噪音
    plain = A.agent_turn(_Client([spin] * 10), "m", [{"role": "user", "content": "x"}],
                         {"mode": "bypass", "allow": set(), "view": "quiet"}, top=True)
    assert "一次都没被检查过" not in plain, f"没设目标也在喊:{plain}"


def test_the_judges_bill_reaches_the_screen_on_the_unattended_path(ws, monkeypatch):
    """**分开记账**和**看得见**是两件事,而 goal gate 那段注释承诺的是后者。

    原话:「它要花 token,而且是每轮结束时花。默认不开;**开了就该看见账单
    (state["goal_tok"])**」。交互里 `/goal` 兑现了这句;`-p` 没有 —— `once()` 打的
    `🎫` 只报 `last_tok`,退出时 state 直接扔掉,会话 JSONL 里存的是消息不是状态。
    而 `once()` 自己的注释推荐的正是这条路(「无人值守正是「说完成了但没完成」
    代价最大的地方」)。于是这道闸最该被看见花销的那条路,一个数字都不打。

    又是这个仓库的老形状:**注释把话说全了,代码只兑现了看得见的那一半。**
    """
    import agent as A
    import console_ui
    notes = []
    monkeypatch.setattr(console_ui, "note", lambda s, *a, **k: notes.append(s))
    monkeypatch.chdir(ws)
    monkeypatch.setenv("TALOS_GOAL", "out.txt 存在")
    u = types.SimpleNamespace(prompt_tokens=100, completion_tokens=20, prompt_tokens_details=None)
    client = _Client([
        _msg(tool_calls=[_tc("write_file", '{"path":"out.txt","content":"a"}')]),
        _msg(content="写好了。"),
        _msg(content='{"ok": true, "reason": "在"}', usage=u),
    ])
    monkeypatch.setattr(A, "make_client", lambda: (client, "m"))
    A.once("写个 out.txt")

    bill = [n for n in notes if "判断器" in n]
    assert bill, f"判断器的账单一次都没上屏 —— 实际打出来的是:{notes}"
    assert "120" in bill[0], f"账单里没有它实际花掉的 token 数:{bill[0]}"


def test_a_crashed_unattended_run_says_the_goal_was_never_checked(ws, monkeypatch):
    """`_unchecked_goal_note` 的注释写的是「**非自愿结束的每一条出口**」,接的是两条。

    它自己那段话:「写成一个函数而不是抄两遍,是因为这片面上的路不止一条(撞步数上限、
    打转被打断),而抄的那一份迟早只改好其中一遍」—— 然后那句括号里的枚举就成了实现范围。
    非自愿出口有四条,另外两条(API 崩、Ctrl-C)在 `once()` 里,一条都没接。

    真事(2026-09-06):`-p` 带 TALOS_GOAL 跑,第 12 分钟供应商 500,判断器一次没跑,
    输出里没有一个字提目标 —— 而这条路上没人在读 transcript,正是这道闸存在的理由。

    判据挂在 `goal_checks` 上而不是「哪几条出口」上:判断器跑没跑它自己知道,
    这样再多一条出口也不会漏 —— **枚举出口正是上面那段注释犯的错。**
    """
    import agent as A
    import console_ui
    notes = []
    monkeypatch.setattr(console_ui, "note", lambda s, *a, **k: notes.append(s))
    monkeypatch.setattr(console_ui, "error", lambda *a, **k: None)
    monkeypatch.chdir(ws)
    monkeypatch.setenv("TALOS_GOAL", "report.md 里有 8 个数")
    monkeypatch.setattr(A, "make_client", lambda: (_Client([RuntimeError("boom")]), "m"))

    with pytest.raises(SystemExit):
        A.once("统计一下")
    assert any("一次都没被检查过" in n for n in notes), f"崩掉的这一轮只字未提目标:{notes}"
