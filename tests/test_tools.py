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

def test_edit_matches_across_line_endings(ws):
    """模型永远输出 \\n;文件里可能是 \\r\\n。不该因此说"找不到"。"""
    import agent as A
    p = os.path.join(ws, "crlf.md")
    with open(p, "wb") as f:
        f.write("# 标题\r\n- 总字数: 0\r\n".encode("utf-8"))
    A.edit_file(p, "- 总字数: 0", "- 总字数: 265")
    assert "265" in A._read_full(p) and "\r\n" in A._read_full(p)   # 原有换行风格保住

def test_script_write_nudges_toward_create_tool(ws):
    """在写脚本的当下提醒 —— SYSTEM 里说了两轮它都没听。"""
    import agent as A
    assert "create_tool" in A.write_file(os.path.join(ws, "analyze.py"), "print(1)")
    assert "create_tool" not in A.write_file(os.path.join(ws, "notes.md"), "hi")   # 只针对脚本
    os.makedirs(A.TOOLS_DIR, exist_ok=True)
    assert "create_tool" not in A.write_file(os.path.join(A.TOOLS_DIR, "t.py"), "x")  # 工具本身不提醒

def test_autotest_reports_a_break_on_the_edit_that_caused_it(ws, monkeypatch):
    import agent as A
    monkeypatch.setattr(A, "AUTOTEST", "")                       # 默认关闭
    A.write_file(os.path.join(ws, "m.py"), "x = 1\n")
    assert "自动测试" not in A.run_tool("write_file", {"path": os.path.join(ws, "m.py"),
                                                       "content": "x = 2\n"})[0]
    monkeypatch.setattr(A, "AUTOTEST", "python -c \"import sys; sys.exit(1)\"")
    out, err = A.run_tool("edit_file", {"path": os.path.join(ws, "m.py"), "old": "2", "new": "3"})
    assert not err and "❌" in out and "退出码 1" in out         # 编辑成功,但测试失败被贴出来
    monkeypatch.setattr(A, "AUTOTEST", "python -c \"pass\"")
    assert "✅" in A.run_tool("edit_file", {"path": os.path.join(ws, "m.py"),
                                            "old": "3", "new": "4"})[0]

def test_write_file_keeps_lf(ws):
    import agent as A
    p = os.path.join(ws, "lf.md")
    A.write_file(p, "a\nb\n")
    assert open(p, "rb").read() == b"a\nb\n"      # 不被 Windows 翻译成 \r\n

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

def test_bash_uses_the_venv_not_the_system_python(ws):
    """`pip install` 必须落在 venv 里 —— 否则 agent 会把宿主机 Python 装脏。"""
    import sys

    import agent as A
    assert A._VENV_ENV["PATH"].startswith(os.path.dirname(os.path.abspath(sys.executable)))
    assert A._VENV_ENV["PIP_REQUIRE_VIRTUALENV"] == "1"        # 兜底:不在 venv 里 pip 直接拒绝
    out, err = A.run_tool("run_bash", {"command": 'python -c "import sys; print(sys.prefix)"'})
    assert not err and os.path.realpath(out.strip()) == os.path.realpath(sys.prefix)

@pytest.mark.skipif(os.name != "nt", reason="cmd.exe 特有")
def test_bashisms_refused_with_the_cmd_equivalent(ws):
    import agent as A
    for c in ["notes=$(ls *.md | wc -l)", "ls -la", "cat x.py", "grep -n foo x", "pwd",
              "dir | grep foo", "source .venv/bin/activate"]:
        out, err = A.run_tool("run_bash", {"command": c})
        assert err and "cmd.exe" in out, c
    for c in ["dir notes", "python head.py", "del probe.py", "findstr /n foo x",
              "type nul > a.txt", "mkdir notes"]:            # 别误伤正常命令
        _out, err = A.run_tool("run_bash", {"command": c})
        assert not err, c

@pytest.mark.skipif(os.name != "nt", reason="cmd.exe 特有")
def test_multiline_bash_refused_loudly(ws):
    """cmd 只跑第一行、exit 0、无输出 —— 静默成功最坑,必须报错。"""
    import agent as A
    out, err = A.run_tool("run_bash", {"command": 'python -c "\nprint(1)\n"'})
    assert err and "第一行" in out

def test_bad_calls_get_actionable_errors(ws):
    """模型偶尔会吐畸形调用 —— 报错要说清正确形状,否则它只能换个姿势再猜。"""
    import agent as A
    out, err = A.run_tool("run_bash", {})                    # 少必填参数
    assert err and "command" in out and "必填" in out
    out, err = A.run_tool("run_bash\n<arg_value>pwd</arg_value>", {})   # 参数塞进了工具名
    assert err and "不能写进名字" in out

def test_tool_schema_unwraps_a_full_json_schema(ws):
    """模型常把整个 JSON Schema 塞进 parameters —— 再包一层就成了畸形,严格的服务端直接 400。"""
    import agent as A
    A.create_tool("nested", "TOOL={'description':'d','parameters':"
                            "{'type':'object','properties':{'n':{'type':'integer'}},'required':['n']}}\n"
                            "def run(a): return str(a['n'])\n")
    spec = next(s for s in A.tool_specs() if s["function"]["name"] == "nested")
    p = spec["function"]["parameters"]
    assert p["properties"] == {"n": {"type": "integer"}} and p["required"] == ["n"]

def test_tool_schema_is_openai_shape():
    import agent as A
    spec = A.tool_specs()[0]
    assert spec["type"] == "function" and "parameters" in spec["function"]
