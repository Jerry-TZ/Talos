"""内核循环(mock 掉模型):工具循环 / MAX_STEPS / 压缩 / 打桩 / token 统计 / 重试。"""
import contextlib
import types

def _ui():
    n = lambda *a, **k: None
    return types.SimpleNamespace(thinking=lambda: contextlib.nullcontext(),
                                 show_tool=n, denied=n, think=n, assistant_text=n, note=n, error=n)

def _msg(content=None, tool_calls=None, usage=None):
    m = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=m)], usage=usage)

def _tc(name, args, cid="c1"):
    return types.SimpleNamespace(id=cid, function=types.SimpleNamespace(name=name, arguments=args))

class _Client:
    def __init__(self, script):
        self.script, self.i = script, 0
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._c))
    def _c(self, **k):
        r = self.script[self.i]; self.i += 1
        if isinstance(r, Exception):
            raise r
        return r

def test_tool_loop_and_token_tracking(ws, monkeypatch):
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    usage = types.SimpleNamespace(prompt_tokens=100, completion_tokens=20, prompt_tokens_details=None)
    client = _Client([_msg(tool_calls=[_tc("run_bash", '{"command":"echo hi"}')], usage=usage),
                      _msg(content="done", usage=usage)])
    state = {"mode": "bypass", "allow": set()}
    out = A.agent_turn(client, "m", [{"role": "user", "content": "hi"}], state)
    assert out == "done" and state["last_calls"] == 1
    assert state["tok"]["in"] == 200 and state["last_tok"]["out"] == 40   # 两次调用的 usage 累计

def test_subagent_tokens_roll_up_to_caller(ws, monkeypatch):
    """子agent 共用 state,它烧的 token 必须算进外层这一轮,不能丢。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    usage = types.SimpleNamespace(prompt_tokens=100, completion_tokens=20, prompt_tokens_details=None)
    client = _Client([_msg(tool_calls=[_tc("spawn_subagent", '{"task":"go read it"}')], usage=usage),
                      _msg(content="sub done", usage=usage),        # 子agent 自己这一步
                      _msg(content="done", usage=usage)])
    state = {"mode": "bypass", "allow": set()}
    assert A.agent_turn(client, "m", [{"role": "user", "content": "hi"}], state) == "done"
    assert state["last_tok"]["in"] == 300 and state["last_tok"]["steps"] == 3   # 含子agent 那一次
    assert state["last_calls"] == 1

def _tool_result(messages):
    return next(m["content"] for m in messages if m.get("role") == "tool")

def test_subagent_summary_counts_real_calls_and_leaks_nothing(ws, monkeypatch):
    """摘要由主循环按真实分发计数,且只含工具名 —— 命令内容不能顺着它回到外层上下文。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    client = _Client([_msg(tool_calls=[_tc("spawn_subagent", '{"task":"go"}')]),
                      _msg(tool_calls=[_tc("run_bash", '{"command":"echo SECRET"}', "c2")]),
                      _msg(content="查完了"),                       # 子agent 的结论
                      _msg(content="done")])
    messages = [{"role": "user", "content": "hi"}]
    A.agent_turn(client, "m", messages, {"mode": "bypass", "allow": set()})
    seen = _tool_result(messages)
    assert "run_bash × 1" in seen and "查完了" in seen
    assert "SECRET" not in seen and "echo" not in seen

def test_subagent_that_ran_nothing_cannot_claim_otherwise(ws, monkeypatch):
    """子agent 说自己读了文件,轨迹说它一个工具都没调 —— 外层必须看得见这个矛盾。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    client = _Client([_msg(tool_calls=[_tc("spawn_subagent", '{"task":"go"}')]),
                      _msg(content="我读完了三个文件,没发现问题"),   # 纯编造,没有任何工具调用
                      _msg(content="done")])
    messages = [{"role": "user", "content": "hi"}]
    A.agent_turn(client, "m", messages, {"mode": "bypass", "allow": set()})
    assert "(没有调用任何工具)" in _tool_result(messages)

def test_trace_records_denied_calls_separately(ws, monkeypatch):
    """被权限门拒掉的调用要单独计,不能和执行失败混在一起。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    client = _Client([_msg(tool_calls=[_tc("run_bash", '{"command":"echo hi"}')]),
                      _msg(content="done")])
    state = {"mode": "plan", "allow": set()}                        # plan = 只读,run_bash 直接拒
    A.agent_turn(client, "m", [{"role": "user", "content": "hi"}], state)
    assert state["trace"] == [{"tool": "run_bash", "error": True, "denied": True}]
    assert A._trace_summary(state["trace"]) == "run_bash × 1,被拒 1"

def test_reflection_recalls_on_the_task_not_on_the_reflection_prompt(ws, monkeypatch):
    """recall 按最后一条 user message 检索,而复盘把 REFLECT_PROMPT 追加成了最后一条 ——
    于是每次复盘捞回来的都是「怎么写技能」,一字不差,跟刚做完的任务无关(真实轨迹里
    连着几个不同任务的复盘轮都哈希到同一个值)。复盘恰恰是最该看见本次任务记忆的时候。"""
    import agent as A
    import recall as R
    monkeypatch.setattr(A, "ui", _ui())
    asked = []
    monkeypatch.setattr(R, "recall", lambda q, **kw: asked.append(q) or "")
    client = _Client([_msg(content="没什么值得记的")])
    A.reflect(client, "m", [{"role": "user", "content": "统计 data/ 里每个 csv 的缺失率"}],
              {"mode": "bypass", "allow": set()})
    assert asked == ["统计 data/ 里每个 csv 的缺失率"]

def test_max_steps_cap(ws, monkeypatch):
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    monkeypatch.setattr(A, "MAX_STEPS", 3)
    loop = _msg(tool_calls=[_tc("run_bash", '{"command":"echo x"}')])
    client = _Client([loop] * 10)
    out = A.agent_turn(client, "m", [{"role": "user", "content": "x"}], {"mode": "bypass", "allow": set()})
    assert "上限" in out

def test_maybe_compact(ws, monkeypatch):
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    client = _Client([_msg(content="SUMMARY")])
    big = [{"role": "user", "content": "x" * 20000},
           {"role": "assistant", "content": "y" * 20000},
           {"role": "user", "content": "z"}]
    out = A.maybe_compact(client, "m", big, force=True)
    assert len(out) == 2 and "SUMMARY" in out[0]["content"]
    small = [{"role": "user", "content": "hi"}]
    assert A.maybe_compact(client, "m", small) is small          # 太短 -> 原样返回

def test_seal_keeps_work_after_a_failed_turn():
    """一轮中途挂了(比如 429),进度必须留着让用户「继续」,只补上缺的工具结果。"""
    import agent as A
    msgs = [{"role": "user", "content": "造个爬虫"},
            {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
            {"role": "tool", "tool_call_id": "a", "content": "第一步的结果"}]
    A._seal(msgs)
    assert len(msgs) == 4 and msgs[0]["content"] == "造个爬虫"   # 任务和已有结果都没丢
    assert msgs[3]["tool_call_id"] == "b" and "中断" in msgs[3]["content"]
    A._seal(msgs)
    assert len(msgs) == 4                                        # 已经补齐了就别再补

def test_seal_fixes_every_unsatisfied_call_not_just_the_last():
    """漏掉任何一个,下一次请求就 400,而且永远好不了 —— 会话直接废掉。"""
    import agent as A
    msgs = [{"role": "user", "content": "a"},
            {"role": "assistant", "tool_calls": [{"id": "x1"}, {"id": "x2"}]},
            {"role": "tool", "tool_call_id": "x1", "content": "第一个的结果"},
            {"role": "user", "content": "b"},
            {"role": "assistant", "tool_calls": [{"id": "y1"}]}]
    A._seal(msgs)
    # 每个 assistant 的 tool_calls 后面都必须紧跟齐全的结果
    for i, m in enumerate(msgs):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            got = []
            for later in msgs[i + 1:]:
                if later.get("role") != "tool":
                    break
                got.append(later["tool_call_id"])
            assert [c["id"] for c in m["tool_calls"]] == got, f"第 {i} 条没补齐"
    A._seal(msgs)
    assert len([m for m in msgs if m.get("role") == "tool"]) == 3      # 幂等,不重复补

def test_prune_old_tool_results():
    import agent as A
    msgs = [{"role": "tool", "content": "Z" * 1000}] + [{"role": "user", "content": "x"}] * 10
    A._prune_old_tool_results(msgs)
    assert msgs[0]["content"].startswith("[已省略")
    recent = {"role": "tool", "content": "Y" * 1000}
    m2 = [{"role": "user", "content": "x"}] * 3 + [recent]
    A._prune_old_tool_results(m2)
    assert recent["content"] == "Y" * 1000                       # 最近的不动

def test_chat_retries_on_busy(monkeypatch):
    import agent as A
    monkeypatch.setattr(A.time, "sleep", lambda *a: None)
    monkeypatch.setattr(A, "ui", _ui())
    assert A._chat(_Client(["OK"])) == "OK"
    assert A._chat(_Client([Exception("Error 429 rate limit"), "OK2"])) == "OK2"
    assert A._chat(_Client([Exception("当前模型用户多"), "OK3"])) == "OK3"
    import pytest
    with pytest.raises(Exception):
        A._chat(_Client([Exception("invalid api key 401")]))     # 非瞬时错误 -> 直接抛

def test_a_long_turn_prunes_inside_the_loop(ws, monkeypatch):
    """两道上下文护栏原来只在 REPL 每轮末尾跑,而那次是死在**一轮之内**:六十来次工具调用,
    历史涨过窗口,400 Prompt exceeds max length,整轮白做。MAX_STEPS 守的是空转,守不了
    一轮太长 —— 100 步远在上下文耗尽之后。剪枝必须发生在循环里,这个测试就盯这一点:
    agent_turn 结束时并不剪,所以只要收尾时看到打桩,就说明循环内剪过。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    monkeypatch.setattr(A, "run_tool", lambda name, args: ("P" * 3000, False))
    script = [_msg(tool_calls=[_tc("run_bash", '{"command":"cmd%d"}' % i, cid="c%d" % i)])
              for i in range(12)] + [_msg(content="done")]
    messages = [{"role": "user", "content": "hi"}]
    A.agent_turn(_Client(script), "m", messages, {"mode": "bypass", "allow": set()})
    stubbed = [m for m in messages
               if m.get("role") == "tool" and m["content"].startswith("[已省略")]
    assert stubbed, "循环里没剪枝 —— 一轮长下去照样会撑爆上下文"
    assert messages[-2]["content"] == "P" * 3000        # 最近的原样留着,别把正在用的也剪了

def test_the_loop_actually_feeds_the_repeat_warning_back(ws, monkeypatch):
    """光有 _repeat_guard 不算数 —— 得确认循环真的把它接在了工具结果上。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    monkeypatch.setattr(A, "run_tool", lambda name, args: ("总行数: 642", False))
    script = [_msg(tool_calls=[_tc("run_bash", '{"command":"python verify.py"}', cid="c%d" % i)])
              for i in range(3)] + [_msg(content="done")]
    messages = [{"role": "user", "content": "hi"}]
    A.agent_turn(_Client(script), "m", messages, {"mode": "bypass", "allow": set()})
    tool_msgs = [m["content"] for m in messages if m.get("role") == "tool"]
    assert tool_msgs[0] == "总行数: 642" and tool_msgs[1] == "总行数: 642"
    assert "第 3 次" in tool_msgs[2] and "总行数: 642" in tool_msgs[2]

def test_the_loop_snapshots_before_a_tool_can_overwrite(ws, monkeypatch):
    """光有 archive_workspace() 不算数 —— 得确认循环真的在动手**之前**调它。
    那次十五个脚本原地覆盖,数据没了才是重点;存晚一步等于没存。"""
    import os
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    p = os.path.join(ws, "data.log")
    with open(p, "w", encoding="utf-8") as f:
        f.write("好数据")
    def destroy(name, args):                        # 冒充 `python fix_logs.py`
        with open(p, "w", encoding="utf-8") as f:
            f.write("坏数据")
        return "修复完成", False
    monkeypatch.setattr(A, "run_tool", destroy)
    client = _Client([_msg(tool_calls=[_tc("run_bash", '{"command":"python fix_logs.py"}')]),
                      _msg(content="done")])
    A.agent_turn(client, "m", [{"role": "user", "content": "hi"}],
                 {"mode": "bypass", "allow": set()})
    saved = [open(os.path.join(A.TRASH_DIR, n), encoding="utf-8").read()
             for n in os.listdir(A.TRASH_DIR)]
    assert "好数据" in saved, "覆盖之前没有存档 —— 数据就真没了"

def test_a_subagent_cannot_reset_the_parents_repeat_counter(ws, monkeypatch):
    """spawn_subagent 把调用方**同一个** state 交给嵌套的 agent_turn。打转计数原来挂在
    state 上,于是子agent 一进门就把父轮的计数清了 —— 恰好在这道闸该起作用的死循环里
    把它关掉。计数必须是每次 agent_turn 自己的局部变量。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    real = A.run_tool          # 只假冒 run_bash —— 把 spawn_subagent 也 mock 掉的话,
    monkeypatch.setattr(       # 嵌套那一轮压根不会发生,这个测试就成了摆设
        A, "run_tool",
        lambda name, args: ("总行数: 642", False) if name == "run_bash" else real(name, args))
    same = _tc("run_bash", '{"command":"python verify.py"}')
    script = [_msg(tool_calls=[same]), _msg(tool_calls=[same]),
              _msg(tool_calls=[_tc("spawn_subagent", '{"task":"顺手查点别的"}')]),
              _msg(content="子agent 干完了"),          # 子agent 自己那一步
              _msg(tool_calls=[same]), _msg(content="done")]
    messages = [{"role": "user", "content": "hi"}]
    A.agent_turn(_Client(script), "m", messages, {"mode": "bypass", "allow": set()})
    hits = [m["content"] for m in messages
            if m.get("role") == "tool" and "第 3 次" in str(m.get("content"))]
    assert hits, "子agent 插了一脚,父轮的打转计数就被清零了"

def test_a_timeout_is_retried_like_any_other_busy_signal():
    """SDK 抛的是 "Request timed out.",里面没有 "timeout" 这个词 —— 刚给客户端设完超时
    才发现,超时本身正好落在重试判据之外,等于设了个「一次就放弃」的开关。"""
    import agent as A
    assert A._chat(_Client([Exception("Request timed out."), "OK"])) == "OK"

def test_the_client_bounds_how_long_one_call_can_hang(monkeypatch):
    """没有超时时用的是 SDK 默认 600 秒 + SDK 自己 2 次重试,外面 _chat 又套 3 次 ——
    最坏一个多小时才报错,屏幕上只有一个转圈。而且 SDK 那两次是静默的,「模型繁忙」
    根本不打印。重试只能有一处,而且必须可见。"""
    import agent as A
    import sys
    seen = {}
    fake = types.ModuleType("openai")          # 测试是纯 stdlib 离线的,不能真 import openai
    fake.OpenAI = lambda **kw: seen.update(kw) or object()
    monkeypatch.setitem(sys.modules, "openai", fake)
    monkeypatch.setenv("ZHIPUAI_API_KEY", "k")
    monkeypatch.setattr(A, "PROVIDER", "glm")
    A.make_client()
    assert seen["timeout"] == A.CHAT_TIMEOUT
    assert seen["max_retries"] == 0            # 重试归 _chat 管,别两层相乘

def test_a_resumed_task_still_gets_its_learning_pass():
    """判据原来看的是**最后一轮**的调用数。一个跑了十五次调用的任务撞上连接错误、用「继续」
    接上,收尾那轮只值 1 次调用 —— 于是整个任务一次都没复盘。正好反了:长到会撞上瞬时
    故障的任务,才是最该学的。连着三轮的复盘就是这么丢的。"""
    import agent as A
    st = {"last_calls": 15}
    assert A._due_for_reflection(st, False)          # 主轮:够了
    st["since_reflect"] = 0                          # 复盘跑过,清零(repl 里做的)
    st["last_calls"] = 1                             # 崩了之后「继续」,这轮只有 1 次调用
    assert not A._due_for_reflection(st, False)      # 刚学过,不用再学

    st2 = {"last_calls": 3}                          # 反过来:主轮没到阈值就崩了
    assert not A._due_for_reflection(st2, False)     # 3 —— 还不够
    st2["last_calls"] = 1                            # 「继续」,又一次调用
    assert not A._due_for_reflection(st2, False)     # 4 —— 还差一点
    assert A._due_for_reflection(st2, False)         # 5 —— 攒够了,该学了
    assert st2["since_reflect"] == 5                 # 零散的几轮加起来也算数

def test_a_correction_always_reflects_however_short():
    """用户纠正你,一次调用也得学 —— 那是最贵的信号。"""
    import agent as A
    assert A._due_for_reflection({"last_calls": 1}, True)
