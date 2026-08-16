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
    assert "SUMMARY" in out[0]["content"]
    small = [{"role": "user", "content": "hi"}]
    assert A.maybe_compact(client, "m", small) is small          # 太短 -> 原样返回


def test_only_the_top_level_turn_writes_back_what_it_cost(ws, monkeypatch):
    """一次用户请求会跑好几遍 `agent_turn`:复盘用**同一个 query** 再跑一遍
    (`reflect` 里 `query=task`),每个子 agent 也各跑一遍。都记的话,一次请求写出好几条
    结果行,而复盘和子 agent 那几条普遍很短 —— 拿它们当独立样本,中位数直接被拉垮,
    **而且看不出错**。所以只有顶层记。

    第一版这条测试写的是 `assert outs[-1]["capped"] is False` —— **结构上不可能失败**:
    每次都传全新的 state,`capped` 的唯一写入点在它自己 return 的前一行。那句断言无论
    生产代码怎么写都是 False,而它恰恰是本该抓住「capped 跨轮泄漏」的那一句。
    第三十二节刚写完六种形状的第三种,这里就又犯一次。现在改成**比较两轮**:
    正常那轮和撞上限那轮必须分得开,把哪一边写死都会红。"""
    import json
    import os
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    monkeypatch.setattr(A, "run_tool", lambda name, args: ("ok", False))

    def _outs():
        """一次顶层请求一行,写在 CACHE_TRACE 里 —— 上一版分在两个文件、两个写入者、
        同一个粒度,谁也 join 不到谁。"""
        if not os.path.exists(A.CACHE_TRACE):
            return []
        with open(A.CACHE_TRACE, encoding="utf-8") as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    # 没有 usage 就没有 token,`_log_turn` 会当成空轮直接跳过 —— 那是对的,
    # 但这条测试要观测的正是那一行,所以得让假客户端报出用量。
    usage = types.SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                                  prompt_tokens_details=None)

    # ① 顶层正常收尾:一次工具调用之后给答案
    script = [_msg(tool_calls=[_tc("read_file", '{"path": "a.py"}')], usage=usage),
              _msg(content="done", usage=usage)]
    A.agent_turn(_Client(script), "m", [{"role": "user", "content": "看看 a.py"}],
                 {"mode": "bypass", "allow": set()}, top=True)
    assert _outs(), "顶层跑完了,轨迹里一条结果都没有 —— 回填那行根本没被调用"
    assert _outs()[-1]["calls"] == 1, f"数字对不上:{_outs()[-1]}"

    # ② 非顶层(复盘 / 子 agent 走的就是这条):一个字都不许写
    before = len(_outs())
    A.agent_turn(_Client([_msg(content="done", usage=usage)]), "m",
                 [{"role": "user", "content": "看看 a.py"}], {"mode": "bypass", "allow": set()})
    assert len(_outs()) == before, "非顶层也记了 —— 一次请求会被算成好几个样本"

    # ③ 撞上限要标出来,而且跟正常那轮**分得开**
    monkeypatch.setattr(A, "MAX_STEPS", 2)
    spin = [_msg(tool_calls=[_tc("read_file", '{"path": "a.py"}', f"c{i}")], usage=usage)
            for i in range(5)]
    A.agent_turn(_Client(spin), "m", [{"role": "user", "content": "一直转"}],
                 {"mode": "bypass", "allow": set()}, top=True)
    flags = [o["capped"] for o in _outs()]
    assert flags == [False, True], f"正常轮和撞上限的轮分不开:{flags}"
    # 撞上限时 `steps` 是 MAX_STEPS+1(闸在自增之后),而真正发生过的模型往返只有
    # MAX_STEPS 次。两个数放在同一份报告里对不上,而报告比的正是它们。
    st = {"mode": "bypass", "allow": set()}
    A.agent_turn(_Client([_msg(tool_calls=[_tc("read_file", '{"path": "a.py"}', f"d{i}")],
                                 usage=usage) for i in range(5)]), "m",
                 [{"role": "user", "content": "再转一次"}], st, top=True)
    assert _outs()[-1]["steps"] == st["last_tok"]["steps"], \
        f'trace 记 {_outs()[-1]["steps"]} 步,而实际模型往返 {st["last_tok"]["steps"]} 步'


def test_the_recall_block_sits_where_it_does_not_invalidate_the_prefix(ws, monkeypatch):
    """回忆块按当前任务捞,**每轮都不一样**;而 system 是整个请求的最前面。前缀缓存逐
    token 从头匹配,系统块尾部改几百字符,它后面的全部(工具 schema 2738 字符 + 整段历史)
    一起作废。实测:系统块 ~13000 字符,每轮变动的尾巴只有 434~1824 字符(3~13%),
    而短轮的命中率只有 61~63%。

    所以它挪进消息列表。这条判据钉四件事,而**第三件才是缓存真正依赖的那个**:

    ① 不在 system 里(在的话这一改等于没做)
    ② 但必须真的送到了模型面前(挪没了就成了「省了缓存,丢了功能」)
    ③ **一轮之内位置不动** —— 切点在进循环前算好。每步重算的话,`_repeat_guard` 那条
       「还剩 4 步」的提示(role=user)会把切点顶走,回忆块跟着挪,它后面的全部重算
    ④ 不落盘 —— 会话文件里存的该是人说过的话"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    monkeypatch.setattr(A, "run_tool", lambda name, args: ("ok", False))
    monkeypatch.setattr(A, "retrieve", lambda: "常驻块")
    import recall as R
    monkeypatch.setattr(R, "recall", lambda q, **k: "# 回忆\n- 这一轮捞到的东西")

    seen = []
    # **打桩要转发给脚本,别自己造答案。** 第一版这里返回的是写死的 `content="x"`,
    # 于是循环第一步就收尾:`seen` 只有一条,而下面那句「两次请求里下标必须一样」
    # 在单元素集合上恒真 —— 又一个结构上不可能失败的断言,而它正是本该抓住
    # 「切点每步重算」的那一句。
    # **MAX_STEPS=6 是这条测试的要害。** `_repeat_guard` 在 `steps == MAX_STEPS - 4` 时
    # 追加一条 role=user 的「还剩 4 步」提示 —— 那是**轮内唯一会新增 user 消息**的地方,
    # 也是「切点每步重算」唯一会出错的场景。第一版这条测试跑的是默认上限,那个提示
    # 一次都没触发,于是把切点改成每步重算,测试照样全绿:**样本软到判据成摆设。**
    monkeypatch.setattr(A, "MAX_STEPS", 6)
    script = [_msg(tool_calls=[_tc("read_file", '{"path": "a.py"}', f"c{i}")]) for i in range(2)]
    script.append(_msg(content="done"))
    client = _Client(script)
    monkeypatch.setattr(A, "_chat", lambda c, **kw: seen.append(kw["messages"])
                        or client.chat.completions.create())
    msgs = [{"role": "user", "content": "老问题"}, {"role": "assistant", "content": "老回答"},
            {"role": "user", "content": "这次的任务"}]
    A.agent_turn(client, "m", msgs, {"mode": "bypass", "allow": set()})
    assert len(seen) >= 3, f"只走了 {len(seen)} 步 —— 下面比「两次请求」的断言会恒真"
    assert any("还剩 4 步" in str(m.get("content")) for m in msgs), \
        "那条会顶走切点的提示没触发 —— 这条测试测不到「每步重算」那个错法"

    for req in seen:
        assert "这一轮捞到的东西" not in req[0]["content"], \
            "回忆块还在 system 里 —— 它后面的工具 schema 和整段历史每轮都会重算"
    assert any("这一轮捞到的东西" in str(m.get("content")) for m in seen[0]), \
        "挪出 system 之后没送到模型面前 —— 省了缓存,丢了功能"

    # ③ 轮内位置不动:两次请求里回忆块的下标必须一样
    idx = [next(i for i, m in enumerate(r) if "这一轮捞到的东西" in str(m.get("content")))
           for r in seen]
    assert len(set(idx)) == 1, f"回忆块在轮内挪了位置({idx})—— 它后面的内容每步重算"
    # 而且要在这次任务之前,不是黏在最后(黏最后的话,下一步追加的消息排在它后面,
    # 第一次调用和第二次调用的公共前缀就断在这儿了)
    assert seen[0][idx[0] + 1]["content"] == "这次的任务"

    # ④ 不落盘
    assert not any("这一轮捞到的东西" in str(m.get("content")) for m in msgs), \
        "回忆块写进了会话历史 —— /history 看到的就不再是人说过的话"


def test_no_request_ever_separates_a_tool_call_from_its_result(ws, monkeypatch):
    """**真实一轮里 400 掉的那条:**
    `An assistant message with 'tool_calls' must be followed by tool messages`。

    因果是这样的:回忆块的切点在进循环前算好(为了轮内不动、别让缓存作废),而我在注释里
    写了「循环里只往尾部追加,所以这个下标一直有效」—— **那句话是假的**:
    `maybe_compact` 会 `messages[:] = ...` 把整个列表换掉,旧下标落在新列表里就是随机位置。
    压缩过后,回忆块被插进了一条 assistant(带 tool_calls)和它的工具结果**中间**,
    接口当场 400,整轮没了。

    上一版的判据测不到,因为**它从来没让压缩在一轮之内触发** —— 压缩是这一改唯一会
    重排列表的东西,而样本里没有它。第四种形状,今天第三次。

    所以这条断言的不是「切点对不对」,是**发出去的每一条请求都合法**:任何带 tool_calls
    的 assistant,后面必须紧跟着它每一个 tool_call_id 的结果。压缩、回忆块插入、剪裁,
    谁破坏了这条都会红。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())

    # ① 先用确定性的单元断言钉住复核本身。第一版只有端到端那半,而它的初始消息只有一条
    # user,于是 `slot=0` —— **插在 0 位永远安全**,不管列表怎么被重排。三个突变体全绿,
    # 判据没在判它声称判的东西。切点落在一条 tool 上才是那个错法,这里直接摆出来。
    pair = [{"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "结果"}]
    got = A._with_recall(list(pair), "# 回忆", slot=1)          # 1 正指着那条 tool
    assert got[1]["role"] != "tool" or got[0]["role"] == "assistant", \
        f"回忆块插进了 assistant 和它的工具结果中间 —— 接口会 400:{[m['role'] for m in got]}"
    assert [m["role"] for m in got] == ["user", "assistant", "tool"], \
        f"应该退到 assistant 之前再插:{[m['role'] for m in got]}"
    assert A._with_recall(list(pair), "# 回忆", slot=99)[-1]["role"] == "user", \
        "下标越界(压缩把列表缩短了)时没有夹到合法范围"

    # ② 端到端:带着几轮历史进来,`slot` 才会是个有意义的深下标
    monkeypatch.setattr(A, "run_tool", lambda name, args: ("x" * 3000, False))
    monkeypatch.setattr(A, "retrieve", lambda: "常驻块")
    monkeypatch.setattr(A, "COMPACT_AT", 4000)          # 让压缩在这一轮之内一定触发
    import recall as R
    monkeypatch.setattr(R, "recall", lambda q, **k: "# 回忆\n- 捞到的东西")

    seen = []
    script = [_msg(tool_calls=[_tc("read_file", '{"path": "a.py"}', f"c{i}")]) for i in range(6)]
    script.append(_msg(content="done"))
    client = _Client(script)

    def _spy(c, **kw):
        msgs = kw["messages"]
        # 压缩自己也走 `_chat`。不认出来的话它会吃掉脚本里的一条,后面全错位 ——
        # 而且 `seen` 里会混进一条根本不是「这一轮的请求」的东西,判据就判错了对象。
        if msgs and "你在压缩" in str(msgs[0].get("content") or ""):
            return _msg(content="SUMMARY")
        seen.append(msgs)
        return client.chat.completions.create()
    monkeypatch.setattr(A, "_chat", _spy)
    hist = []
    for i in range(4):                       # 几轮历史,让 slot 落在列表深处
        hist += [{"role": "user", "content": f"老问题{i}"},
                 {"role": "assistant", "content": "", "tool_calls": [
                     {"id": f"h{i}", "type": "function",
                      "function": {"name": "read_file", "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": f"h{i}", "content": "y" * 900}]
    hist.append({"role": "user", "content": "干活"})
    A.agent_turn(client, "m", hist, {"mode": "bypass", "allow": set()})

    assert len(seen) >= 4, f"只发了 {len(seen)} 次请求,压缩来不及触发"
    assert any("压缩摘要" in str(m.get("content")) for r in seen for m in r), \
        "压缩没触发 —— 这条测试测不到那个错法"
    for n, req in enumerate(seen, 1):
        pending = []
        for m in req:
            if m.get("role") == "tool":
                assert pending and m["tool_call_id"] in pending, \
                    f"第 {n} 次请求:工具结果 {m['tool_call_id']} 前面没有发起它的 assistant"
                pending.remove(m["tool_call_id"])
            else:
                assert not pending, (
                    f"第 {n} 次请求:一条带 tool_calls 的 assistant 后面插进了 "
                    f"{m.get('role')} 消息,而它的结果 {pending} 还没跟上 —— 接口会 400")
                pending = [c["id"] if isinstance(c, dict) else c.id
                           for c in (m.get("tool_calls") or [])]


def test_the_cache_instrument_watches_what_was_actually_sent(ws, monkeypatch):
    """`_log_turn`(原 `_log_cache`)只哈希了 `retrieve()`,而真正发出去的 system 是
    `SYSTEM + _env_block() + learned + recalled` —— **`recalled` 按当前任务捞、几乎每轮都变,
    而它压根不在被哈希的那半边**。于是本机 34 轮里 29 轮报「system 没变」,两组命中率
    差 1.8 个点(n=5),看起来像「这个变量不重要」。**真相是仪表在测一个不动的东西。**

    前缀缓存逐 token 从头匹配,第一个不同的字符之后全部作废 —— 所以「改了多少」不重要,
    「从第几个字符开始改」才重要。判据分两半,因为坏的可能是任何一半:

    · 生产端:`agent_turn` 记下的必须**就是发给模型的那一串**(拿 `_chat` 收到的对比)
    · 消费端:`retrieve()` 没变、只有 recall 那段变了时,`prefix_kept` 必须掉下来"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    sent = {}

    def _spy(client, **kw):
        sent["system"] = kw["messages"][0]["content"]
        return _Client([_msg(content="done")]).chat.completions.create()
    monkeypatch.setattr(A, "_chat", _spy)

    state = {"mode": "bypass", "allow": set()}
    A.agent_turn(None, "m", [{"role": "user", "content": "把三个 csv 合并"}], state, top=True)
    assert state["sys_now"] == sent["system"], \
        "记下的不是发出去的那一串 —— 量的是另一个东西"

    # 非顶层不许写:子 agent 共享 state,写了就会盖掉顶层那份
    A.agent_turn(None, "m", [{"role": "user", "content": "别的事"}], state)
    assert state["sys_now"] == sent["system"] or state["sys_now"], "被子层覆盖了"

    # 消费端:只动 recall 那一段(retrieve() 不变),prefix_kept 必须掉
    head = "稳定的前缀" * 50
    st = {"sys_now": head + "回忆:A", "tok": {}}
    st["last_tok"] = {"in": 100, "cached": 80}
    A._log_turn(st, steps=1)
    st["sys_now"] = head + "回忆:完全不同的一段"
    A._log_turn(st, steps=1)
    rows = [__import__("json").loads(l) for l in open(A.CACHE_TRACE, encoding="utf-8") if l.strip()]
    assert rows[-1]["prefix_kept"] is not None, "没记前缀留存"
    assert rows[-1]["prefix_kept"] < 1.0, \
        f"只有 recall 段变了就说前缀全留住 —— 又在测那个不动的东西:{rows[-1]}"
    assert rows[-1]["prefix_kept"] > 0.5, f"共同的头明明还在,却算成全丢:{rows[-1]}"


def test_the_turn_row_splits_the_first_call_from_the_rest(ws, monkeypatch):
    """一轮一个命中率答不了「谁在漏」。第 1 次调用吃的是**跨轮**前缀
    (system + tools + 老历史),第 2..N 次吃的是**轮内**前缀(循环只往后追加,
    理论上该接近 100%)。`_prune_old_tool_results` 每步原地改写旧工具输出,
    **只会伤后者** —— 混着记就永远查不出它赔了多少。

    分开之后日常使用就在产这个数据,不用专门花几十万 token 跑对照实验。

    钉三件事:两个数按各自的分子分母算(不是把整轮的除一除)、
    只有一次调用时 `hit_rest` 必须是 None(没有轮内可言)、
    以及**子 agent 的调用不许混进来**(它挂在本层局部变量上,不挂共享的 state)。"""
    import json
    import os
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    monkeypatch.setattr(A, "run_tool", lambda name, args: ("ok", False))

    def _rows():
        with open(A.CACHE_TRACE, encoding="utf-8") as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def _u(prompt, cached):
        return types.SimpleNamespace(
            prompt_tokens=prompt, completion_tokens=10,
            prompt_tokens_details=types.SimpleNamespace(cached_tokens=cached))

    # 第 1 次 1000 里命中 200(跨轮差),之后两次 1000 里命中 900(轮内好)
    script = [_msg(tool_calls=[_tc("read_file", '{"path": "a.py"}', "c1")], usage=_u(1000, 200)),
              _msg(tool_calls=[_tc("read_file", '{"path": "a.py"}', "c2")], usage=_u(1000, 900)),
              _msg(content="done", usage=_u(1000, 900))]
    A.agent_turn(_Client(script), "m", [{"role": "user", "content": "干活"}],
                 {"mode": "bypass", "allow": set()}, top=True)
    r = _rows()[-1]
    assert r["hit_first"] == 0.2, f"跨轮那个数不是按第 1 次算的:{r}"
    assert r["hit_rest"] == 0.9, f"轮内那个数不是按第 2..N 次算的:{r}"
    assert r["hit"] == round(2000 / 3000, 3), "整轮那个数也要留着,老数据序列不能断"

    # 只有一次调用:没有「轮内」,不许拿 0 顶上去
    os.remove(A.CACHE_TRACE)
    A.agent_turn(_Client([_msg(content="done", usage=_u(500, 100))]), "m",
                 [{"role": "user", "content": "一句话"}],
                 {"mode": "bypass", "allow": set()}, top=True)
    r = _rows()[-1]
    assert r["hit_first"] == 0.2 and r["hit_rest"] is None, \
        f"单次调用的轮给「轮内」贡献了一个数 —— 它会把要看的中位数拉垮:{r}"


def test_only_a_real_permission_refusal_is_reported_as_denied(ws, monkeypatch):
    """`state["trace"]` 的 `denied` 会被 `_trace_summary` 原样报给**父 agent**。
    上一版写的是 `not allowed`,而 `allowed` 在三种情况下都是 False:权限真的拒了、
    参数根本执行不了、工具名不存在。后两种一个框都没弹、没人拒绝过任何东西 ——
    于是子 agent 的汇报里写着「权限拒了 N 次」,而实际上一次都没有。

    又是一句在某条路径上为假的话,这次的收信人是父 agent(它拿这份汇报判断子 agent
    是不是被挡住了,而这正是它无法自述、只能靠主循环记录的那件事)。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    monkeypatch.setattr(A, "run_tool", lambda name, args: ("ok", False))
    script = [_msg(tool_calls=[_tc("run_bash", "{}")]),                    # 参数不合法
              _msg(tool_calls=[_tc("no_such_tool", "{}", "c2")]),          # 工具不存在
              _msg(content="done")]
    state = {"mode": "bypass", "allow": set()}
    A.agent_turn(_Client(script), "m", [{"role": "user", "content": "干活"}], state)
    flags = [(t["tool"], t["denied"]) for t in state["trace"]]
    assert flags == [("run_bash", False), ("no_such_tool", False)], \
        f"没人拒绝过任何东西,却报成了被拒:{flags}"


def test_the_step_cap_message_does_not_promise_an_intact_history(ws, monkeypatch):
    """撞上限那句原话是「历史都还在」—— 而 `_prune_old_tool_results` 每轮把旧的大块
    工具输出原地换成「已省略」,`maybe_compact` 还会把头部换成摘要。会话确实能接着走,
    但「都还在」是假的,而人会照着它决定要不要说「继续」。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    monkeypatch.setattr(A, "run_tool", lambda name, args: ("x" * 2000, False))
    monkeypatch.setattr(A, "MAX_STEPS", 3)
    spin = [_msg(tool_calls=[_tc("read_file", '{"path": "a.py"}', f"c{i}")]) for i in range(8)]
    msgs = [{"role": "user", "content": "一直转"}]
    out = A.agent_turn(_Client(spin), "m", msgs, {"mode": "bypass", "allow": set()})
    assert "历史都还在" not in out, f"承诺了一件裁剪之后不成立的事:{out}"
    assert "裁剪" in out or "摘要" in out, f"没说清早先的内容可能已经不在了:{out}"
    assert "继续" in out, "拿掉假话的同时把能走的那条路也拿掉了"


def test_compaction_must_actually_get_under_the_threshold(ws, monkeypatch):
    """**压完还是超,下一步就再压一次。** 实测日志里连着两条:
    `压缩(33 条 → 10 条,最近 8 条原样留着)`、`压缩(11 条 → 7 条,最近 5 条原样留着)`。
    每多压一次 = 一次全量缓存作废 + 一次额外的模型调用,而信息还被摘要吃掉一层。

    根因是**单位不一致**:尾部保留按「条数」写(`COMPACT_KEEP=8`),而预算一直按「字符」
    算(`COMPACT_AT`)。一条一万字符的长回答就能让留下的 8 条自己超预算 ——
    这是我加尾部保留时没算到的代价。

    判据钉的是**性质不是机制**:压缩之后必须真的低于阈值。怎么做到的(缩尾巴、
    砍证据、还是别的)以后可以换,这条不用跟着改。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    monkeypatch.setattr(A, "_chat", lambda c, **kw: _msg(content="简报"))
    # **用真实常数,不打桩。** 第一版把 COMPACT_AT 和 COMPACT_TAIL_CHARS 都换成了小值,
    # 于是把默认预算改成无穷大(等价于没有这个机制)时测试照样全绿 ——
    # 判据只盖住了机制,没盖住那两个数本身。

    # 尾部那几条里塞一条巨长的回答 —— 光按条数留就压不下去
    msgs = [{"role": "user", "content": "干活"}]
    for i in range(6):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "x" * 3000})
    msgs.append({"role": "assistant", "content": "长" * 25000})
    assert A._ctx_chars(msgs) > A.COMPACT_AT, "样本还没超阈值,压缩根本不会触发"

    out = A.maybe_compact(None, "m", msgs, force=True)
    assert A._ctx_chars(out) < A.COMPACT_AT, (
        f"压完还是 {A._ctx_chars(out)} 字符,超过阈值 {A.COMPACT_AT} —— "
        "下一步会立刻再压一次,每次都是一次全量缓存作废加一次模型调用")
    # 别为了压下去把尾巴整个丢了:尾部保留这件事本身还得成立
    assert len(out) > 2 or A._ctx_chars(msgs[-1:]) > A.COMPACT_TAIL_CHARS, \
        "尾巴全丢了 —— 那就退回成「只剩摘要」,下一步接不上刚才那一步"


def test_the_summary_can_see_what_the_tools_actually_did(ws, monkeypatch):
    """压缩的提示词要它写「③已经做完的 ④关键决定和它的理由」,而上一版送进去的内容
    **把工具调用和工具结果全滤掉了** —— 一轮里真正发生过的事几乎都在那里面。
    **要它答的东西,输入里没有**,而两边都是同一次改动里我自己写的。

    实测:六次 `edit_file` 改 critical.py,送进摘要模型的内容里 `edit_file` 和结果里的
    哨兵串一个都不在。判据就钉这一条:摘要的输入里必须找得到工具名和结果的痕迹。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    got = {}

    def _spy(client, **kw):
        got["msgs"] = kw["messages"]
        return _Client([_msg(content="SUMMARY")]).chat.completions.create()
    monkeypatch.setattr(A, "_chat", _spy)

    msgs = [{"role": "user", "content": "改 critical.py" + "x" * 40000}]
    for i in range(6):
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"c{i}", "type": "function",
                                     "function": {"name": "edit_file",
                                                  "arguments": '{"path": "critical.py"}'}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}",
                     "content": "edited critical.py; TEST_SENTINEL 188 passed "
                                + "HEAD_MARK" + "z" * 5000 + "TAIL_MARK"})
    A.maybe_compact(None, "m", msgs, force=True)

    blob = "".join(str(m.get("content") or "") for m in got["msgs"])
    assert "edit_file" in blob, "摘要看不见调用过哪个工具,却被要求写「已经做完的」"
    assert "critical.py" in blob, "摘要看不见动过哪个文件"
    assert "TEST_SENTINEL" in blob, "摘要看不见工具返回了什么"
    # **是留证据,不是搬原文。** 每条工具结果折一行、限长 —— 不限的话「压缩」这一步
    # 自己就把整段历史又发了一遍,比不压还贵。
    assert "HEAD_MARK" in blob and "TAIL_MARK" not in blob, \
        "大块工具输出被整个搬进了摘要输入 —— 压缩自己成了最贵的那一步"


def test_compaction_keeps_the_last_few_messages_verbatim(ws, monkeypatch):
    """摘要写不出「刚才那一步的原文」,而下一步恰恰接在那上面。

    上一版把整段历史压成 2 条,尾部一条不留。实测:一轮 32 步的任务压缩之后,模型花了
    约十次调用重新读它刚读过的文件、重新列它刚列过的目录,直到重复熔断把它拽出来。
    摘要告诉它「做过什么」,但它需要的是「上一条工具返回了什么」。

    切尾部不能直接 `messages[-keep:]`:切点落在 `tool` 消息上,它的 `assistant`
    tool_calls 留在被摘要的那一半 —— **一条没有来处的工具结果,接口当场报错**。
    所以起点要往后挪到第一条不是 tool 的消息。这条断言的就是这两件事。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    # 切点落在 tool 上时必须往后挪 —— 这一条直接钉 `_tail_start`,不绕端到端
    msgs = [{"role": "user", "content": "干活"}]
    for i in range(8):
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [_tc("read_file", "{}", f"c{i}")]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"结果{i}"})
    for keep in range(1, len(msgs)):
        i = A._tail_start(msgs, keep)
        assert msgs[i].get("role") != "tool", \
            f"keep={keep} 时切在了工具结果上 —— 它的 assistant 会被摘要吃掉,接口直接报错"
        assert i > 0, f"keep={keep} 时整段都成了尾巴,没有头可摘"

    client = _Client([_msg(content="SUMMARY")])
    msgs[0]["content"] = "干活" + "x" * 40000
    out = A.maybe_compact(client, "m", list(msgs), force=True)
    assert "SUMMARY" in out[0]["content"], "摘要没进去"
    assert out[-1]["content"] == "结果7", f"最后一条不是原文:{out[-1]}"
    assert len(out) > 2, "又压成了只剩摘要 —— 下一步接不上刚才那一步"
    # 落单的 tool:每一条工具结果前面都必须有发起它的那条 assistant
    seen = set()
    for m in out:
        for c in (m.get("tool_calls") or []):
            seen.add(c.id if hasattr(c, "id") else c["id"])
        if m.get("role") == "tool":
            assert m["tool_call_id"] in seen, \
                f"落单的工具结果 {m['tool_call_id']} —— 它的 assistant 被摘要吃掉了"


def test_cheap_pruning_runs_before_paying_the_model(ws, monkeypatch):
    """分级治理:先剪旧工具输出,不够再叫模型。`_prune_old_tool_results` 本来就存在,
    但它跑在压缩**之后** —— 于是压缩每次都对着没剪过的历史叫模型。同一个便宜手段,
    只是没用在该用的地方。"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    calls = []
    monkeypatch.setattr(A, "_chat", lambda *a, **k: calls.append(1) or _Client(
        [_msg(content="SUMMARY")]).chat.completions.create())
    msgs = [{"role": "user", "content": "干活"}]
    for i in range(12):
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [_tc("read_file", "{}", f"c{i}")]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "大块输出" * 1000})
    assert A._ctx_chars(msgs) > A.COMPACT_AT
    out = A.maybe_compact(None, "m", msgs)
    assert calls == [], "剪一下就够了,却还是花钱叫了模型"
    assert A._ctx_chars(out) < A.COMPACT_AT, "剪完还是超,那这一级就是白加的"
    assert any("已省略工具输出" in str(m.get("content")) for m in out), "根本没剪"

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

def test_reflection_says_so_even_when_it_decides_to_write_nothing(ws, monkeypatch):
    """复盘开口那句(`🧠 这次用了 N 步 — 复盘看有没有值得记的…`)是无条件打的,而收尾那句
    原来只在**写了 memory 行**时才打。于是最常见的那个结局 —— 看过了、判断没什么可记的、
    一个字都不写 —— 屏幕上是开口那句悬着,后面直接是提示符。

    而复盘要跑十几到八十秒。真实一轮里用户问的就是这个:「为什么最后还是模型思考中,
    但是还没出结果」—— 它没卡,它做完了。**「做完了什么都没记」和「卡住了」长得一模一样。**

    这是第二十节那三层沉默的同一个形状,而且是最刺眼的一种:被安静扔掉的那个行为
    是**正确**的(不该学的就别学),于是系统做对了事,看起来却像故障。

    收尾必须无条件,内容才分情况。改技能和新建技能同样算「记下了东西」——
    只数文件个数的话,查重成功那次(UPDATE 而不是 NEW,正是想要的行为)会被
    报成「什么都没记」。"""
    import os
    import agent as A
    notes = []
    monkeypatch.setattr(A, "ui", _ui())
    monkeypatch.setattr(A.ui, "note", lambda s: notes.append(s))
    monkeypatch.setattr(A, "agent_turn", lambda *a, **k: "done")
    monkeypatch.setattr(A, "_known_skills", lambda t: "")
    monkeypatch.setattr(A, "_memory_lines", lambda: [])
    monkeypatch.setattr(A, "_tag_new_memory", lambda before: 0)
    msgs = [{"role": "user", "content": "算第 10 个斐波那契数"}]

    # ① 什么都没写 —— 也得说一声
    notes.clear()
    A.reflect(None, "m", msgs, {"mode": "bypass", "allow": set()})
    assert notes, "复盘一个字都没说 —— 屏幕上只剩「复盘看有没有值得记的…」和一个提示符"
    assert "没什么值得记的" in " ".join(notes), f"没说清是「做完了」还是「还在跑」:{notes}"

    # ② 改了一条已有技能(查重成功那条路)—— 不许报成「什么都没记」
    os.makedirs(A.SKILLS_DIR, exist_ok=True)
    p = os.path.join(A.SKILLS_DIR, "existing.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write("---\nname: existing\ndescription: 旧的\n---\n1. old\n")

    def _edit(*a, **k):
        with open(p, "a", encoding="utf-8") as f:
            f.write("2. 这次补的\n")
        return "done"
    monkeypatch.setattr(A, "agent_turn", _edit)
    notes.clear()
    A.reflect(None, "m", msgs, {"mode": "bypass", "allow": set()})
    joined = " ".join(notes)
    assert "没什么值得记的" not in joined, \
        f"改了一条已有技能却报成什么都没记 —— UPDATE 正是想要的行为:{notes}"
    assert "技能有改动" in joined, f"改动没被说出来:{notes}"


def test_every_state_key_is_classified_and_child_state_follows_it():
    """`state` 里混着三类性质完全不同的东西,分错档已经出过三次事:repeat 计数被子轮
    清零、`capped` 让父任务的复盘被跳过、`asked` 漏继承导致子 agent 删用户点名的文件时
    **一声不吭**。三次的修法都是「把这个键挪到对的那一档」。

    而这张分类表一直只活在**注释**里 —— 三份副本、零个判据。第四次因此栽:外部审阅逮到
    两处注释与实现矛盾(`asked` 被写成"本轮",而 `_CHILD_KEYS` 里它明确被继承),
    **而我第一次只修了两份中的一份。**

    所以判据的形状是「**一个都不许漏**」,不是「这张表看着对吗」——
    AST 扫 agent.py 里真正碰过的每一个 state 键,新加一个忘了分类,当场红。
    枚举合法的永远落后一步(第三十二节),这条反过来:**枚举全部,要求每个都归档**。"""
    import ast
    import io
    import agent as A

    tree = ast.parse(io.open(A.__file__, encoding="utf-8").read())
    used = set()
    for n in ast.walk(tree):
        if (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
                and n.value.id in ("state", "parent", "child")
                and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str)):
            used.add(n.slice.value)
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and isinstance(n.func.value, ast.Name)
              and n.func.value.id in ("state", "parent", "child")
              and n.func.attr in ("get", "setdefault", "pop") and n.args
              and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)):
            used.add(n.args[0].value)
    assert len(used) > 10, f"扫描本身失效了,只找到 {used} —— 全绿会变成假消息"

    table = {"继承": set(A.STATE_INHERIT), "汇总": set(A.STATE_SHARED), "不跨层": set(A.STATE_LOCAL)}
    classified = set().union(*table.values())
    assert not (used - classified), (
        f"这些 state 键没被分档,而分错档已经出过三次事:{sorted(used - classified)}\n"
        "把它加进 agent.py 的 STATE_INHERIT / STATE_SHARED / STATE_LOCAL 之一")
    assert not (classified - used), \
        f"分类表里有已经没人用的键,留着会误导:{sorted(classified - used)}"
    for a in table:
        for b in table:
            if a < b:
                assert not (table[a] & table[b]), f"{a} 和 {b} 都收了 {table[a] & table[b]}"

    # 分类表说了算,`_child_state` 就得照着做 —— 上一版的测试在自己代码里重拼了一遍这个
    # dict,于是把生产代码改回去,测试照样绿(那条教训写在 `_child_state` 的 docstring 里)。
    parent = {k: f"<{k}>" for k in used}
    child = A._child_state(parent)
    assert set(child) == set(A.STATE_INHERIT) | set(A.STATE_SHARED), \
        f"子 state 拿到的跟分类表对不上:多了 {set(child) - classified},少了 " \
        f"{(set(A.STATE_INHERIT) | set(A.STATE_SHARED)) - set(child)}"
    assert not (set(child) & set(A.STATE_LOCAL)), "本轮字段漏给了子 agent"


def test_a_subagent_hitting_the_step_cap_does_not_cancel_the_parents_reflection(monkeypatch):
    """一个 state 里混着三类性质完全不同的东西,而子 agent 原来拿的是父的同一个 dict:

        继承 — mode / allow / view / asked / denied   子轮该按同样的权限和同样的"用户点名过什么"跑
        汇总 — tok / trace                            一次请求的总账,子轮的消耗算在父头上
        本轮 — capped / last_* / since_reflect        只描述"刚刚这一轮",跨层就是错的

    (`asked` 这一栏上一版写在"本轮"里,和 `_CHILD_KEYS` 里它明确被继承直接矛盾 ——
    外部审阅逮到的,而我当时只改了 agent.py 那一处,漏了这里。**同一句话写在两个地方,
    修一处就等于留了一个会把人带回去的坑。** 这也正是下一条待办要解决的东西:
    把这张分类表从"两处注释"变成一条会红的判据。)

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
        st["last_tok"] = {"in": 1000, "out": 10, "cached": cached}
        A._log_turn(st, steps=3)
    rows = [json.loads(l) for l in open(tmp_path / "cache.jsonl", encoding="utf-8")]
    assert [r["sys_changed"] for r in rows] == [False, False, True], rows
    assert [r["hit"] for r in rows] == [0.9, 0.95, 0.3]
    st["last_tok"] = {"in": 0, "out": 0, "cached": 0}
    A._log_turn(st)                                            # 空轮不记
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
    st["last_tok"] = {"in": 100, "out": 1, "cached": 50}
    A._log_turn(st, steps=2)
    row = json.loads(open(tmp_path / "c.jsonl", encoding="utf-8").readline())
    assert row["reads"] == 4, row
    assert "reads" not in st, "计数没清零,下一轮会把这轮的读数算进去"
    st["last_tok"] = {"in": 100, "out": 1, "cached": 50}
    A._log_turn(st, steps=2)
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

def test_an_unrunnable_call_never_reaches_the_permission_box(ws, monkeypatch):
    """参数**先验后问**这条接线,自己一直没人测。

    `_bad_args` 上线时我在 commit 里写了「反向验证:退回旧实现当场红」—— 那句验的是
    同一条 commit 里的 `_targets`,不是这个。全仓库 `grep -rn _bad_args tests/` 零命中,
    把 agent_turn 里那两行删掉,153 条测试照样全绿。**判据存在 ≠ 判据在被执行**,
    这是今天第三次撞上同一个形状,而这次是在我自己刚写完那条 commit 之后。

    要守的不变量:一次**根本执行不了**的调用不配弹权限框。反过来的代价是真实的 ——
    `run_bash {}` 弹出的框里参数是空的,人按 [a](反射性答案),这一下放行的是整个
    bash 类,下一条真命令直接不问了。

    所以这条从 agent_turn 走真实分发,只断言外部可见的三件事:框没弹、会话授权没变、
    模型收到的是能照着改的说明。故意用 `mode="default"` + `ask -> "a"`:退回旧实现时,
    第一条就会把 run_bash 加进 allow。"""
    import agent as A
    boxes = []
    ui = _ui()
    ui.preview = lambda name, args: boxes.append(name)
    ui.ask = lambda: "a"                      # 人几乎总是按 a —— 这正是它危险的原因
    ui.ask_again = lambda ans: "a"
    monkeypatch.setattr(A, "ui", ui)
    monkeypatch.setattr(A, "run_tool", lambda name, args: ("ran", False))
    script = [_msg(tool_calls=[_tc("run_bash", "{}")]),              # 少必填参数
              _msg(tool_calls=[_tc("run_bash", "[]", "c2")]),        # 合法 JSON 但不是对象
              _msg(content="done")]
    messages = [{"role": "user", "content": "hi"}]
    state = {"mode": "default", "allow": set()}
    assert A.agent_turn(_Client(script), "m", messages, state) == "done"
    assert boxes == [], f"为一次执行不了的调用弹了权限框:{boxes}"
    assert state["allow"] == set(), "一次执行不了的调用换到了整个会话的 run_bash 授权"
    tools = [m["content"] for m in messages if m.get("role") == "tool"]
    assert "必填参数" in tools[0] and "command" in tools[0]      # 说清楚缺什么
    assert "JSON 对象" in tools[1] and "list" in tools[1]        # 说清楚收到的是什么


def test_a_required_parameter_with_no_value_is_not_runnable_either(ws, monkeypatch):
    """键存在 ≠ 能执行。上一版 `_bad_args` 只数键,于是两种调用照样换到权限框:

    · `{"command": null}` —— 过了键检查,然后在 check_permission 里拿 None 去
      `os.path.normcase`,抛 TypeError。那行在 run_tool 的 try **外面**,整轮当场没了 ——
      而这正是 `_bad_args` 号称堵上的那个洞,它只堵了非对象那一半。
    · `{"command": ""}` —— 不崩,弹出一个**空的**权限框。人按 [a](框里没东西可看,
      更不会犹豫),放行的是整个 bash 类。

    空串不能一刀切:`write_file{content:""}` 是建空文件,`edit_file{new:""}` 是删掉一段,
    两者都合法。所以判据默认拒绝、例外写明(`_MAY_BE_BLANK`),而不是反过来。"""
    import agent as A
    # 不能执行的:
    for args, why in ((None, "null 值"), ("", "空串"), ("   ", "纯空白"), (12, "类型不对")):
        assert A._bad_args("run_bash", {"command": args}) is not None, f"{why} 被放行了"
    assert A._bad_args("edit_file", {"path": "a.py", "old": "", "new": "y"}) is not None
    assert A._bad_args("spawn_subagent", {"task": " \n "}) is not None
    # 合法的空,一个都不能误伤:
    assert A._bad_args("write_file", {"path": "a.txt", "content": ""}) is None, "写空文件是合法的"
    assert A._bad_args("edit_file", {"path": "a.py", "old": "x", "new": ""}) is None, "删掉一段是合法的"
    assert A._bad_args("read_file", {"path": "a.py", "offset": "abc"}) is None, \
        "可选参数写错是一次能执行、会干净失败的调用,不该在权限之前拦"

    # **schema 里声明的每种 type 都要校验,不只是 string。** 上一版只校验 string,
    # 理由是「内置工具的必填字段全是 string」—— 那是拿当前的工具表当判据,而
    # `create_tool` 的 parameters/required 是模型自己写的。
    A.TOOLS["_probe_types"] = (lambda a: "ok",
                               {"n": {"type": "integer"}, "flag": {"type": "boolean"},
                                "xs": {"type": "array"}, "who": {"type": "nope"}},
                               ["n", "flag", "xs", "who"], "probe", "bash")
    try:
        ok = {"n": 1, "flag": True, "xs": [1], "who": "随便"}
        assert A._bad_args("_probe_types", ok) is None, "全对的一组被拦下来了"
        for k, bad, why in ((("n"), "not-an-integer", "整数收了字符串"),
                            ("n", True, "isinstance(True, int) 为真,bool 混进整数了"),
                            ("flag", 1, "布尔收了整数"),
                            ("xs", {"a": 1}, "数组收了对象")):
            assert A._bad_args("_probe_types", {**ok, k: bad}) is not None, why
        # 声明了不认识的 type 就别管 —— 那是"没声明",拦它属于自己发明规矩
        assert A._bad_args("_probe_types", {**ok, "who": 123}) is None
        # 但 null 例外,而这一条上面那行**盖不住**:审计说 `v is None` 那两行是冗余的
        # (`_type_ok(None, "string")` 本来就是 False),摘掉全绿 —— 全绿是因为没人测
        # 不认识的 type。声明了 `"type": "nope"` 时 `_type_ok` 一律放行,这两行就是
        # 拦住 null 的唯一一道。**「摘掉没测试红」有两种解释,这次是第二种。**
        assert A._bad_args("_probe_types", {**ok, "who": None}) is not None, \
            "type 不认识时 null 一路放行了 —— 它会带着 None 走到 check_permission"
    finally:
        del A.TOOLS["_probe_types"]

    # run_tool 复用同一个判据。原来那份「同样的检查」只查键存在 —— 一个 required=[]
    # 的自建工具,`run_tool(name, [])` 会把列表一路交到工具函数手里。
    A.TOOLS["_probe_norequired"] = (lambda a: f"got-{type(a).__name__}", {}, [], "probe", "read")
    try:
        out, is_err = A.run_tool("_probe_norequired", [])
        assert is_err and "JSON 对象" in out, f"非对象参数直接进了工具函数:{out!r}"
    finally:
        del A.TOOLS["_probe_norequired"]

    # 接线:空值的调用同样不许弹框、不许换授权,而且不许把整轮带走。
    boxes = []
    ui = _ui()
    ui.preview = lambda name, args: boxes.append(name)
    ui.ask, ui.ask_again = (lambda: "a"), (lambda ans: "a")
    monkeypatch.setattr(A, "ui", ui)
    script = [_msg(tool_calls=[_tc("run_bash", '{"command": null}')]),
              _msg(tool_calls=[_tc("run_bash", '{"command": "  "}', "c2")]),
              _msg(content="done")]
    messages = [{"role": "user", "content": "hi"}]
    state = {"mode": "default", "allow": set()}
    assert A.agent_turn(_Client(script), "m", messages, state) == "done", "整轮被守卫带走了"
    assert boxes == [] and state["allow"] == set()


def test_a_schema_that_declares_two_types_must_not_take_the_turn_down(ws, monkeypatch):
    """`"type": ["string", "null"]` 是合法 JSON Schema,而 `create_tool` 的 parameters
    是模型自己写的。上一版 `_type_ok` 里那句 `want not in _JSON_TYPES` 拿列表去查字典 ——
    `TypeError: unhashable type: 'list'`。

    要命的是它抛在哪:`_bad_args` 跑在 `check_permission` 之前、在 `run_tool` 的 try
    **外面**(agent.py 那段注释写的就是「抛出去是整轮没了」)。于是**这道防止整轮没了
    的闸,自己把整轮带走了** —— 而且是模型给自己写工具时踩,不是敌手构造的输入。

    所以这条断言的不是"拦住"也不是"放过",是**别崩**:union type 按"没声明"处理
    (跟 `"type": "nope"` 同一条规矩,不自己发明),但 null 仍然要拦住。"""
    import agent as A
    A.TOOLS["_probe_union"] = (lambda a: "ok", {"u": {"type": ["string", "null"]}},
                               ["u"], "probe", "bash")
    try:
        assert A._bad_args("_probe_union", {"u": "x"}) is None
        assert A._bad_args("_probe_union", {"u": 123}) is None, "union 当没声明处理,不该拦"
        assert A._bad_args("_probe_union", {"u": None}) is not None, "null 还是不能执行"
    finally:
        del A.TOOLS["_probe_union"]


def test_the_error_a_model_gets_back_must_name_the_parameters_it_has_to_supply(ws):
    """`_schema_hint` 是错误信息里唯一**可执行**的部分 —— 「少了必填参数 ['names']」只说了
    哪儿错了,照着它改不出下一次调用;后面那句「应该是 {"names": "<array>"},其中 names
    必填」才是模型能照着改的。而把 `_schema_hint` 整个打桩成 `''`,172 条全绿:所有断言
    都只查前半句。

    这在自建工具上最要紧:内置工具的 schema 模型见过很多遍,自己刚写的那个没见过,
    只有这句提示能告诉它参数长什么样。所以断言绑在**工具自己的 schema** 上 —— 名字、
    类型、哪些必填都得从 TOOLS 里读出来,写死一句"应该是 JSON 对象"过不了。"""
    import agent as A
    A.TOOLS["_probe_hint"] = (lambda a: "ok",
                              {"quokka": {"type": "array"}, "wombat": {"type": "integer"}},
                              ["quokka"], "probe", "bash")
    try:
        for args in ({}, [], {"quokka": None}, {"quokka": "不是数组"}):
            msg = A._bad_args("_probe_hint", args)
            assert msg is not None
            for want in ("quokka", "array", "wombat", "integer"):
                assert want in msg, f"{args!r} 的报错里没有 {want} —— 模型照着它改不出来:{msg}"
            assert "必填" in msg and "wombat" not in msg.split("其中")[-1], \
                f"没说清哪些必填,可选的 wombat 会被当成必填补上:{msg}"
    finally:
        del A.TOOLS["_probe_hint"]


def test_the_read_budget_message_must_be_true_from_the_subagents_side(ws, monkeypatch):
    """读预算**故意**跨子 agent 累计(父烧完额度派个子 agent 就又有一份,那道闸等于没有)。
    那条设计是对的。坏的是它说的话。

    真实一轮:父读了 13 次撞上限,派出去的子 agent 一个字都没读过,收到的却是
    「这一轮**你**已经读了 13 次…用**你已经读到的**内容往下做」。子 agent 回话说
    `this environment is intercepting every attempt to read those two files`——
    **它没胡说,它是照着一句在它视角下为假的话做的正确推理。**

    今天第四次撞同一类:假的拒绝理由(把模型推向 python -c)、假的警告(让人拒掉正确的
    清理)、该说而没说(复盘的沉默像卡死),现在是**换个视角才假**的话。
    所以判据是:这句话在**每个视角下**都得成立,而且得给没花过额度的那个一条能走的路。"""
    import os
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    p = os.path.join(A.WORKSPACE, "big.py")
    open(p, "w", encoding="utf-8").write("x = 1\n" * 50)

    def _burn():
        seen = {}
        for i in range(A.READ_LIMIT * 3):
            out = A._read_guard(seen, "read_file", {"path": p}, f"片段{i}")
            if "片段" not in out:
                return out
        raise AssertionError("烧不满额度,这条测试没测到东西")

    monkeypatch.setitem(A._RUNTIME, "depth", 0)
    top = _burn()
    assert "别再一段一段翻" in top and "用你已经读到的内容" in top

    monkeypatch.setitem(A._RUNTIME, "depth", 1)          # 子 agent 里
    sub = _burn()
    assert "用你已经读到的内容" not in sub, \
        "对一个可能一个字都没读过的子 agent 说「用你已经读到的内容往下做」—— 它可能没有"
    # **上一版这里断言的是「这份额度不是你花的」,而这条测试自己构造的场景恰恰是子 agent
    # 自己烧满的**(`_burn` 每次新建 `seen`)—— 我断言了一句在测试自身场景里为假的话,
    # 而生产代码在同一场景下照样会那么说。第三十四节刚写完「递给谁?那个人知道什么?」,
    # 下一条测试就把收信人搞错了。现在只说能证明的:额度整次请求共用,来源不确定;
    # 而「不是环境在拦你」在两种来源下都成立。
    assert "整次请求共用" in sub and "不是环境在拦你" in sub, \
        f"没说清额度是共用的,它会以为环境坏了:{sub}"
    assert "不是你花的" not in sub, "又断言了一句在这个场景里不成立的话"
    assert "让派你来的那一层接着处理" in sub, "拦住了却没给一条能走的路"
    # 两边都不许再说「这一轮你已经读了」—— 额度是按一次请求算的,不是按这一层
    for msg in (top, sub):
        assert "你已经读了" not in msg, f"这句话换个视角就是假的:{msg[:60]}"


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
    # 值是 (次数, mtime) —— mtime 是为了让「文件没变」那句话变成真的,见 _read_guard
    assert sum(v[0] for v in seen7.values()) == A.READ_LIMIT - 1, "一条命令把同一个文件数了不止一次"


def test_a_broken_guard_must_not_take_the_whole_turn_down(ws, monkeypatch):
    """两条守卫跑在 `run_tool` 的 try/except **外面**,所以它们抛什么都直接冲出 agent_turn。
    实测:路径里带 `\x00` 的 read_file —— `os.stat`/`open` 抛的是 `ValueError`,而
    `_read_key` 只 `except OSError`,接不住。与其追每一种可能的异常,不如认下这件事:
    **守卫是省钱的,不是干活的;它自己坏了就别守,不该拦下整轮的活。**"""
    import agent as A
    monkeypatch.setattr(A, "ui", _ui())
    monkeypatch.setattr(A, "run_tool", lambda name, args: ("正常结果", False))
    # NUL 要写成 **JSON 转义**(源码里两个反斜杠),不能是 Python 层面的裸 NUL:
    # 裸的那种 `json.loads` 当场就拒(strict 模式不许字符串里有控制字符),args 退化成
    # `{}`,**那个 NUL 根本到不了守卫** —— 这条断言一直靠 run_tool 被打桩才绿。
    # 是「参数先验后问」那条改动把它暴露出来的:空 args 现在会被当成缺参报错。
    script = [_msg(tool_calls=[_tc("read_file", '{"path":"a\\u0000b.py"}', cid="c0")]),
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


def test_the_guard_and_the_file_tools_must_resolve_paths_the_same_way(ws, monkeypatch):
    """`v.py` 和 `workspace/v.py` 是同一个文件,守卫却把它们记在两个计数器上 ——
    因为 `_in_workspace` 先走 `_strip_workspace_prefix`,而守卫没走。后果有两个:
    预算凭空翻倍,以及 `write_file("workspace/v.py")` 清了个不存在的键,于是
    边写边看的循环照样在第 6 轮断掉。**而那个函数的文档字符串写的就是
    「模型会照抄 workspace/ 这个前缀」。** 上一轮我收敛了三个拼法,正好停在第四个之前。"""
    import agent as A, os
    monkeypatch.setattr(A, "ui", _ui())
    # `_strip_workspace_prefix` 脱的是**工作区自己的名字**,所以工作区必须真叫 workspace ——
    # ws 夹具给的是 tmp_path,名字是随机的,拿它测这条等于没测。
    real = os.path.join(A.WORKSPACE, "workspace")
    os.makedirs(real, exist_ok=True)
    monkeypatch.setattr(A, "WORKSPACE", os.path.realpath(real))
    p = os.path.join(A.WORKSPACE, "v.py")
    open(p, "w", encoding="utf-8").write("x = 1\n")
    spells = ("v.py", "./v.py", "workspace/v.py", p)
    assert len({A._read_key(s) for s in spells}) == 1, \
        "同一个文件的四种拼法没落在一个键上:" + repr({s: A._read_key(s)[1] for s in spells})
    seen = {}
    for i in range(A.READ_LIMIT):
        last = A._read_guard(seen, "read_file", {"path": spells[i % 4]}, "内容")
    assert "别再一段一段翻" in last and len(seen) == 1
    # 写入清零也得认这个拼法
    A._read_guard(seen, "write_file", {"path": "workspace/v.py"}, "wrote")
    assert seen == {}, f"用 workspace/ 前缀写入之后没清零: {seen}"


def test_a_file_that_actually_changed_is_not_refused(ws, monkeypatch):
    """守卫的说辞是「文件没变,再读一遍不会读出新东西」—— 那句话原来是猜的:
    只有 write_file / edit_file 清零,而 `run_bash` 是第三个写入者(脚本原地覆写、
    重定向、模型自己 open(o,'w'))。于是刚被重新生成的文件在第 6 次被拒,
    还附赠一句每轮都为假的话。改成问 mtime —— **问文件系统,别在正则上加分支。**"""
    import agent as A, os, time
    monkeypatch.setattr(A, "ui", _ui())
    out = os.path.join(A.WORKSPACE, "gen.txt")
    seen = {}
    for i in range(A.READ_LIMIT * 3):
        open(out, "w", encoding="utf-8").write(f"第 {i} 版\n")     # 脚本重新生成了它
        os.utime(out, (1e9 + i, 1e9 + i))                          # mtime 分辨率不背这个锅
        assert A._read_guard(seen, "read_file", {"path": out}, f"第 {i} 版") == f"第 {i} 版", \
            f"第 {i} 轮:文件真的变了,却被当成打转拦下"
    # 没变的时候照拦 —— 别把守卫治没了
    for _ in range(A.READ_LIMIT * 2):
        last = A._read_guard(seen, "read_file", {"path": out}, "内容")
    assert "别再一段一段翻" in last, "文件不变时守卫失效了"


def test_a_pure_write_command_is_not_counted_as_reading_its_own_output(ws, monkeypatch):
    r"""`_READISH` 里有 `open\s*\(`,于是 `python -c "open(o,'w').write(x)"` 被算成
    读它自己的输出:六次之后模型被告知「别再翻页」,而它翻的是自己正在写的文件。"""
    import agent as A, os
    monkeypatch.setattr(A, "ui", _ui())
    o = os.path.join(A.WORKSPACE, "o.txt")
    seen = {}
    for i in range(A.READ_LIMIT * 3):
        open(o, "w", encoding="utf-8").write(f"{i}\n")
        os.utime(o, (1e9 + i, 1e9 + i))
        cmd = {"command": f"""{A.sys.executable} -c "open('{o}','w').write('{i}')" """}
        assert A._read_guard(seen, "run_bash", cmd, "写完了") == "写完了", f"第 {i} 次写被当成读拦下"
    # 解释器自己永远不算被读的文件 —— POSIX 上它叫 `bin/python`,**没有扩展名**,
    # 按扩展名挡的那道闸在那边整个失效,而它就在每条命令的开头。
    assert A._SELF not in {k[1] for k in seen}, "解释器被记成了被读的文件"
    # 上面那条在 Windows 上判不到:`.exe` 那道过滤先把它接住了,所以拆掉身份判据它照样绿
    # —— **判据写在 python.exe 这个名字上,Linux 的两格 CI 就永远红不了。**
    # 用一个没有扩展名的假解释器,两个平台都能红。
    fake = os.path.join(A.WORKSPACE, "python")            # 没有扩展名,跟 POSIX 上一样
    open(fake, "w", encoding="utf-8").write("")
    monkeypatch.setattr(A, "_SELF", os.path.normcase(os.path.realpath(fake)))
    seen2 = {}
    for i in range(A.READ_LIMIT * 2):
        A._read_guard(seen2, "run_bash",
                      {"command": f"""{fake} -c "print(open('x{i}.txt').read())" """}, "输出")
    assert seen2 == {}, f"没有扩展名的解释器被记成了被读的文件: {seen2}"


def test_the_read_budget_carries_into_subagents(ws, monkeypatch):
    """两条守卫看着像一对,守的不是一回事:`_repeat_guard` 守「这一轮卡住了」,per-turn 才对;
    `_read_guard` 守的是 **token 预算**,而子 agent 的 token 本来就滚进父的 state["tok"]。
    不累计的话,父烧完 6 次读,派个子 agent 就又有 6 次,内容照样整份发回来 ——
    而「守卫响了就绕路」是这个模型实测过的行为(它写过三十个 extractN.py)。"""
    import agent as A, os
    monkeypatch.setattr(A, "ui", _ui())
    _real = A.run_tool          # 只把 read_file 换成桩,spawn_subagent 必须真跑
    monkeypatch.setattr(A, "run_tool",
                        lambda name, args: ("原始内容", False) if name == "read_file" else _real(name, args))
    A._RUNTIME.pop("reads", None); A._RUNTIME.pop("depth", None)
    rd = lambda i: _msg(tool_calls=[_tc("read_file", '{"path":"same.py","offset":%d}' % i, cid="c%d" % i)])
    # 父读满 READ_LIMIT 次 → 派子 agent → 子 agent 再读同一个文件
    script = ([rd(i) for i in range(A.READ_LIMIT)]
              + [_msg(tool_calls=[_tc("spawn_subagent", '{"task":"接着读"}', cid="sp")])]
              + [rd(100), _msg(content="子完成")]          # 子 agent 那一轮
              + [_msg(content="done")])
    messages = [{"role": "user", "content": "读它"}]
    A.agent_turn(_Client(script), "m", messages, {"mode": "bypass", "allow": set()})
    # 守卫的话进的是子 agent **内部**的消息,父只拿到最终答案 —— 所以断在计数器上:
    # 接上了就是 READ_LIMIT+1,子轮自己从头数就是 1。这两个值差得足够远,判据不含糊。
    cnt = A._RUNTIME["reads"].get(A._read_key("same.py"), (0, None))[0]
    assert cnt == A.READ_LIMIT + 1, \
        f"子 agent 里那次读没接着父的计数(现在 {cnt},接上了应该是 {A.READ_LIMIT + 1})"
    # 而最外层重新起一轮必须清零 —— 否则预算跨任务累计,第二个任务一开局就被拦
    A.agent_turn(_Client([_msg(content="下一轮")]), "m",
                 [{"role": "user", "content": "新任务"}], {"mode": "bypass", "allow": set()})
    assert A._RUNTIME["reads"] == {}, "最外层新起一轮没清空读预算"


def test_pages_counts_lines_the_same_way_the_pager_splits_them(ws, monkeypatch):
    """`read_file` 用 `splitlines()` 分页,`_pages` 只数 `\n` —— 单位不一样。
    一个 `\r` 结尾(老 Mac)或带换页符的文件会**少算四倍**,上限跟着缩水,
    整本还没翻完一遍就被拦。`_pages` 存在的全部理由就是让上限跟着分页的单位走。"""
    import agent as A, os
    for sep in (b"\r", b"\x0c", b"\r\n", b"\n"):
        p = os.path.join(A.WORKSPACE, "sep.txt")
        with open(p, "wb") as f:
            f.write(sep.join(b"x" * 5 for _ in range(A.READ_MAX_LINES * 4)))
        real = len(open(p, encoding="utf-8", errors="replace").read().splitlines())
        want = max(1, -(-real // A.READ_MAX_LINES))
        assert A._pages(p) == want, f"分隔符 {sep!r}: _pages={A._pages(p)} 而真实要翻 {want} 次"
