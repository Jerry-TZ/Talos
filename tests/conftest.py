"""pytest 配置:让 tests 能 import talos 模块,并把所有文件根重定向到临时目录
(测试绝不碰真实项目文件)。"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # talos/
sys.path.insert(0, HERE)

@pytest.fixture
def ws(tmp_path, monkeypatch):
    """临时工作区:把 agent/session/recall 的路径全指向 tmp,返回该目录字符串。"""
    import agent
    import recall
    import session
    d = str(tmp_path)
    monkeypatch.setattr(agent, "WORKSPACE", os.path.realpath(d))
    monkeypatch.setattr(agent, "MEMORY_FILE", os.path.join(d, "memory.md"))
    monkeypatch.setattr(agent, "SKILLS_DIR", os.path.join(d, "skills"))
    monkeypatch.setattr(agent, "TOOLS_DIR", os.path.join(d, "tools"))
    monkeypatch.setattr(agent, "TRASH_DIR", os.path.join(d, "trash"))
    # 这一条是漏掉之后被逮到才补的:`_log_cache` 直接写 `CACHE_TRACE`,而这张清单没有它 ——
    # 一条新测试跑一次,两行合成数据就落进了**真实的** .talos/cache_trace.jsonl,
    # 而那份文件正是用来判断缓存优化有没有效的样本。这个 fixture 的 docstring 写着
    # 「测试绝不碰真实项目文件」,而它自己是一张**枚举**出来的清单 —— 枚举永远落后一步。
    monkeypatch.setattr(agent, "CACHE_TRACE", os.path.join(d, "cache_trace.jsonl"))
    # 回收站已改成问磁盘,不再有模块级缓存要清
    monkeypatch.setattr(session, "SESS_DIR", os.path.join(d, "sessions"))
    monkeypatch.setattr(recall, "MEMORY_FILE", os.path.join(d, "memory.md"))
    monkeypatch.setattr(recall, "SKILLS_DIR", os.path.join(d, "skills"))
    monkeypatch.setattr(recall, "SESS_DIR", os.path.join(d, "sessions"))
    monkeypatch.setattr(recall, "HITS_FILE", os.path.join(d, "hits.json"))
    monkeypatch.setattr(recall, "TRACE_FILE", os.path.join(d, "recall_trace.jsonl"))
    return os.path.realpath(d)

@pytest.fixture(autouse=True)
def _no_test_ever_writes_the_real_talos(tmp_path, monkeypatch):
    """把三个「追加式落盘」的路径无条件指向 tmp —— 不管这条测试要不要 `ws`。

    `ws` 里已经有这三行了,但 **`ws` 是按需申请的**:一条不写 `ws` 参数的测试什么保护都
    没有。`test_a_tool_that_does_not_exist_never_reaches_the_permission_prompt` 和
    `test_a_subagent_hitting_the_step_cap_does_not_cancel_the_parents_reflection` 就是
    这样 —— 它们只要 `monkeypatch`,却顺着 `agent` 的循环间接调到了 `recall.recall()`。
    于是**跑一次 pytest 往真实的 `.talos/recall_trace.jsonl` 追加 3 行**,四周下来 949 行,
    占了整份轨迹的 76%。我拿这份轨迹算过检索效果并且发布了结论,那份结论因此是错的。

    `ws` 的注释当时把病因写成「枚举永远落后一步」,补法是往清单里再加一条(`CACHE_TRACE`)。
    **诊断偏了半步**:这次漏的 `TRACE_FILE` 早就在清单里了,漏的是**另一条进入路径** ——
    同一条不变式两条路,只守了「申请了 ws」那条。JUDGING 第六节:立完一道闸,先问这片面
    还有几条路。所以这道闸挂在 autouse 上,和 `_keys_stay_put` 一样,不给测试选择的余地。

    判据:tests/test_memory.py::test_no_fixture_is_needed_to_keep_tests_off_the_real_talos"""
    import agent
    import recall
    import session
    d = str(tmp_path)
    monkeypatch.setattr(recall, "TRACE_FILE", os.path.join(d, "recall_trace.jsonl"))
    monkeypatch.setattr(recall, "HITS_FILE", os.path.join(d, "hits.json"))
    monkeypatch.setattr(agent, "CACHE_TRACE", os.path.join(d, "cache_trace.jsonl"))
    # **第三条路,同一天补的**:`once()` 原来一个会话都不开,所以 SESS_DIR 只在 `ws` 里
    # 挡着就够了。现在它每跑一次写一个会话文件 —— 一条不要 `ws` 却调到 `once()` 的测试
    # 就会往真实的 `.talos/sessions/` 里塞垃圾,而那批文件正是往事检索和燃尽表的语料。
    # 上面那段说「枚举永远落后一步」,这就是下一步:**加功能等于给这片面新开一条路。**
    monkeypatch.setattr(session, "SESS_DIR", os.path.join(d, "sessions"))

@pytest.fixture(autouse=True)
def _keys_stay_put():
    """任何测试都不许把 `agent._KEYS` 弄脏,弄脏了当场点名是谁。

    `_scrub` 把 `_KEYS` 从「只有 make_client 读」变成**每一次工具调用都读**。它被写脏一次,
    后面每条测试的工具输出都跟着变形 —— 上一次是 test_loop 塞了个单字符假 key,红的却是
    三十条之外的 test_async_tool_is_awaited。症状离病灶那么远,靠读回溯基本读不出来。

    闸挂在**漏斗**上(每条测试都过 autouse),不是挂在某一条测试上:test_tools 里那两条
    早就自己 patch 了 _KEYS,于是「这片面已经有判据了」—— 而 test_loop 那条谁也没看。
    JUDGING 第六节说的就是这句话最容易骗到自己。

    只还原、不断言是不够的:那样脏了也没人知道,下一条测试可能正好依赖那份脏。"""
    import agent
    before = dict(agent._KEYS)
    yield
    after = dict(agent._KEYS)
    agent._KEYS.clear()
    agent._KEYS.update(before)          # 先还原,再报错,别把脏留给后面
    assert after == before, (
        "这条测试改了 agent._KEYS 又没还回去。它是全局的,而 _scrub 每次工具调用都读它,"
        "留下的假 key 会去抹后面所有测试的工具输出。改法:monkeypatch.setattr(A, '_KEYS', {}) 。")
