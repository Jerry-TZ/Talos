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
