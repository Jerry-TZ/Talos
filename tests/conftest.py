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
