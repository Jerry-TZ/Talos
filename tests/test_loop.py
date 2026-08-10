"""内核循环(mock 掉模型):工具循环 / MAX_STEPS / 压缩 / 打桩 / token 统计 / 重试。"""
import contextlib
import types

import pytest

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

def test_a_dropped_connection_is_retried_not_raised(monkeypatch):
    """真实丢过一次复盘:任务做完、正要复盘时掉网,SDK 抛 `APIConnectionError("Connection error.")`。
    那串里一个既有的瞬时关键词都不含 —— 没有 429、没有 timeout、没有 busy —— 于是一次就抛了出去,
    复盘那一轮整个没了。掉线比"对面忙"更该重试:429 要等对方缓过来,掉线常常下一秒就通。"""
    import agent as A
    monkeypatch.setattr(A.time, "sleep", lambda *a: None)
    monkeypatch.setattr(A, "ui", _ui())
    assert A._chat(_Client([Exception("Connection error."), "OK"])) == "OK"

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

def test_only_a_slow_call_reports_how_long_it_took(monkeypatch):
    """先试过把秒数放进转圈里实时更新,更糟:Windows 传统控制台没法可靠原地重绘,
    秒数宽度一变(9s → 10s → 100s)就刷屏。改成事后报一次 —— 但快调用不值一行,
    一个 43 次调用的任务会多出 43 行噪音。"""
    import agent as A
    seen = []
    monkeypatch.setattr(A, "ui", types.SimpleNamespace(took=seen.append, note=lambda *a: None))
    clock = iter([0.0, 1.0, 0.0, A.SLOW_CALL + 5])          # 快一次,慢一次
    monkeypatch.setattr(A.time, "time", lambda: next(clock))
    A._chat(_Client(["OK"]))
    assert seen == [], "快调用不该报"
    A._chat(_Client(["OK"]))
    assert seen and seen[0] >= A.SLOW_CALL, f"慢调用该报耗时,实际 {seen}"

def test_a_second_timeout_gives_up_instead_of_waiting_another_five_minutes(monkeypatch):
    """超时跟"忙"不是一回事。忙是等一下就好;超时说明这次调用本来就要跑过 CHAT_TIMEOUT,
    重试三遍就是三遍注定失败 —— 默认 300s 下,一次失败要 15 分钟才告诉你。推理模型上
    这是常态,不是意外。上一条测试把超时并进了 transient,这条负责给它封顶。

    "忙" 仍然重试满 3 次:那是免费额度拥塞,等一下真的会好。"""
    import agent as A
    monkeypatch.setattr(A.time, "sleep", lambda _s: None)   # 别让退避拖慢套件
    to = lambda: Exception("Request timed out.")
    with pytest.raises(Exception, match="timed out"):
        A._chat(_Client([to(), to(), "OK"]))
    assert A._chat(_Client([Exception("429 rate limit"),
                            Exception("当前模型用户多"), "OK"])) == "OK"

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


def test_a_tool_that_does_not_exist_never_reaches_the_permission_prompt(monkeypatch):
    """模型编了个 del_probe,权限框照弹。两个毛病:人要为一件不会发生的事做决定;
    而未知名字被兜底归成 "bash",对着这个假名字按 [a] 放行的是整个 bash 类,
    真正的 run_bash 从此不再问。批准的东西必须先存在。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    asked = []
    monkeypatch.setattr(A, "check_permission",
                        lambda st, cls, n, a: (asked.append(n), (True, ""))[1])
    script = [_msg(tool_calls=[_tc("del_probe", '{"path":"x.py"}')]), _msg(content="ok")]
    messages = [{"role": "user", "content": "hi"}]
    A.agent_turn(_Client(script), "m", messages, {"mode": "default", "allow": set()})
    assert asked == [], f"不存在的工具走到了权限门: {asked}"
    said = [m["content"] for m in messages if m.get("role") == "tool"]
    assert any("unknown tool" in str(c) for c in said), said

def test_reflection_looks_at_the_task_that_just_finished(monkeypatch):
    """REPL 的 messages 跨轮累积,而 reflect() 原来正向取第一条 user 消息 —— 于是从第二轮起,
    查重摆到复盘眼前的是**本次会话开头那个任务**的技能表,跟刚做完的事毫无关系,而提示词
    还写着"上面有沾边的就去改"。`/compact` 之后更糟:首条 user 消息变成压缩简报。
    agent_turn 里算 query 用的就是 reversed,这里跟它对齐。"""
    import agent as A
    seen = {}
    monkeypatch.setattr(A, "_known_skills", lambda t: seen.setdefault("task", t) and "")
    monkeypatch.setattr(A, "agent_turn", lambda *a, **k: "done")
    monkeypatch.setattr(A, "_memory_lines", lambda: [])
    monkeypatch.setattr(A, "_tag_new_memory", lambda before: 0)
    messages = [{"role": "user", "content": "把三个 csv 合并成一张表"},
                {"role": "assistant", "content": "好"},
                {"role": "user", "content": "帮我升级 rust 依赖 cargo update"}]
    A.reflect(None, "m", messages, {"mode": "bypass", "allow": set()})
    assert seen["task"] == "帮我升级 rust 依赖 cargo update", \
        f"复盘查的是会话第一个任务,不是刚做完的: {seen['task']!r}"

def test_a_subagent_hitting_the_step_cap_does_not_cancel_the_parents_reflection(monkeypatch):
    """一个 state 里混着三类性质完全不同的东西,而子 agent 原来拿的是父的同一个 dict:

        继承 — mode / allow / view      子轮该按同样的权限跑
        汇总 — tok / trace              一次请求的总账,子轮的消耗算在父头上
        本轮 — capped / last_* / asked  只描述"刚刚这一轮",跨层就是错的

    代价出过两次:第一次是 repeat 计数被子轮清零(已修);第二次是 capped —— 子 agent
    撞 MAX_STEPS 会写 state["capped"]=True,父任务明明成功返回,repl 却因为这个标记
    跳过**整个任务**的复盘。修 repeat 那次只挪了一个变量,没看这一类。

    这条测试同时钉住两头:本轮字段不许漏上去,汇总字段不许因此断掉。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    monkeypatch.setattr(A, "MAX_STEPS", 3)
    real = A.run_tool
    monkeypatch.setattr(A, "run_tool",
                        lambda n, a: ("ok", False) if n == "run_bash" else real(n, a))
    busy = _msg(tool_calls=[_tc("run_bash", '{"command":"echo x"}')])
    script = ([_msg(tool_calls=[_tc("spawn_subagent", '{"task":"子任务"}')])]
              + [busy] * 4                       # 子 agent 一路打转到撞上限
              + [_msg(content="parent done")])   # 父 agent 正常收尾
    st = {"mode": "bypass", "allow": set()}
    out = A.agent_turn(_Client(script), "m", [{"role": "user", "content": "父任务"}], st)
    assert out == "parent done"
    assert not st.get("capped"), "子 agent 撞上限,把父任务的复盘一起取消了"
    assert st["tok"]["calls"] >= 5, "本轮字段隔离了,但 token 汇总也跟着断了"
    assert len(st.get("trace", [])) >= 5, "trace 汇总断了 —— 子 agent 的调用没记进去"

def test_cache_trace_pairs_the_system_hash_with_the_hit_rate(tmp_path, monkeypatch):
    """P3(KV cache)当初被划成「已否决」,依据是命中 92~99% —— 但那是 -p 一次性模式量的,
    中间不复盘。交互式会话里复盘每写一条技能,retrieve() 的常驻块就变,而它在 system
    prompt 里,一变整个前缀作废。要判断这件事,得把"system 变没变"和"命中多少"记在同一行
    —— 而这两个数从来没被同时记下来过。先量,别改。"""
    import json
    import agent as A
    monkeypatch.setattr(A, "CACHE_TRACE", str(tmp_path / "cache.jsonl"))
    blocks = iter(["技能表 v1", "技能表 v1", "技能表 v2"])       # 第三轮复盘写了新技能
    monkeypatch.setattr(A, "retrieve", lambda: next(blocks))
    st = {}
    for cached in (900, 950, 300):
        A._log_cache(st, {"in": 1000, "out": 10, "cached": cached, "steps": 3})
    rows = [json.loads(l) for l in open(tmp_path / "cache.jsonl", encoding="utf-8")]
    assert [r["sys_changed"] for r in rows] == [False, False, True], rows
    assert [r["hit"] for r in rows] == [0.9, 0.95, 0.3]
    A._log_cache(st, {"in": 0, "out": 0, "cached": 0})          # 空轮不记
    assert len(list(open(tmp_path / "cache.jsonl", encoding="utf-8"))) == 3


def test_cache_trace_counts_reads_per_turn(tmp_path, monkeypatch):
    """READ_MAX_LINES=250 而 png2epub.py 有 508 行 —— 读全文至少三次,每次都重发整个已积累
    的上下文。分页上限是为省 token 设的,但在"需要读全文"的任务上可能是净亏:省下的是被读
    的行数,付出的是上下文重发。n=1,所以先量,别改。"""
    import json
    import agent as A
    monkeypatch.setattr(A, "CACHE_TRACE", str(tmp_path / "c.jsonl"))
    monkeypatch.setattr(A, "retrieve", lambda: "块")
    st = {"reads": 4}
    A._log_cache(st, {"in": 100, "out": 1, "cached": 50, "steps": 2})
    row = json.loads(open(tmp_path / "c.jsonl", encoding="utf-8").readline())
    assert row["reads"] == 4, row
    assert "reads" not in st, "计数没清零,下一轮会把这轮的读数算进去"
    A._log_cache(st, {"in": 100, "out": 1, "cached": 50, "steps": 2})
    rows = [json.loads(l) for l in open(tmp_path / "c.jsonl", encoding="utf-8")]
    assert rows[1]["reads"] == 0

def test_a_subagent_still_warns_about_files_the_user_named(monkeypatch):
    """按三分类挑字段时漏了 `asked` —— _named_in_request 靠它判断"这次要删的文件,用户在
    原话里点过名吗"。于是顶层跑 `del important_report.md` 会打出警告,子 agent 跑同一条
    命令一声不吭。用户点名要的东西,不会因为这活派给子 agent 干就不算数。"""
    import agent as A
    parent = {"mode": "default", "allow": set(), "view": "normal", "capped": True,
              "asked": "帮我整理 important_report.md,别删掉它"}
    child = A._child_state(parent)          # 打生产代码,别在测试里重拼一遍那个 dict
    assert "capped" not in child, "本轮字段漏给子轮了"
    args = {"command": "del important_report.md"}
    assert A._named_in_request(parent, args) == ["important_report.md"]
    assert A._named_in_request(child, args) == ["important_report.md"], \
        "子 agent 里丢了这层警告"

def test_paging_through_one_file_gets_cut_off(monkeypatch):
    """_repeat_guard 拦不住翻页:它的判据是「输出一模一样」,而换个 offset 输出就不同,
    判据一次都不成立。实测一轮里同一个脚本被换着 offset 读了几十次,连着两轮撞满
    100 步上限。所以按 (read_file, path) 单独数,换 offset 也算同一次。
    edit_file 故意不数 —— 同一个文件改十遍是正常干活,读十遍不是。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    seen = {}
    args = lambda off: {"path": "v.py", "offset": off, "limit": 40}
    for i in range(A.READ_LIMIT - 1):                       # 前 READ_LIMIT-1 次照常给内容
        assert A._read_guard(seen, "read_file", args(i * 40), f"内容{i}") == f"内容{i}"
    cut = A._read_guard(seen, "read_file", args(999), "内容N")
    assert "别再一段一段翻" in cut and "内容N" not in cut     # 到点了:不给内容,只给出路
    # 改同一个文件不受影响,数的也不是它
    for _ in range(A.READ_LIMIT * 2):
        assert A._read_guard(seen, "edit_file", {"path": "v.py"}, "edited") == "edited"
    assert A._read_guard(seen, "read_file", {"path": "别的.py"}, "另一个") == "另一个"

def test_the_read_guard_is_actually_wired_into_the_loop(ws, monkeypatch):
    """两条守卫的单元测试都直接调 `_read_guard` —— 把 agent_turn 里那一行调用删掉,
    它们照样绿。**接线本身没人测。** 这条从 agent_turn 走一遍真实分发。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    monkeypatch.setattr(A, "run_tool", lambda name, args: ("原始内容", False))
    n = A.READ_LIMIT + 1
    script = [_msg(tool_calls=[_tc("read_file", '{"path":"same.py","offset":%d}' % i, cid="c%d" % i)])
              for i in range(n)] + [_msg(content="done")]
    messages = [{"role": "user", "content": "读它"}]
    A.agent_turn(_Client(script), "m", messages, {"mode": "bypass", "allow": set()})
    tools = [m["content"] for m in messages if m.get("role") == "tool"]
    assert tools[0] == "原始内容" and "别再一段一段翻" in tools[-1]

def test_slicing_a_file_with_run_bash_counts_too(ws, monkeypatch):
    """真实一轮:模型三十几次 run_bash 打印 agent.py 的不同片段,一次 read_file 都没用 ——
    上一版守卫按**工具名**计数,于是一次都没触发。文件工具关在 workspace 里,读上一级的
    源码只能走 run_bash,所以这条路不是它偷偷绕的,是唯一的路。
    **按工具名计数的守卫,绕过它不需要动机,换个工具就行。**"""
    import agent as A, os
    monkeypatch.setattr(A, "ui", _ui())
    p = os.path.join(A.WORKSPACE, "big.py")
    open(p, "w", encoding="utf-8").write("x = 1\n" * 50)
    seen = {}
    for i in range(A.READ_LIMIT - 1):
        cmd = {"command": f'findstr /n "def" {p}'}          # 每次片段不同,输出也不同
        assert A._read_guard(seen, "run_bash", cmd, f"片段{i}") == f"片段{i}"
    cut = A._read_guard(seen, "run_bash", {"command": f"type {p}"}, "片段N")
    assert "别再一段一段翻" in cut and "片段N" not in cut
    # 解释器本身不算被读的文件。每条命令开头都是那个 venv 的 python.exe,不滤掉的话
    # 它会先于任何真实文件撞上限,把正常命令的输出也拦掉 —— 实测就是这么误伤的。
    exe = os.path.join(A.WORKSPACE, "python.exe")
    open(exe, "w").write("")
    seen2 = {}
    for i in range(A.READ_LIMIT * 2):          # 每次读**不同**的文件,只有解释器是重复的
        cmd = {"command": exe + f' -c "print(open(\'f{i}.txt\').read())"'}
        assert A._read_guard(seen2, "run_bash", cmd, "输出") == "输出"
    # grep 的**搜索词**不算文件。`_targets` 第一步是无条件的文件名正则,而它吃得下
    # `os.path` 这种带点的标识符 —— 六个不同文件配同一个搜索词,也能把计数记满。
    seen3 = {}
    for i in range(A.READ_LIMIT * 2):
        assert A._read_guard(seen3, "run_bash",
                             {"command": f'findstr /n "os.path" f{i}.py'}, "命中") == "命中"
    # 改过的文件下次读确实是新内容 —— 写一次就该清零,否则边写边看的循环第 6 轮就断了
    seen4, path = {}, os.path.join(A.WORKSPACE, "calc.py")
    for _ in range(A.READ_LIMIT * 3):
        assert A._read_guard(seen4, "read_file", {"path": path}, "内容") == "内容"
        A._read_guard(seen4, "edit_file", {"path": path}, "edited")
    # 换个拼法不该重置:v.py / ./v.py / 绝对路径 是同一个文件
    seen5, spells = {}, ("v.py", "./v.py", os.path.join(A.WORKSPACE, "v.py"))
    for i in range(A.READ_LIMIT):
        last = A._read_guard(seen5, "read_file", {"path": spells[i % 3]}, "内容")
    assert "别再一段一段翻" in last and len(seen5) == 1   # 三种拼法必须落在同一个键上
    # 上限跟着文件长度走:一本要翻 N 页的文件,读 N 次是翻完一遍,不是打转。
    # 定死 6 次的话,2118 行的 agent.py 在第 6 次就被拦 —— 而翻完一遍要 9 次。
    big = os.path.join(A.WORKSPACE, "long.py")
    open(big, "w", encoding="utf-8").write("x = 1\n" * (A.READ_MAX_LINES * 9))
    assert A._pages(big) == 9
    seen6 = {}
    for _ in range(A.READ_LIMIT * 2):                    # 12 次:小文件早拦了,这个还没到
        assert A._read_guard(seen6, "read_file", {"path": big}, "内容") == "内容"
    for _ in range(A.READ_LIMIT):
        last = A._read_guard(seen6, "read_file", {"path": big}, "内容")
    assert "别再一段一段翻" in last                       # 但翻到两遍还是要拦
    # 跑脚本不算读:同一个文件被执行多少次都不该拦(判官就是这么反复跑的)
    for _ in range(A.READ_LIMIT * 2):
        assert A._read_guard(seen, "run_bash", {"command": f"python {p} arg"}, "跑完了") == "跑完了"
    # 一条命令里同一个文件出现两种拼法,只能算**一次**读。`_targets` 对一条命令既返回短名
    # (`_FILENAME`)又返回整条路径(`_TOKEN`),而计数曾经是在拼法上循环的 —— 一条命令加 2,
    # 第 3 次就被拦下,而消息里写着"本轮已读 6 次"(数字和事实对不上,这是它露出来的马脚)。
    # 用两个**相对**拼法,不碰盘符:上面那个绝对路径的写法在 Windows+py3.10 上侥幸不红
    # (`_TOKEN` 吃不下 `C:`,3.10 又把 `\a\b.py` 当绝对路径解析到当前盘,两个键分属不同盘符),
    # 而 CI 的另外三格全红。判据要在四格上都成立,就不能依赖盘符。
    seen7 = {}
    open(os.path.join(A.WORKSPACE, "twice.py"), "w", encoding="utf-8").write("x = 1\n")
    for _ in range(A.READ_LIMIT - 1):
        assert A._read_guard(seen7, "run_bash", {"command": "type ./twice.py"}, "片段") == "片段"
    assert len(seen7) == 1, f"同一个文件被记成了 {len(seen7)} 个键"
    assert sum(seen7.values()) == A.READ_LIMIT - 1, "一条命令把同一个文件数了不止一次"


def test_a_broken_guard_must_not_take_the_whole_turn_down(ws, monkeypatch):
    """两条守卫跑在 `run_tool` 的 try/except **外面**,所以它们抛什么都直接冲出 agent_turn。
    实测:路径里带 `\x00` 的 read_file —— `os.stat`/`open` 抛的是 `ValueError`,而
    `_read_key` 只 `except OSError`,接不住。与其追每一种可能的异常,不如认下这件事:
    **守卫是省钱的,不是干活的;它自己坏了就别守,不该拦下整轮的活。**"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    monkeypatch.setattr(A, "run_tool", lambda name, args: ("正常结果", False))
    script = [_msg(tool_calls=[_tc("read_file", '{"path":"a\u0000b.py"}', cid="c0")]),
              _msg(content="done")]
    messages = [{"role": "user", "content": "读它"}]
    assert A.agent_turn(_Client(script), "m", messages,
                        {"mode": "bypass", "allow": set()}) == "done"
    assert [m["content"] for m in messages if m.get("role") == "tool"] == ["正常结果"]
    # 兜的是**调用点**,不是某一条守卫的内部 —— 所以两条守卫抛什么类型都得接住。
    # 断言必须走 agent_turn:直接调 `_read_guard` 测不到接线,而接线正是这条修的东西。
    for victim in ("_read_guard", "_repeat_guard"):
        with monkeypatch.context() as m:
            m.setattr(A, "ui", _ui())
            m.setattr(A, "run_tool", lambda name, args: ("正常结果", False))
            m.setattr(A, victim, lambda *a, **k: (_ for _ in ()).throw(RuntimeError("守卫坏了")))
            msgs = [{"role": "user", "content": "读它"}]
            assert A.agent_turn(_Client([_msg(tool_calls=[_tc("read_file", '{"path":"v.py"}')]),
                                         _msg(content="done")]),
                                "m", msgs, {"mode": "bypass", "allow": set()}) == "done", \
                f"{victim} 抛异常时整轮被带走了"
            assert [m2["content"] for m2 in msgs if m2.get("role") == "tool"] == ["正常结果"]


def test_pages_does_not_slurp_the_whole_file(ws, monkeypatch):
    """`read_file` 拒绝超大文件时,建议的正是「用 run_bash 的 more/findstr 截取」—— 那条
    命令回头就落进 `_read_guard` → `_pages`。所以这条路径**专门服务于大文件**,而它原来
    `f.read()` 整个吞下去。一道用来省 token 的闸,不该是全程序里内存峰值最高的地方。"""
    import agent as A, os, tracemalloc
    big = os.path.join(A.WORKSPACE, "huge.log")
    with open(big, "wb") as f:
        for _ in range(40):
            f.write(b"x" * (1 << 20) + b"\n")            # 40MB,超过 READ_MAX_BYTES
    tracemalloc.start()
    pages = A._pages(big)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 4 << 20, f"_pages 峰值 {peak} 字节 —— 整个文件被吞进内存了"
    assert pages >= 1
    # 而且没到上限时根本不该去读文件:前几次调用一次 _pages 都不许发生
    calls = []
    monkeypatch.setattr(A, "_pages", lambda p: calls.append(p) or 1)
    seen = {}
    for _ in range(A.READ_LIMIT - 1):
        A._read_guard(seen, "read_file", {"path": big}, "内容")
    assert calls == [], f"没到上限就扫了 {len(calls)} 遍文件"
