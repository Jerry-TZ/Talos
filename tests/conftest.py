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
    monkeypatch.setattr(session, "SESS_DIR", os.path.join(d, "sessions"))
    monkeypatch.setattr(recall, "MEMORY_FILE", os.path.join(d, "memory.md"))
    monkeypatch.setattr(recall, "SKILLS_DIR", os.path.join(d, "skills"))
    monkeypatch.setattr(recall, "SESS_DIR", os.path.join(d, "sessions"))
    monkeypatch.setattr(recall, "HITS_FILE", os.path.join(d, "hits.json"))
    monkeypatch.setattr(recall, "TRACE_FILE", os.path.join(d, "recall_trace.jsonl"))
    return os.path.realpath(d)
