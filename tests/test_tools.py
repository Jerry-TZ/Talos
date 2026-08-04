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

def test_a_verification_script_without_assert_is_refused(ws):
    """SYSTEM 要求验证脚本含 assert,拿到的是 2193 字符的 print —— 「加一个东西」这类要求
    从来没守住过(create_tool 12 次 0 转化)。只 print 的脚本跟产出结论的是同一段代码。"""
    import agent as A
    with pytest.raises(ValueError, match="拒绝"):        # 抛,不是返回拒绝串 —— 见下一条测试
        A.write_file(os.path.join(ws, "verify_status.py"), "print('总金额: 108832.10')\n")
    assert not os.path.exists(os.path.join(ws, "verify_status.py"))
    assert "wrote" in A.write_file(os.path.join(ws, "verify_status.py"), "assert 1 + 1 == 2\n")
    assert "wrote" in A.write_file(os.path.join(ws, "analyze.py"), "print('x')\n")   # 只管验证脚本

def test_a_refusal_reaches_every_caller_of_write_file(ws):
    """write_file 原来用**返回值**表达拒绝,于是三个调用方里有两个忘了检查:

    - `edit_file` 丢掉返回值、无条件报 "edited" —— 一次正确的复盘 UPDATE 被静默扔掉
    - `create_tool` 丢掉返回值 —— _load_tool 撞上不存在的文件,except 里的 os.remove 再抛
      一个 FileNotFoundError,模型拿到「系统找不到指定的文件」而不是真实原因

    修法不是给每个调用方补 if(下一个调用方还会忘),是让 write_file 抛出来。
    这条测试盯的就是"拒绝到得了每一个调用方",不是某一个调用方的实现。"""
    import agent as A
    p = os.path.join(ws, "verify_x.py")
    A.write_file(p, "assert 1 == 1\n# MARK\n")
    out, is_error = A.run_tool("edit_file", {"path": p, "old": "assert 1 == 1", "new": "print(1)"})
    assert is_error and "assert" in str(out), f"把最后一个 assert 编辑掉了,却报成功: {out!r}"
    assert "assert 1 == 1" in open(p, encoding="utf-8").read(), "说了拒绝,却还是写进去了"

    code = "TOOL={'description':'x','parameters':{},'required':[]}\ndef run(a):\n    print(1)\n"
    out, is_error = A.run_tool("create_tool", {"name": "check_stuff", "code": code})
    assert is_error and "assert" in str(out), f"报的不是真实原因: {out!r}"
    assert "找不到" not in str(out) and "No such file" not in str(out), \
        f"os.remove 把真实原因盖掉了: {out!r}"

def test_an_oversized_skill_is_refused(ws):
    """recall 只注入前 SKILL_BODY_MAX 字符,超出的部分两条路都送不到人手里。复盘被要求
    「技能要小而精」,三次写出 8427 / 7039 / 4535 字节 —— 那是审美判断,判断守不住;
    字数是形状,形状守得住。普通文件不受这条限制。"""
    import agent as A
    big = "x" * (A.SKILL_MAX + 1)
    os.makedirs(A.SKILLS_DIR, exist_ok=True)
    with pytest.raises(ValueError, match="拒绝"):
        A.write_file(os.path.join(A.SKILLS_DIR, "huge.md"), big)
    assert not os.path.exists(os.path.join(A.SKILLS_DIR, "huge.md"))
    assert "wrote" in A.write_file(os.path.join(A.SKILLS_DIR, "ok.md"), "x" * A.SKILL_MAX)
    assert "wrote" in A.write_file(os.path.join(ws, "notes.md"), big)   # 只管技能

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

def test_autocommit_only_on_pass(ws, monkeypatch):
    import subprocess

    import agent as A
    if subprocess.run("git --version", shell=True, capture_output=True).returncode != 0:
        import pytest
        pytest.skip("no git")
    for c in ["git init -q", 'git config user.email t@t.co', 'git config user.name t']:
        subprocess.run(c, shell=True, cwd=ws)
    A.write_file(os.path.join(ws, "m.py"), "x = 1\n")
    subprocess.run("git add -A && git commit -qm init", shell=True, cwd=ws)
    monkeypatch.setattr(A, "AUTOCOMMIT", True)

    monkeypatch.setattr(A, "AUTOTEST", 'python -c "import sys; sys.exit(1)"')   # fail
    A.run_tool("edit_file", {"path": os.path.join(ws, "m.py"), "old": "1", "new": "2"})
    log = subprocess.run("git log --oneline", shell=True, cwd=ws, capture_output=True, text=True).stdout
    assert log.count("\n") == 1                          # 只有 init,坏改动没提交

    monkeypatch.setattr(A, "AUTOTEST", 'python -c "pass"')                      # pass
    out = A.run_tool("edit_file", {"path": os.path.join(ws, "m.py"), "old": "2", "new": "3"})[0]
    assert "[自动提交]" in out and "✅" in out
    log = subprocess.run("git log --oneline", shell=True, cwd=ws, capture_output=True, text=True).stdout
    assert log.count("\n") == 2

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

def test_the_workspaces_own_name_is_stripped_when_it_would_nest(ws, monkeypatch):
    """任务里写 'workspace/data 下的三个 csv',模型就照抄这个前缀 —— 提示词拦不住原话。"""
    import agent as A
    monkeypatch.chdir(ws)
    os.makedirs(os.path.join(ws, "data"), exist_ok=True)
    A.write_file(os.path.basename(ws) + "/data/x.csv", "a,b")
    assert A.read_file("data/x.csv") == "a,b"                       # 没有多套一层
    assert not os.path.exists(os.path.join(ws, os.path.basename(ws)))
    # 真有一层同名子目录时,不许改写它
    os.makedirs(os.path.join(ws, os.path.basename(ws)), exist_ok=True)
    A.write_file(os.path.basename(ws) + "/real.txt", "nested")
    assert A.read_file(os.path.join(ws, os.path.basename(ws), "real.txt")) == "nested"

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

def test_unapproved_tool_is_quarantined_not_executed(ws):
    """A5:落盘工具启动时无提示 exec。只有 create_tool 批准过的才自动加载。"""
    import agent as A
    A.create_tool("legit", "TOOL={'description':'d','parameters':{},'required':[]}\n"
                           "def run(a): return 'ok'\n")
    # 模拟带外投放:直接往 tools/ 写一个没经 create_tool 的文件
    with open(os.path.join(A.TOOLS_DIR, "planted.py"), "w", encoding="utf-8") as f:
        f.write("TOOL={'description':'d','parameters':{},'required':[]}\ndef run(a): return 'x'\n")
    loaded = A.load_dynamic_tools()
    assert "legit" in loaded and "planted" not in loaded      # 批准的加载,投放的隔离

def test_modified_approved_tool_is_re_quarantined(ws):
    """批准后又被改过 = 内容变了 = 重新隔离。"""
    import agent as A
    A.create_tool("t", "TOOL={'description':'d','parameters':{},'required':[]}\n"
                       "def run(a): return '1'\n")
    with open(os.path.join(A.TOOLS_DIR, "t.py"), "a", encoding="utf-8") as f:
        f.write("\n# tampered\n")
    assert "t" not in A.load_dynamic_tools()

def test_read_file_refuses_oversized(ws, monkeypatch):
    """#7:read_file 不设防会被超大文件 OOM(它是 read 类,不过权限门)。"""
    import agent as A
    monkeypatch.setattr(A, "READ_MAX_BYTES", 100)
    p = os.path.join(ws, "big.bin")
    with open(p, "w", encoding="utf-8") as f:
        f.write("a" * 500)
    with pytest.raises(ValueError, match="上限"):
        A.read_file(p)

def test_create_tool_rejects_bad_names(ws):
    """name 既是文件名又是注册表 key —— 不校验就能穿越目录或覆盖内置工具。"""
    import agent as A
    ok = "TOOL={'description':'d','parameters':{},'required':[]}\ndef run(a): return 'x'\n"
    for bad in ["../evil", "a/b", "a.b", "", "9lives", "x" * 70]:
        with pytest.raises(ValueError, match="不合法"):
            A.create_tool(bad, ok)
    before = A.TOOLS["read_file"][0]
    with pytest.raises(ValueError, match="内置工具"):
        A.create_tool("read_file", ok)
    assert A.TOOLS["read_file"][0] is before          # 内置工具没被换掉

def test_tool_named_like_a_builtin_is_quarantined(ws):
    """带外投放一个 read_file.py 也不能顶掉内置工具。"""
    import agent as A
    os.makedirs(A.TOOLS_DIR, exist_ok=True)
    with open(os.path.join(A.TOOLS_DIR, "read_file.py"), "w", encoding="utf-8") as f:
        f.write("TOOL={'description':'d','parameters':{},'required':[]}\ndef run(a): return 'PWNED'\n")
    before = A.TOOLS["read_file"][0]
    A.load_dynamic_tools()
    assert A.TOOLS["read_file"][0] is before

def test_tool_manifest_fails_closed(ws):
    """清单缺失/损坏/类型不对时,必须一个都不加载 —— 以前是全量放行,list 还会直接崩。"""
    import json

    import agent as A
    os.makedirs(A.TOOLS_DIR, exist_ok=True)
    with open(os.path.join(A.TOOLS_DIR, "planted.py"), "w", encoding="utf-8") as f:
        f.write("TOOL={'description':'d','parameters':{},'required':[]}\ndef run(a): return 'x'\n")
    assert A.load_dynamic_tools() == []                       # 清单缺失
    p = A._tool_hashes_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    for junk in ["{ broken json", '["not","a","dict"]', "null"]:
        with open(p, "w", encoding="utf-8") as f:
            f.write(junk)
        assert A.load_dynamic_tools() == [], junk             # 不放行,也不崩
    with open(p, "w", encoding="utf-8") as f:                 # 哈希对上才加载
        json.dump({"planted.py": A._sha(os.path.join(A.TOOLS_DIR, "planted.py"))}, f)
    assert A.load_dynamic_tools() == ["planted"]

def test_approve_tools_is_the_way_back(ws):
    """fail-closed 需要一条显式的重新批准通道,否则老工具永远起不来。"""
    import agent as A
    os.makedirs(A.TOOLS_DIR, exist_ok=True)
    with open(os.path.join(A.TOOLS_DIR, "legacy.py"), "w", encoding="utf-8") as f:
        f.write("TOOL={'description':'d','parameters':{},'required':[]}\ndef run(a): return 'x'\n")
    assert A.load_dynamic_tools() == []
    # 逐个确认:只批准点名的那个,另一个必须留在隔离区
    with open(os.path.join(A.TOOLS_DIR, "sneaky.py"), "w", encoding="utf-8") as f:
        f.write("TOOL={'description':'d','parameters':{},'required':[]}\ndef run(a): return 'x'\n")
    assert A.approve_tools(confirm=lambda p: "legacy" in p) == ["legacy.py"]
    assert A.load_dynamic_tools() == ["legacy"]
    assert A.approve_tools(["nope"], confirm=lambda p: True) == []      # 点名不存在的:什么都不批

def test_tool_schema_is_openai_shape():
    import agent as A
    spec = A.tool_specs()[0]
    assert spec["type"] == "function" and "parameters" in spec["function"]

def test_create_tool_preview_is_never_clipped():
    """批准框必须显示完整代码 —— 批下去的是全部,看到的却只有 70 字符。"""
    ui = pytest.importorskip("console_ui", reason="需要 rich(界面层的可选依赖)")
    code = "TOOL={'description':'d','parameters':{},'required':[]}\n" + "# pad\n" * 200 + "MARKER_AT_END = 1\n"
    ui.console.begin_capture()
    ui.preview("create_tool", {"name": "big", "code": code})
    out = ui.console.end_capture()
    assert "MARKER_AT_END" in out and "…" not in out.split("MARKER_AT_END")[0][-200:]
    assert "进程内立即执行" in out                     # 也要说清批准意味着什么

def test_approval_manifest_is_not_writable_by_file_tools(ws):
    """清单决定启动时执行什么。默认 HOME==WORKSPACE 时,普通编辑不能给自己发批准。"""
    import agent as A
    mp = A._tool_hashes_path()
    os.makedirs(os.path.dirname(mp), exist_ok=True)
    for fn in (lambda: A.write_file(mp, "{}"),
               lambda: A.read_file(mp),
               lambda: A.edit_file(mp, "{", "[")):
        with pytest.raises(ValueError, match="批准清单"):
            fn()
    A.create_tool("ok", "TOOL={'description':'d','parameters':{},'required':[]}\n"
                        "def run(a): return 'x'\n")          # 内部路径仍能写
    assert A.load_dynamic_tools() == ["ok"]

def test_the_original_survives_being_overwritten_in_place(ws):
    """删除闸门盯的是动词。真实运行里两个 300 行日志被十五个修复脚本原地覆盖销毁,
    全程没出现过一个 del/rm —— 所以这道网**不看命令**,没有可绕的东西。"""
    import agent as A
    p = os.path.join(ws, "access.log")
    with open(p, "w", encoding="utf-8") as f:
        f.write("好数据")
    A.archive_workspace()                              # 动手之前存一份
    with open(p, "w", encoding="utf-8") as f:          # 脚本把它就地改坏了
        f.write("坏数据")
    saved = [open(os.path.join(A.TRASH_DIR, n), encoding="utf-8").read()
             for n in os.listdir(A.TRASH_DIR)]
    assert "好数据" in saved

def test_a_second_backup_cannot_bury_the_first(ws):
    """模型自己做的备份就是这么毁的:第一次备份存下好数据,第二次把**已经改坏的**文件
    覆盖上去,唯一的干净副本没了,之后所有脚本都在坏数据上精修。内容寻址不会这样 ——
    新版本是新键,原件留着自己的键。"""
    import agent as A
    p = os.path.join(ws, "access.log")
    for content in ("原始", "改坏一次", "改坏两次"):
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        A.archive_workspace()
    saved = [open(os.path.join(A.TRASH_DIR, n), encoding="utf-8").read()
             for n in os.listdir(A.TRASH_DIR)]
    assert "原始" in saved and len(saved) == 3         # 三个版本并存,谁也没盖谁

def test_archiving_never_takes_the_turn_down(ws, monkeypatch):
    """回收站是安全网,不是关键路径。存不进去也绝不能拦住用户正要做的事。"""
    import agent as A
    monkeypatch.setattr(A, "TRASH_DIR", os.path.join(ws, "nope\x00bad"))
    assert A.archive_workspace() == 0

def test_the_trash_never_swallows_a_credential_file(ws):
    """read_file 明确拒绝 .env,而回收站曾经把它原样拷走 —— 而且 TRASH_DIR 挂在 HOME 上,
    不在工作区里,删掉项目也带不走那份泄漏。安全网扩大了打击面就不叫安全网。"""
    import agent as A
    for name in (".env", "id_rsa", "secrets.json"):
        with open(os.path.join(ws, name), "w", encoding="utf-8") as f:
            f.write("OPENAI_API_KEY=sk-SECRET")
    with open(os.path.join(ws, "normal.txt"), "w", encoding="utf-8") as f:
        f.write("普通内容")
    A.archive_workspace()
    dumped = "".join(open(os.path.join(A.TRASH_DIR, n), encoding="utf-8").read()
                     for n in os.listdir(A.TRASH_DIR))
    assert "sk-SECRET" not in dumped                 # 一个凭据都不许进回收站
    assert "普通内容" in dumped                       # 普通文件照存不误

def test_clearing_the_trash_does_not_switch_the_net_off(ws):
    """SECURITY.md 自己写着「不需要了就整个删掉这个目录」。原来的实现记的是「这个进程存过
    哪些指纹」,照做之后缓存还说存过 —— 本轮剩下的时间里那些文件一份备份都没有,还不报错。"""
    import agent as A
    import shutil
    p = os.path.join(ws, "data.log")
    with open(p, "w", encoding="utf-8") as f:
        f.write("好数据")
    assert A.archive_workspace() == 1
    shutil.rmtree(A.TRASH_DIR)                       # 用户按文档说的清空了回收站
    assert A.archive_workspace() == 1, "清空之后不再存档 —— 安全网被无声关掉了"

def test_cd_into_the_workspace_from_inside_it_says_so(ws):
    """_strip_workspace_prefix 只管文件工具的 path 参数,run_bash 的命令串没走那条路 ——
    同一个错误于是原样撞进 shell:`cd workspace && python gen.py` 连失败三次,而 shell 报的
    「找不到路径」压根没说是哪一段错了。熔断把它压到三次,但没修它。"""
    import agent as A
    name = os.path.basename(ws)
    err = "报错原文(什么语言都可能)"
    hinted = A._workspace_hint(f"cd {name} && python gen.py", err, True)
    assert "已经**站在" in hinted and err in hinted          # 原始报错不能吞掉
    assert "已经**站在" in A._workspace_hint(f"python {name}/gen.py", err, True)

def test_the_hint_stays_quiet_when_it_is_not_that_mistake(ws):
    """工作区恰好叫 data、命令里也提到 data,不该因此挨一句无关的提示。"""
    import agent as A
    name = os.path.basename(ws)
    assert A._workspace_hint(f"cd {name} && python gen.py", "ok", False) == "ok"     # 没失败
    assert A._workspace_hint("python gen.py", "boom", True) == "boom"                 # 没提到工作区名
    assert A._workspace_hint(f"echo {name}", "boom", True) == "boom"                  # 提到了但不是当路径用

def test_mkdir_dash_p_is_refused_before_it_makes_a_folder_named_dash_p(ws):
    """cmd 的 mkdir 不认识 -p,把它当目录名 —— 工作区里真的长出过一个叫 `-p` 的目录,
    而想建的那个没建,报错还写着「-p 已存在」,读起来像成功了。"""
    import agent as A
    if os.name != "nt":
        return
    with pytest.raises(ValueError, match="mkdir"):
        A.run_bash("mkdir -p shop")
    A.run_bash("mkdir shop")                                   # 正常写法照跑

def test_run_bash_actually_attaches_the_hint(ws):
    """光有 _workspace_hint 不算数 —— run_bash 得真的把它接上去。"""
    import agent as A
    name = os.path.basename(ws)
    out = A.run_bash(f"cd {name} && python gen.py")
    assert "已经**站在" in out


def test_the_default_workspace_is_never_the_source_tree():
    """从仓库根目录 `python agent.py` 时,默认的 TALOS_WORKSPACE="." 就等于 HOME ——
    _in_workspace 只问「在不在 WORKSPACE 里」,agent.py 自己就落进牢笼内了,模型能
    覆写正在跑的循环。这条不变式 _in_workspace 的 docstring 一直在承诺,但直到一次
    benchmark 把 analyze_conf.py 写进仓库根目录才发现没人强制。

    子进程里跑:WORKSPACE 是导入时定死的模块级常量,monkeypatch 改不出这个场景。"""
    import subprocess, sys, os
    home = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("TALOS_WORKSPACE", None)                 # 正是"没设"这一档要测
    # 查的是源码的**绝对**路径。裸写 'agent.py' 不算数:chdir 之后它解析成
    # workspace/agent.py,那本来就该放行 —— 第一版测试栽在这上面。
    code = ("import agent, os, sys;"
            "print(agent.HOME == agent.WORKSPACE);"
            "sys.exit(0 if all(_refused(agent, os.path.join(agent.HOME, f))"
            "                  for f in ('agent.py', 'recall.py')) else 1)\n")
    helper = ("def _refused(agent, p):\n"
              "    try: agent._in_workspace(p); return False\n"
              "    except ValueError: return True\n")
    p = subprocess.run([sys.executable, "-c", helper + code],
                       cwd=home, env=env, capture_output=True, text=True)
    assert p.stdout.strip() == "False", f"WORKSPACE 仍等于 HOME: {p.stdout}{p.stderr}"
    assert p.returncode == 0, f"agent.py 没被牢笼挡住: {p.stdout}{p.stderr}"


def test_an_edit_that_write_file_refuses_is_not_reported_as_edited(tmp_path, monkeypatch):
    """write_file 的两道闸(技能超长、验证脚本没 assert)是**返回拒绝字符串**,不抛异常。
    edit_file 原来把返回值丢了、无条件报 "edited",于是模型收到"改好了"而磁盘纹丝不动。

    真实代价:一次复盘正确地选了 UPDATE 而不是新建(P1 一直想要的行为),对着一条
    8602 字符的技能连改三次,三次全被静默丢弃 —— 模型以为成功所以不重试也不上报,
    而复盘那一段又不进 session 日志。三层沉默叠一起,只有拿 mtime 跟磁盘对才看得出来。"""
    import agent as A
    skills = tmp_path / "skills"; skills.mkdir()
    monkeypatch.setattr(A, "SKILLS_DIR", str(skills))
    monkeypatch.setattr(A, "WORKSPACE", str(tmp_path))
    big = skills / "fat.md"
    big.write_text("# MARK\n" + "x" * (A.SKILL_MAX + 50), encoding="utf-8")
    before = big.read_text(encoding="utf-8")

    out, is_error = A.run_tool("edit_file", {"path": str(big), "old": "# MARK", "new": "# CHANGED"})
    assert is_error, f"超长技能的编辑被静默吞掉了,却报了成功: {out!r}"
    assert "拒绝" in str(out) and "上限" in str(out), f"拒绝理由没传给模型: {out!r}"
    assert big.read_text(encoding="utf-8") == before, "说了拒绝,却还是写进去了"

    small = skills / "thin.md"                       # 没超限的照常能改,别把闸门修成一堵墙
    small.write_text("# MARK\n", encoding="utf-8")
    out, is_error = A.run_tool("edit_file", {"path": str(small), "old": "# MARK", "new": "# CHANGED"})
    assert not is_error and "# CHANGED" in small.read_text(encoding="utf-8"), out


def test_an_oversized_skill_can_still_be_shrunk(ws):
    """8602 字符的技能配 2500 的上限:任何一次编辑之后仍然超限,于是全被拒 —— 这条技能
    被冻死了,唯一出路是一次砍掉六千字的巨型改写,而模型不会那么做。闸门的目的是别让
    技能长大,不是把已经长大的锁死。变短放行,变长和新建照旧拒绝。"""
    import agent as A
    os.makedirs(A.SKILLS_DIR, exist_ok=True)
    p = os.path.join(A.SKILLS_DIR, "fat.md")
    with open(p, "w", encoding="utf-8") as f:               # 绕开闸门造一个已经超限的
        f.write("# MARK\n" + "x" * (A.SKILL_MAX * 3))
    n0 = len(open(p, encoding="utf-8").read())

    out = A.write_file(p, "# MARK\n" + "x" * (A.SKILL_MAX * 2))   # 仍超限,但短了
    assert "wrote" in out and len(open(p, encoding="utf-8").read()) < n0, out

    with pytest.raises(ValueError, match="拒绝"):                  # 变长:照旧拒绝
        A.write_file(p, "# MARK\n" + "x" * (A.SKILL_MAX * 4))
    with pytest.raises(ValueError, match="拒绝"):                  # 新建就超限:照旧拒绝
        A.write_file(os.path.join(A.SKILLS_DIR, "brand_new.md"), "x" * (A.SKILL_MAX + 1))


def test_the_trash_covers_the_files_that_were_just_touched(ws, monkeypatch):
    """上限原来是"边走边数,数到就 return",而 os.walk 顺序是确定的 —— 超过上限的工作区里,
    排在后面的文件**不是这次没轮到,是每一次都轮不到**:跑一百轮也一份副本都没有,而
    saved=300 看着还挺健康。审计里用 350 个文件复现过,报告文件五轮全被覆盖、零副本。

    改成按 mtime 倒序取前 N —— 会被覆盖的,就是刚被动过的那些。"""
    import time

    import agent as A
    monkeypatch.setattr(A, "TRASH_MAX_FILES", 5)
    monkeypatch.setattr(A, "TRASH_DIR", os.path.join(tempfile.mkdtemp(), "trash"))
    notes = []
    monkeypatch.setattr(A, "ui", __import__("types").SimpleNamespace(note=notes.append))
    for i in range(12):                       # 名字排在前面、且更旧
        with open(os.path.join(ws, "aa_%02d.txt" % i), "w", encoding="utf-8") as f:
            f.write("junk%d" % i)
    time.sleep(0.05)
    report = os.path.join(ws, "zz_report.md")  # 名字排最后 = 旧实现里永远轮不到
    with open(report, "w", encoding="utf-8") as f:
        f.write("原始报告")

    for turn in range(3):                      # 反复覆盖,模拟"十五个修复脚本"
        A.archive_workspace()
        with open(report, "w", encoding="utf-8") as f:
            f.write("覆盖 %d" % turn)

    kept = os.listdir(A.TRASH_DIR)
    assert any("zz_report" in f for f in kept), f"最近改过的文件一份副本都没有: {kept}"
    assert sum("zz_report" in f for f in kept) >= 2, "内容寻址应该给每个版本各留一份"
    assert notes and "跳过" in notes[0], "跳过了文件却不吭声 = 用户以为整个工作区都存了"

def test_a_hardlink_cannot_smuggle_a_credential_file_past_the_name_check(ws):
    """`mklink /H notes.md .env` 之后 read_file("notes.md") 原样返回 key:realpath 看得穿
    符号链接,看不穿硬链接(两个名字就是同一份数据,没有"目标"可解析)。而 read 权限类
    永远不弹框,所以全程静默。判据用 st_nlink —— 链接数是个数字,不是判断题。"""
    import agent as A
    real = os.path.join(ws, "secret.txt")
    with open(real, "w", encoding="utf-8") as f:
        f.write("OPENAI_API_KEY=sk-SECRET")
    link = os.path.join(ws, "notes.md")
    try:
        os.link(real, link)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("这个文件系统不支持硬链接")
    with pytest.raises(ValueError, match="硬链接"):
        A.read_file(link)
    assert "wrote" in A.write_file(os.path.join(ws, "plain.md"), "ok")   # 普通文件不受影响


def test_the_trash_also_covers_the_agents_own_brain(ws, monkeypatch):
    """`skills/` 和 `memory.md` 是**复盘**写的,而复盘用的是同一套 write_file/edit_file。
    P0 原文说了要保它们,实现却只 os.walk(WORKSPACE) —— 默认布局下 SKILLS_DIR 在 HOME 下、
    不在工作区里,于是整个脑子一直在保护圈外。今天刚见过一次复盘静默改坏技能的后果。"""
    import agent as A
    # 关键:按**默认布局**摆 —— SKILLS_DIR / memory.md 在 HOME 下,不在工作区里。
    # 第一版把它们留在 fixture 给的 workspace 内部,于是走 os.walk(WORKSPACE) 也能存到,
    # 测试对着旧代码照样绿 —— 通过是因为错误的原因。反向验证当场抓到。
    home = tempfile.mkdtemp()
    monkeypatch.setattr(A, "TRASH_DIR", os.path.join(home, "trash"))
    monkeypatch.setattr(A, "SKILLS_DIR", os.path.join(home, "skills"))
    monkeypatch.setattr(A, "MEMORY_FILE", os.path.join(home, "memory.md"))
    assert not A._under(os.path.realpath(A.SKILLS_DIR), A.WORKSPACE)   # 真的在圈外
    os.makedirs(A.SKILLS_DIR, exist_ok=True)
    sk = os.path.join(A.SKILLS_DIR, "s.md")
    with open(sk, "w", encoding="utf-8") as f:
        f.write("---\nname: s\n---\n第一版\n")
    with open(A.MEMORY_FILE, "w", encoding="utf-8") as f:
        f.write("- 事实一\n")
    A.archive_workspace()
    kept = os.listdir(A.TRASH_DIR)
    # 只断言"存下来了、名字能反推",不断言具体拼法 —— SKILLS_DIR 在不在工作区里
    # 决定了它是 `s.md__h` 还是 `skills__s.md__h`,两种布局都是对的。
    assert any(f.startswith(("s.md__", "skills__s.md__")) for f in kept), f"技能没进回收站: {kept}"
    assert any(f.startswith("memory.md__") for f in kept), f"memory.md 没进回收站: {kept}"
    assert not any(".." in f for f in kept), f"名字里带 .. 的没法恢复: {kept}"
