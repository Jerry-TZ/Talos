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
