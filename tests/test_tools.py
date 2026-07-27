"""工具 + 工作目录限制 + 省token(截断/分页)+ 自造工具。"""
import os
import tempfile

import pytest

def test_read_write_edit(ws):
    import agent as A
    p = os.path.join(ws, "t.txt")
    assert "wrote" in A.write_file(p, "hello world")
    assert A.read_file(p) == "hello world"
    assert A.edit_file(p, "world", "talos").startswith("edited")
    assert A._read_full(p) == "hello talos"

def test_edit_requires_unique(ws):
    import agent as A
    p = os.path.join(ws, "t.txt")
    A.write_file(p, "aa")
    with pytest.raises(ValueError):
        A.edit_file(p, "a", "b")        # 不唯一
    with pytest.raises(ValueError):
        A.edit_file(p, "zzz", "b")      # 找不到

def test_read_truncation_and_paging(ws):
    import agent as A
    p = os.path.join(ws, "big.txt")
    A.write_file(p, "".join("line%d\n" % i for i in range(300)))
    r = A.read_file(p)
    assert r.count("\n") <= A.READ_MAX_LINES + 2 and "共 300 行" in r
    r2 = A.read_file(p, offset=100, limit=5)
    assert "line100" in r2 and "line104" in r2 and "line105\n" not in r2

def test_edit_uses_full_read(ws):
    import agent as A
    p = os.path.join(ws, "big.txt")
    A.write_file(p, "".join("line%d\n" % i for i in range(300)))
    A.edit_file(p, "line299", "LAST")   # 在 250 行上限之外
    assert "LAST" in A._read_full(p)

def test_run_bash_truncation(ws, monkeypatch):
    import agent as A
    monkeypatch.setattr(A, "BASH_MAX_CHARS", 15)
    out = A.run_bash("echo hello world foo bar baz qux")
    assert "已截断" in out

def test_relative_paths_resolve_inside_workspace(ws, monkeypatch):
    """模型写 'notes/x.md' 指的是工作区里,不是进程碰巧站着的地方。"""
    import agent as A
    monkeypatch.chdir(ws)
    os.makedirs(os.path.join(ws, "notes"), exist_ok=True)
    A.write_file("notes/x.md", "hi")                      # 相对路径,不该越界
    assert os.path.exists(os.path.join(ws, "notes", "x.md"))
    assert A.read_file("notes/x.md") == "hi"

def test_workspace_jail(ws):
    import agent as A
    outside = os.path.join(tempfile.mkdtemp(), "evil.txt")
    with pytest.raises(ValueError):
        A.write_file(outside, "x")
    with pytest.raises(ValueError):
        A.read_file(outside)
    out, err = A.run_tool("read_file", {"path": outside})   # 经 run_tool 变成错误串,不崩
    assert err and "越界" in out

def test_create_tool_and_persist(ws):
    import agent as A
    A.create_tool("dbl", "TOOL={'description':'x2','parameters':{'n':{'type':'string'}},'required':['n']}\n"
                         "def run(a):\n    return str(int(a['n'])*2)\n")
    assert A.TOOLS["dbl"][0]({"n": "21"}) == "42"
    assert "dbl" in A.load_dynamic_tools()               # 重启后还能加载

def test_read_survives_windows_encodings(ws):
    """PowerShell 的 `>` 默认写 UTF-16LE,编辑器爱加 BOM —— 都不能让 agent 崩。"""
    import agent as A
    for name, raw in [("u16.md", "- 中文 line\n".encode("utf-16")),
                      ("bom.md", "﻿- 中文 line\n".encode("utf-8"))]:
        p = os.path.join(ws, name)
        with open(p, "wb") as f:
            f.write(raw)
        assert A._read_full(p).strip() == "- 中文 line"      # BOM 剥掉,内容不糊

def test_binary_file_is_refused_not_mangled(ws):
    """读二进制会返回乱码,模型会把乱码当内容 —— 必须报错并告诉它用什么库。"""
    import agent as A
    p = os.path.join(ws, "a.docx")
    with open(p, "wb") as f:
        f.write(b"PK\x03\x04\x00\x00binary\x00stuff")
    with pytest.raises(ValueError, match="二进制"):
        A._read_full(p)

def test_create_tool_failure_leaves_nothing_behind(ws):
    import agent as A
    with pytest.raises(ValueError, match="模块最外层"):        # 报错要能教会模型怎么改
        A.create_tool("broken", "def run(a):\n    return '1'\n")   # 缺 TOOL
    assert not os.path.exists(os.path.join(A.TOOLS_DIR, "broken.py"))
    assert "broken" not in A.load_dynamic_tools()               # 不会每次启动都加载失败

def test_async_tool_is_awaited(ws):
    """自造工具用 playwright 之类的 async 库时,不能把 coroutine 原样丢回模型。"""
    import agent as A
    A.create_tool("a_tool", "TOOL={'description':'x','parameters':{},'required':[]}\n"
                            "async def run(a):\n    return 'async ok'\n")
    assert A.run_tool("a_tool", {}) == ("async ok", False)

@pytest.mark.skipif(os.name != "nt", reason="cmd.exe 特有")
def test_multiline_bash_refused_loudly(ws):
    """cmd 只跑第一行、exit 0、无输出 —— 静默成功最坑,必须报错。"""
    import agent as A
    out, err = A.run_tool("run_bash", {"command": 'python -c "\nprint(1)\n"'})
    assert err and "第一行" in out

def test_tool_schema_is_openai_shape():
    import agent as A
    spec = A.tool_specs()[0]
    assert spec["type"] == "function" and "parameters" in spec["function"]
