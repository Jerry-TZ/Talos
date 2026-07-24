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
