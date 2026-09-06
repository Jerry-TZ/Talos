"""工具 + 工作目录限制 + 省token(截断/分页)+ 自造工具。"""
import os
import subprocess
import sys
import tempfile

import pytest


def _encodable(s: str, enc: str) -> bool:
    try:
        s.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False

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

def test_a_missing_file_says_where_it_looked_and_where_the_real_one_is(ws, monkeypatch):
    """找不到文件时,报错要说清**在哪找的**;项目根有同名的就直接给路径。

    原来只抛一个 `FileNotFoundError`,模型只好 `dir` 一层层猜。冒烟实测:
    「用一句话总结 README.md」花了 **5 次调用 / 46,667 token**,四条任务里最贵的一条,
    而线索一直在手边 —— 读路径本来就额外放开了 HOME(`_in_workspace` 的 `for_read`)。
    **不是说错话,是说得不够**,而说得不够一样按步数收费。

    第三条是这条判据的重点,也是唯一一条摘掉之后会出安全问题的:提示**只说能证明的** ——
    「那边有」不够,还得「你真的读得到」。后半句是再走一遍同一道闸问出来的,不是假设。
    否则 `.env` 这类会被热心地指出来,而模型跟过去撞一堵墙 ——
    **一条指向你打不开的东西的提示,比不给提示更糟。**"""
    import agent as A
    monkeypatch.chdir(ws)
    home = os.path.realpath(tempfile.mkdtemp())
    monkeypatch.setattr(A, "HOME", home)

    # ① 哪儿都没有:说清楚在工作区下找的,而且**不许凭空指一个项目根的路径** ——
    #    指向一个也不存在的文件,比不指更浪费步数。
    with pytest.raises(ValueError) as e1:
        A.read_file("nope.md")
    assert ws in str(e1.value), f"没说在哪找的,模型只能猜:{e1.value}"
    assert home not in str(e1.value), f"项目根也没有,却指了过去:{e1.value}"

    # ② 项目根有一个读得到的同名文件 —— 直接把路径给它,省掉那几步 dir
    with open(os.path.join(home, "README.md"), "w", encoding="utf-8") as f:
        f.write("# hi")
    with pytest.raises(ValueError) as e2:
        A.read_file("README.md")
    assert home in str(e2.value) and "读得到" in str(e2.value), \
        f"项目根明明有,还是让模型自己去找:{e2.value}"

    # ③ 项目根有,但那边的闸不让读 —— 一个字都不许提。
    #    **样本用 credentials.md,不用 .env。** 第一版用的就是 .env,而 .env 在
    #    `_is_secret_path` 那道**总闸**上、进函数第一步就被拒了,压根走不到这段提示代码 ——
    #    断言于是**结构上不可能红**(JUDGING 里的形状③),而它当时是绿的。
    #    是变异测试逮到的:把闸摘掉、只凭「文件存在」就指路,那一版照样全绿。
    #    credentials.md 过得了总闸(`_SECRET_NAMES` 里是无扩展名的 `credentials`),
    #    只在 HOME 那条支路的词干闸上被拦 —— 这才真的走到这里。
    with open(os.path.join(home, "credentials.md"), "w", encoding="utf-8") as f:
        f.write("k=v")
    with pytest.raises(ValueError) as e3:
        A.read_file("credentials.md")
    assert home not in str(e3.value), \
        f"把一个读不到的凭据文件指给模型了:{e3.value}"

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

@pytest.mark.skipif(os.name != "nt", reason="POSIX 上到处都是 UTF-8,原生命令的输出编码不是问题")
def test_run_bash_reads_back_what_a_native_command_printed(ws):
    """`dir` / `git` / `findstr` 不是 Python —— 它们按**控制台代码页**输出,不是 UTF-8。

    `run_bash` 写死 `encoding="utf-8"`,而旁边那行注释("else a GBK console kills any
    child that prints 中文")想到的是 `PYTHONIOENCODING` 管得住的**Python 子进程**。
    原生命令不在其内:中文 Windows 上 `dir` 一个中文名文件,模型收到的是 `����.txt` ——
    **它拿不到那个文件名,后面每一步都跟着错**,而输出看着像正常返回。

    掩盖它的是 `talos.bat` 里的 `chcp 65001`:交互启动永远踩不到。而 `once()`
    (`-p`、EXAM、benchmark)不走那个 bat —— **无人值守那条路一直在拿乱码**。
    同一族坑第五十二节记过一次,那次是 Talos 往外写 stderr,这条是往里读子进程输出。

    判据不写死 cp936:runner 是英文 Windows(OEM 437),挑一个**这台机器编得出来**的词,
    别把本机语义烤进断言 —— 那个错误这个仓库今天刚犯过一次。"""
    import agent as A, locale
    oem = locale.getpreferredencoding(False)
    word = next((w for w in ("报告", "café", "naïve") if _encodable(w, oem)), None)
    assert word, f"这台机器的 {oem} 一个非 ASCII 词都编不出来,判据失效"
    with open(os.path.join(ws, "f.txt"), "wb") as f:
        f.write(word.encode(oem))                # 原生命令吐出来的就是这样的字节
    out, err = A.run_tool("run_bash", {"command": "type f.txt"})
    assert not err, out
    assert word in out, f"原生命令的输出解错码了:拿到 {out.strip()!r},要的是 {word!r}"


def test_editing_an_approved_tool_says_so_at_the_time(ws):
    """`create_tool` 造完、`edit_file` 改两版 —— 最普通的流程,而它把工具锁死。

    哈希只有 `create_tool` 和 `--approve-tools` 更新。`edit_file` 不更新,**也不能更新**:
    那等于模型自己给自己发批准,这把锁就没有了(`_in_workspace` 的注释写的就是这句)。
    于是改完当场没有任何动静,**下一次启动**才报「批准后被改过」,而那句话读起来像篡改。

    真事:`figcheck.py` 就是这么死的 —— 造完两分钟内自己 `edit_file` 了两次,
    然后每次启动报了一个月。上一次调查改的是措辞(把两种原因拆开写),
    **没往下问一句「那它到底是谁改的」** —— 答案就在会话记录里,一条 grep 的距离。

    能补的不是批准,是**在改的那一刻说一声**:模型此刻还在这个文件上,还接得住。"""
    import agent as A
    os.makedirs(A.TOOLS_DIR, exist_ok=True)
    p = os.path.join(A.TOOLS_DIR, "figcheck.py")
    with open(p, "w", encoding="utf-8") as f:
        f.write("def figcheck():\n    return 1\n")
    A._approve_tool(p)
    out, err = A.run_tool("edit_file", {"path": p, "old": "return 1", "new": "return 2"})
    assert not err, out
    assert "--approve-tools figcheck" in out, \
        "改完一个已批准的工具,当场什么都没说 —— 它下次启动就不执行了,而模型不知道"

    # **反面同样要钉住**:普通文件改一下不许报这句,否则每次编辑都在喊狼来了。
    q = os.path.join(ws, "note.py")
    with open(q, "w", encoding="utf-8") as f:
        f.write("x = 1\n")
    out2, err2 = A.run_tool("edit_file", {"path": q, "old": "x = 1", "new": "x = 2"})
    assert not err2 and "--approve-tools" not in out2, "普通文件被误报成工具"


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
    # **不是「带外投放」—— `write_file` 自己就够得着 TOOLS_DIR。** `_in_workspace` 明写
    # 放开「agent 的大脑」(skills/tools/memory),原来这儿用裸 open() 模拟外人投放,
    # 把真实路径说小了:模型按一次 `a` 会话放行 write_file 之后,每一次写都不再显示代码,
    # 而 create_tool **刻意不支持**会话放行。两条路写出的文件一模一样 —— 拦住它的从来
    # 不是「谁写的」,是**摘要写不进去**(test_approval_manifest_is_not_writable_by_file_tools)。
    A.write_file(os.path.join(A.TOOLS_DIR, "planted.py"),
                 "TOOL={'description':'d','parameters':{},'required':[]}\ndef run(a): return 'x'\n")
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

def test_the_quarantine_notice_says_which_kind_of_quarantine_it_is(ws, monkeypatch):
    """隔离**为什么**发生,得写在给人看的那句话里 —— 上面两条只验了「没加载」。

    真事:`figcheck.py` 的告警每次启动都响,响了两周,我一直当噪音划过去。
    去查才发现哈希锁是对的 —— 14 个工具 13 个哈希对得上,它对不上:**批准之后
    内容被改过**,正是这把锁存在的全部理由。我读漏了,因为那句话把两种情况
    揉成了「不是 create_tool 造的,**或**造好后被改过」。

    这两种严重性差得远:
    · 没批准过 —— tools/ 里冒出个陌生 .py(clone 带的、别的进程写的)。要看一眼。
    · **批准后被改过** —— 这文件是你自己批的,之后变了。你没动过它就是篡改。
    代码分得清(`name in approved` 就是判据),消息里给扔了。**守卫是对的,
    关于守卫的那句话不精确** —— 而没人查那句话,于是真报警被当成噪音磨掉。

    判据钉的是**用词**,不是行为:行为那两条已经有人管了。"""
    import agent as A
    notes = []
    monkeypatch.setattr(A, "ui", type("U", (), {"note": staticmethod(notes.append)})())

    A.create_tool("changed", "TOOL={'description':'d','parameters':{},'required':[]}\n"
                             "def run(a): return '1'\n")
    with open(os.path.join(A.TOOLS_DIR, "changed.py"), "a", encoding="utf-8") as f:
        f.write("\n# tampered\n")
    with open(os.path.join(A.TOOLS_DIR, "stranger.py"), "w", encoding="utf-8") as f:
        f.write("TOOL={'description':'d','parameters':{},'required':[]}\ndef run(a): return 'x'\n")

    A.load_dynamic_tools()
    assert notes, "两个文件被隔离了,却一句话都没跟用户说"
    msg = notes[-1]
    assert "changed.py(批准后被改过)" in msg, (
        "「批准后被改过」是这把锁真正要拦的那一种,必须单独说出来,"
        "不能跟「没批准过」揉成一个『或』。实际说的是:" + msg)
    assert "stranger.py(没批准过)" in msg, "陌生文件那一种也得写清楚。实际:" + msg
    # **原因说对了,来由还得说对。** 上一版这句让人按「篡改」去查,而实测最常见的
    # 是「造完又自己改了一版」—— 按篡改查什么也查不到,于是下次就当噪音划过去。
    assert "造完又改过" in msg, "「被改过」最常见的来由要写在前面,实际:" + msg

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

def test_the_brain_door_is_open_for_tools_and_shut_for_the_loop():
    """生产拓扑下,写路径够得着的**恰好**是哪几样 —— fixture 测不出来。

    `ws` 把 TOOLS_DIR 设成 WORKSPACE **里面**的 d/tools,于是「工具目录可写」在测试里
    永远成立,而且成立的理由是错的(它落在 workspace 那一支上)。真实布局里 TOOLS_DIR
    是 HOME 的兄弟目录,靠 `_in_workspace` 里 `_under(full, TOOLS_DIR)` 那一句单独放开 ——
    把那句删掉,全套判据照绿,而模型从此改不了自己写过的工具。跟
    test_the_default_workspace_is_never_the_source_tree 同一类:**fixture 的拓扑不是
    生产的拓扑**,差别正好落在没人看的那一格。

    两样一起断言,因为这道门的价值在于它**只开一半**:
      工具目录  可写   —— 大脑归它,create_tool 写进去的东西它得能改;
      agent.py  不可写 —— 它改不了自己正在跑的那个循环。

    那个「可写」之所以不等于「可执行」,是因为文件和摘要是两把钥匙、这道门只交出一把 ——
    但那半边**故意不写进这条判据**。反向验证证的:把 `_in_workspace` 里对批准清单的专门
    拒绝整个关掉,加上第三条断言照样绿 —— 生产布局里清单落在 `HOME/.talos/`,本来就在
    牢笼外,换个理由一样被拒。那道专门拒绝只在 HOME==WORKSPACE 的默认布局下承重,判据是
    test_approval_manifest_is_not_writable_by_file_tools(它跑在 `ws` 里,清单正好落在
    WORKSPACE 内)。**会绿,但不是因为它说的那个原因 —— 那种断言不如不写。**

    子进程跑:HOME / WORKSPACE / TOOLS_DIR 是导入时定死的模块级常量,monkeypatch
    造不出这个场景。"""
    import subprocess, sys, os
    home = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    env.pop("TALOS_WORKSPACE", None)                 # 正是默认布局这一档要测
    helper = ("def _ok(agent, p):\n"
              "    try: agent._in_workspace(p); return True\n"
              "    except ValueError: return False\n")
    code = ("import agent, os;"
            "print(_ok(agent, os.path.join(agent.TOOLS_DIR, 'x.py')),"
            "      _ok(agent, os.path.join(agent.HOME, 'agent.py')))\n")
    p = subprocess.run([sys.executable, "-c", helper + code],
                       cwd=home, env=env, capture_output=True, text=True)
    assert p.stdout.split() == ["True", "False"], (
        "大脑那道门开错了(工具目录 / agent.py 应为 可写 / 不可写):" + p.stdout + p.stderr)

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

def test_reading_a_directory_says_it_is_a_directory(ws):
    """变异扫描扫出来的空白之一。Windows 上 `open()` 一个目录抛的是光秃秃的
    「Permission denied」—— 模型会以为是权限闸拦了它,于是去申请权限、换 `run_bash`
    再试一次,而它要的只是列个目录。**错得像另一个错**,比错本身贵。"""
    import agent as A
    p = os.path.join(ws, "conf")
    os.makedirs(p, exist_ok=True)
    # **第一版这里传的是相对路径 `"conf"`,而 `ws` fixture 不 chdir** —— 于是 read_file
    # 去仓库根下找 `conf`,抛的是「越界」,断言却照样绿。摘掉被测的那道闸,它**还是绿的**:
    # 一条因为错误理由通过的判据。今天同一个坑踩了第二次(上一次是回收站那条)。
    with pytest.raises(ValueError) as e:
        A.read_file(p)
    assert "目录" in str(e.value) and "dir" in str(e.value), "得说清它是目录、以及该用什么列"
    # **这一句是 CI 的 ubuntu 两格逼出来的。** Linux 上目录的 `st_nlink` 至少是 2,
    # 于是硬链接闸(排在 isdir 之前)对**任何目录**都命中,报的是「它是个硬链接,
    # 先 del 掉链接」—— 一条模型无法执行、而且会让它去删目录的意见。
    # Windows 上目录 nlink 是 1,本机永远绿。
    assert "硬链接" not in str(e.value), "读目录报成了硬链接 —— 那条意见对方执行不了"

def test_a_directory_with_posix_link_counts_still_reads_as_a_directory(ws, monkeypatch):
    """把 Linux 的语义搬到本机来测:目录的 `st_nlink` 报 2。

    不搬的话这条回归**只有 CI 能发现**,而 CI 那一格我等四分钟才看得到。
    这个仓库为「本机绿不是证据」已经付过好几次账 —— 能搬进本机的语义就搬进来。"""
    import agent as A
    p = os.path.join(ws, "conf")
    os.makedirs(p, exist_ok=True)
    real_stat, target = os.stat, os.path.realpath(p)

    def fake(path, *a, **k):
        st = real_stat(path, *a, **k)
        if os.path.realpath(path) == target:          # 只有这一个目录假装有 2 个链接
            return os.stat_result(tuple(st)[:3] + (2,) + tuple(st)[4:])
        return st

    monkeypatch.setattr(os, "stat", fake)
    with pytest.raises(ValueError) as e:
        A.read_file(p)
    assert "硬链接" not in str(e.value) and "目录" in str(e.value), \
        f"nlink=2 的目录被当成硬链接拦了:{e.value}"

def test_the_cap_must_not_drop_the_one_file_that_is_about_to_be_overwritten(ws, monkeypatch):
    """上限按 mtime 倒序取前 N,于是**最久没动过的文件排在最末**,名额永远轮不到它 ——
    而它恰恰是这次唯一真有危险的那个。真实运行里每轮都在印「回收站这次跳过了 320 个
    较旧文件」,那 320 个不是随机的 320 个,是最不像会被碰、因此一旦被碰最没准备的那些。

    「最近谁被动过」只是「这一次写下去会不会没」的一个猜法。调用方说得出要动哪个文件的
    时候就不该再猜 —— 而 write_file / edit_file 每次都说得出。"""
    import agent as A
    monkeypatch.setattr(A, "TRASH_MAX_FILES", 3)
    monkeypatch.chdir(ws)            # 真实运行里 cwd 就是工作区(`agent.py:276` 启动时 chdir),
                                     # 模型写的相对路径靠这个才落在工作区里 —— fixture 不管这个
    old = os.path.join(ws, "stable.conf")
    with open(old, "w", encoding="utf-8") as f:
        f.write("一年没动过的好数据")
    os.utime(old, (1, 1))                              # mtime 最旧 —— 倒序排在最末
    for i in range(5):
        with open(os.path.join(ws, f"new{i}.txt"), "w", encoding="utf-8") as f:
            f.write("刚写的")
    def dumped():
        return "".join(open(os.path.join(A.TRASH_DIR, n), encoding="utf-8").read()
                       for n in os.listdir(A.TRASH_DIR))
    A.archive_workspace()
    assert "一年没动过" not in dumped(), \
        "前提没成立:它本该被上限砍掉。不砍的话下面那句什么都没测到"
    A.archive_workspace("stable.conf")                 # 传的是模型写的那种相对路径
    assert "一年没动过" in dumped(), "点名要动的文件仍然被上限砍掉了 —— 网正好漏在受力点上"

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
    monkeypatch.setattr(A, "_TRASH_LAST_SKIP", 0)   # 提示只在"跳过数变多"时说,测试要从零起
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


def test_the_skip_notice_shuts_up_until_it_gets_meaningfully_worse(ws, monkeypatch):
    """第一版每次存档都打这行,一轮刷了 4 遍。改成"变多才说"之后,任务每写一个文件跳过数
    就 +1(实测 206→207→210→211),又刷了 5 遍 —— **判据太灵敏,等于没改**。
    真正的信息是"你有一批文件没有副本",说一次就交付完了;只有明显变多才算新消息。"""
    import agent as A
    monkeypatch.setattr(A, "TRASH_MAX_FILES", 3)
    monkeypatch.setattr(A, "TRASH_DIR", os.path.join(tempfile.mkdtemp(), "t"))
    monkeypatch.setattr(A, "_TRASH_LAST_SKIP", 0)
    notes = []
    monkeypatch.setattr(A, "ui", __import__("types").SimpleNamespace(note=notes.append))

    def make(n):
        for i in range(n):
            with open(os.path.join(ws, "f%03d.txt" % i), "w", encoding="utf-8") as f:
                f.write("x%d" % i)

    make(10)
    A.archive_workspace()
    assert len(notes) == 1, f"第一次该说一声: {notes}"
    make(11)                                   # 只多一个文件 —— 跳过数 +1
    A.archive_workspace()
    assert len(notes) == 1, f"只多一个就又喊,还是刷屏: {notes}"
    make(40)                                   # 明显变多
    A.archive_workspace()
    assert len(notes) == 2, f"跳过数翻了几倍,该再说一次: {notes}"


def test_a_hardlinked_credential_never_reaches_the_trash(ws, monkeypatch):
    """`_in_workspace` 加了 st_nlink 判据之后 read_file 挡住了硬链接,但 archive_workspace
    **从不调用 _in_workspace**,自己带一套 guard —— 于是同一个文件,读被拒,而每次写操作前
    的快照会把密钥**原样拷进 .talos/trash/ 明文躺着**,一声不吭。

    补了被发现的那条入口、没补其余的 —— 这正是当天审计的主题,而修复本身重蹈了它。"""
    import agent as A
    monkeypatch.setattr(A, "TRASH_DIR", os.path.join(tempfile.mkdtemp(), "t"))
    monkeypatch.setattr(A, "_TRASH_LAST_SKIP", 0)
    real = os.path.join(ws, "secret.txt")
    with open(real, "w", encoding="utf-8") as f:
        f.write("OPENAI_API_KEY=sk-LEAK")
    link = os.path.join(ws, "notes.md")
    try:
        os.link(real, link)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("这个文件系统不支持硬链接")
    A.archive_workspace()
    for fn in os.listdir(A.TRASH_DIR):
        body = open(os.path.join(A.TRASH_DIR, fn), encoding="utf-8", errors="replace").read()
        assert "sk-LEAK" not in body, f"密钥明文进了回收站: {fn}"

def test_a_long_call_says_it_is_still_alive(monkeypatch):
    """转圈本身不说明还活着。原来的理由是「跑两次就知道这个模型一次要几十秒」——
    实测一轮里单次调用 16s~235s(15 倍跨度),没有基线可学,用户只能猜,猜错就把
    一个还活着的调用 Ctrl-C 掉。所以要定期**追加**一行(不是原地重绘:legacy_windows
    上秒数宽度一变就刷屏)。

    不用 console.begin_capture():rich 的捕获缓冲是**线程局部**的,后台线程的 print
    根本进不去那个 buffer —— 第一版测试就是这么假红的。
    """
    import time
    ui = pytest.importorskip("console_ui", reason="需要 rich(界面层的可选依赖)")
    lines = []
    monkeypatch.setattr(ui.console, "print", lambda *a, **k: lines.append(str(a[0]) if a else ""))
    monkeypatch.setattr(ui, "HEARTBEAT", 0.05)
    with ui.thinking():
        time.sleep(0.25)
    beats = [ln for ln in lines if "已等" in ln]
    assert beats, f"长调用期间一行心跳都没有:{lines!r}"
    assert "0.1s" not in " ".join(beats), f"秒数没取整:{beats!r}"

def test_no_provider_key_survives_startup(tmp_path):
    """启动那一刻,六个 provider 的 key 一个都不该留在 `os.environ` 里。

    这一半必须**真的起一个进程**:`monkeypatch.setenv` 天生发生在 import 之后,
    于是它永远走不到模块级那行 pop —— 变异测试当场证明了,把模块级改成 `_KEYS = {}`
    和「只 pop claude 一个」两个变异体都照绿。**判据结构上不可能红**,JUDGING.md
    第三种形状,而且是在为它写的判据里犯的。

    模块级那行不是 make_client 的重复:make_client 只 pop **当前** provider 的 key。
    真实的 .env 里往往同时躺着好几家的 key(实测就有 DEEPSEEK + ZHIPUAI),
    没被选中的那几个照样会被 run_bash 继承出去。"""
    import subprocess
    import sys
    import agent as A
    decoys = {e: "sk-DECOY" for e, _, _ in A.PROVIDERS.values()}
    code = ("import os, agent; "
            "print(' '.join(e for e, _, _ in agent.PROVIDERS.values() if e in os.environ))")
    r = subprocess.run([sys.executable, "-c", code], cwd=A.HOME, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       env=dict(os.environ, PYTHONIOENCODING="utf-8", **decoys))
    assert r.returncode == 0, "探针进程没起来:" + (r.stderr or "")[-800:]
    left = r.stdout.strip()
    assert not left, (
        "import agent 之后这些 key 还留在 os.environ 里:" + left + " —— "
        "run_bash 吃的是 dict(os.environ, ...),子进程会连它们一起继承。")

def test_no_provider_key_reaches_a_shell_the_model_can_run(monkeypatch):
    """`.env` 拒读堵的是文件那条路;环境变量继承这条路一直是通的。

    实测(假 key):`run_bash("echo %DEEPSEEK_API_KEY%")` 把它原样打印出来 —— 而
    run_bash 的输出会进上下文、进 provider 历史、进 `.talos/sessions/*.jsonl` 明文。
    这跟 SECURITY.md 里记成「已修」的 `read_file('.env')` 是**同一个危害、同一片面**,
    只是换了条通道。补了被发现的那条、没补其余的,这个项目已经栽过一次(硬链接 →
    回收站那条)。

    **六个 provider 全枚举,不抽查。** PROVIDERS 是个闭集,新加一个 provider 而漏了
    它的 key,这条就该红一次让人看一眼 —— 同 test_docs 的 `set(claimed) == set(_CORE)`。

    挡不住的写在这儿,免得判据被当成比它更强的东西:`create_tool` 造的工具在**本进程**
    exec,照样读得到 `A._KEYS`。加密也一样 —— 存储形态换不掉运行时形态。
    """
    import sys
    import types
    import agent as A
    fake = types.ModuleType("openai")          # 同 test_loop:离线测试不真 import openai
    fake.OpenAI = lambda **kw: object()
    monkeypatch.setitem(sys.modules, "openai", fake)
    monkeypatch.setattr(A, "_KEYS", {})        # 别把假 key 留给后面的测试

    for name, (key_env, _, _) in A.PROVIDERS.items():
        monkeypatch.setenv(key_env, "sk-DECOY-" + name)
        monkeypatch.setattr(A, "PROVIDER", name)
        A.make_client()
        assert key_env not in os.environ, (
            f"{key_env} 在建完客户端之后还留在 os.environ —— "
            "run_bash 吃的是 dict(os.environ, ...),子进程会连它一起继承")
        cmd = f"echo %{key_env}%" if os.name == "nt" else f"echo ${key_env}"
        out = A.run_bash(cmd)
        assert "sk-DECOY-" not in out, (
            f"run_bash 把 {key_env} 读出来了:{out!r} —— "
            "这段输出会进上下文和明文会话日志,后果跟 read_file('.env') 一样。")


def test_an_api_key_never_rides_a_tool_result_back_into_the_conversation(ws, monkeypatch):
    """`.env` 拒读堵的是 read_file;shell 一直是通的。

    实测:`run_bash("type .env")` 把整份 .env 原样打出来。会话放行 run_bash 之后零弹框,
    输出进上下文 → provider 历史 → `.talos/sessions/*.jsonl` 明文。跟 `read_file('.env')`
    同一个后果,而那条记在「已修」表里。

    **第二个断言比第一个重要。** 只断言「key 不在输出里」的话,命令失败、文件为空、
    工作区不对 —— 每一种都让判据变绿,而它一件事都没验(样本软到判据成摆设)。
    所以必须同时看到抹除标记:证明**真的读到了**,只是被抹了。

    两个出口都走一遍:工具的正常输出、工具抛的异常。异常那条是自造工具最容易踩的
    —— 它的报错信息是模型自己写的,里面带什么全凭它。"""
    import agent as A
    KEY = "sk-DECOY-9f3a-not-a-real-key"
    monkeypatch.setattr(A, "_KEYS", {"DEEPSEEK_API_KEY": KEY})
    with open(os.path.join(ws, ".env"), "w", encoding="utf-8") as f:
        # **同一个值写两遍**,不是凑数:第一版只写一遍,于是「只抹第一处」那个变异体
        # 照绿 —— 样本软到判据成摆设(第四种形状)。真实的 .env 里同一把 key 挂在两个
        # 名下、或旧值被注释掉留在下面,都很常见。
        f.write("DEEPSEEK_API_KEY=" + KEY + "\n# 换名之前那份: " + KEY + "\n")

    out, is_err = A.run_tool("run_bash", {"command": "type .env" if os.name == "nt" else "cat .env"})
    assert not is_err, "命令本身就失败了,这条判据什么也没验:" + out
    assert KEY not in out, "key 明文从 run_bash 回到了对话里:" + out
    assert "已抹掉" in out, (
        "既没看到 key 也没看到抹除标记 —— 那多半是命令根本没读到 .env,"
        "判据在为一个没发生的事情发绿灯。输出:" + out)

    monkeypatch.setattr(A, "run_bash", lambda c: _boom(KEY))
    out2, is_err2 = A.run_tool("run_bash", {"command": "whatever"})
    assert is_err2 and KEY not in out2, "工具抛的异常把 key 带回了对话里:" + out2
    assert "已抹掉" in out2, "异常正文里那个 key 没被抹:" + out2


def test_a_key_cut_in_half_is_still_a_key(ws, monkeypatch):
    """整值精确匹配的天花板 ③:往 key 中间插一个字节的空白,`v in s` 就不成立了。

    单独立一条,是因为它跟另外两个天花板**性质不同**:
    ① 编码变形(base64 / `certutil -encode`)—— 明文不在输出里,模型得自己解码。
       真模型红队时明确拒绝走这条,说那是「绕过安全控制」。
    ② 旁道(只报长度 / 开头 / 是不是十六进制)—— 一个明文字符都没有,一次漏几个 bit。
    ③ **切开** —— 明文一个字符都不少,去掉换行就是原文。**零解码、零轮次**,而且
       `python -c "s=open('.env').read();print(s[:20]);print(s[20:])"` 长得像
       「分块读个文件」,不像绕过 —— 拦住 ① 的那层对齐,不一定会在这儿触发。

    所以抹除不能只认整值,连续 `_RUN` 个字符也得抹。**这不叫堵上了**:见第三段。"""
    import agent as A
    KEY = "sk-DECOY-9f3a-not-a-real-key"          # 27 个字符,跟真 key 一个量级
    monkeypatch.setattr(A, "_KEYS", {"DEEPSEEK_API_KEY": KEY})
    half = len(KEY) // 2

    # ① 真实可达:一条命令就能让 key 断成两行,而且它长得像正常的分块读取。
    with open(os.path.join(ws, ".env"), "w", encoding="utf-8") as f:
        f.write("DEEPSEEK_API_KEY=" + KEY)
    cut = len("DEEPSEEK_API_KEY=") + half         # 从 key 正中间切,不是从 `sk-` 后面
    prog = "s=open('.env').read();print(s[:%d]);print(s[%d:])" % (cut, cut)
    out, is_err = A.run_tool("run_bash", {"command": 'python -c "' + prog + '"'})
    assert not is_err, "命令本身就失败了,这条判据什么也没验:" + out
    assert "已抹掉" in out, (
        "既没看到 key 也没看到抹除标记 —— 多半是命令没读到 .env,"
        "判据在为一件没发生的事发绿灯:" + out)
    assert KEY not in "".join(out.split()), (
        "把空白去掉之后 key 明文又拼回来了 —— 抹除只认整值,一个换行就绕过去了:" + out)

    # ② 换个分隔符也得挡住。只堵换行的话,「已修」就只覆盖了一条路。
    for sep in ("\n", " ", "\r\n", "|", "\t"):
        got = A._scrub(KEY[:half] + sep + KEY[half:])
        assert "已抹掉" in got, f"分隔符 {sep!r} 切开就抹不着了:{got!r}"
        assert KEY not in "".join(got.split()), f"分隔符 {sep!r}:明文还能拼回来:{got!r}"

    # ③ **把天花板本身也钉住。** 切得比 _RUN 还碎照样漏 —— 只是碎到那个份上,
    #    每片带的信息就退回 ② 的量级了。这条断言红了**不代表坏了**,代表天花板动了:
    #    请回来改这段注释、改 agent.py 里 _scrub 的 ponytail 段、改 SECURITY.md 那一行。
    #    一句只覆盖一条路的「已修」比不修更危险,所以宁可把漏的地方也写成判据。
    #    片长**写死 6**,不跟着 `A._RUN` 走:第一版写的是 `A._RUN - 2`,样本跟着常数
    #    一起缩,`_RUN` 调成 3 也照绿 —— 判据自己没法红,就是个摆设。反向验证逮到的。
    crumbs = " ".join(KEY[i:i + 6] for i in range(0, len(KEY), 6))
    assert "已抹掉" not in A._scrub(crumbs), (
        "切碎到 _RUN 以下也抹得着了 —— 天花板变了,请把上面三处文档一起改掉,"
        "别留一句过期的「已修」。")


def test_a_secret_that_is_not_a_provider_key_is_scrubbed_too(ws, monkeypatch):
    """`.env` 里不止 provider key。`GITHUB_TOKEN` 是仓库写权限,漏了比漏 LLM key 更贵。

    `_KEYS` 只装 `PROVIDERS` 表里那 6 个名字。别的东西 `_load_dotenv` 照样塞进
    `os.environ`,**而且不 pop** —— 于是 `echo %GITHUB_TOKEN%` 一句话拿到明文,
    `_scrub` 又完全不认识它。实测(假 token):子进程看得见 ✔,抹除不认识 ✘。

    **这不是没人写。** SECURITY.md 那条早就点名了:「范围仅限 Talos 自己的六个 key
    —— 你环境里的 `GITHUB_TOKEN`、`AWS_SECRET_ACCESS_KEY` 照样被子进程继承。这是
    故意的:『像密钥的变量名』是个开集,黑名单在那儿只是表演;`PROVIDERS` 是闭集,
    枚举得完。」**这个理由对,但它把两样东西说成了一样。** `os.environ` 确实是开集
    —— 用户 shell 里 export 什么谁也不知道,那一半至今没盖住(见本条末尾)。可
    `.env` 不是:它带进来什么,`_load_dotenv` 当场逐行看着,**是个闭集**。而
    `.env.example` 正是文档推荐用户填配置的地方(「就不用每次在终端 $env:... 手动
    设了」)—— **最常走的那条路,恰好是能枚举的那条**。

    所以规则反着写、挂**名字**不挂值:「像不像密钥」看值才是开集(`GITHUB_TOKEN`
    里有 TOKEN,`DATABASE_URL` 一个提示字都没有,里面照样躺着密码);而「`.env`
    带进来的、除了 Talos 自己要读要印的 `TALOS_*`」是闭的。

    **不 pop,只抹。** provider key 能 pop 是因为没人需要它进子进程(请求是 Talos
    自己发的);`GITHUB_TOKEN` / `DATABASE_URL` 放在 .env 里**就是给子进程用的**,
    pop 掉等于把 `gh` 和 `psql` 一起弄坏。所以这里只买「回不来」,不买「看不见」。
    盲发(`curl -d $GITHUB_TOKEN ...`)是 `_EXFIL` 的活,不是这条的。

    **盖不住的那半照旧、也不假装盖住了**:shell 里 export 的 `GITHUB_TOKEN` 不经过
    `.env`,这条判据管不着,实测仍是明文回到对话里。SECURITY.md 那句话在那一半上
    是对的 —— 开集就是开集。"""
    import agent as A
    TOKEN = "ghp-DECOY-repo-write-token-1111"
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    # **按生产的路子建 _KEYS**,不是手工塞一个进去 —— 手工塞等于预设了修复:
    # 真正漏的地方是 `.env` 的 GITHUB_TOKEN **压根进不了** _KEYS。这样写,
    # 挑选规则一旦漏掉它,下面那条端到端断言也跟着红。
    monkeypatch.setattr(A, "_KEYS", A._dotenv_secrets({"GITHUB_TOKEN": TOKEN}))

    cmd = "echo %GITHUB_TOKEN%" if os.name == "nt" else "echo $GITHUB_TOKEN"
    out, is_err = A.run_tool("run_bash", {"command": cmd})
    assert not is_err, "命令本身就失败了,这条判据什么也没验:" + out
    assert "已抹掉" in out, (
        "既没看到 token 也没看到抹除标记 —— 多半是 echo 根本没取到变量,"
        "判据在为一件没发生的事发绿灯:" + out)
    assert TOKEN not in out, "GITHUB_TOKEN 明文从 run_bash 回到了对话里:" + out

    # 挑值的规则本身:名字是开集,`.env` 的内容是闭集,所以按后者挑。
    got = A._dotenv_secrets({
        "TALOS_PROVIDER": "deepseek",              # Talos 自己的配置,正常输出里会出现
        "TALOS_MODEL": "deepseek-v4-flash",        # 够长,但名字说明它不是秘密
        "GITHUB_TOKEN": TOKEN,                     # 名字里有 TOKEN
        "DATABASE_URL": "postgres://u:pw3333@h/db",  # 名字里一个提示字都没有
        "EDITOR": "vim",                           # 太短:抹了会把正常输出糊花
    })
    assert set(got) == {"GITHUB_TOKEN", "DATABASE_URL"}, (
        "挑错了。TALOS_* 是 Talos 自己要印的,短值抹了会糊花正常输出;"
        f"剩下的一律当秘密。实际挑出:{sorted(got)}")

    # 上面两段测的都是**规则**。把规则接到 `_KEYS` 上的是一句模块级语句,删掉它
    # 整个修复无声蒸发,而两段都还绿。所以这儿真起一个进程 import 一次。
    # 不能靠本机的 `.env`:CI 上没有、本机上有 —— 上一次 CI 四格全红就是栽在这个差别上。
    fake = os.path.join(ws, "envprobe")
    os.makedirs(fake, exist_ok=True)
    with open(os.path.join(fake, ".env"), "w", encoding="utf-8") as f:
        f.write("TALOS_PROVIDER=deepseek\nGITHUB_TOKEN=" + TOKEN + "\n")
    r = subprocess.run(
        [sys.executable, "-c", "import agent, os, sys;"
         "sys.stdout.write(repr(sorted(k for k, v in agent._ENV_SECRETS.items() if v == sys.argv[1])))",
         TOKEN],
        cwd=fake, capture_output=True, text=True,
        env=dict(os.environ, PYTHONPATH=os.path.dirname(A.__file__),
                 TALOS_WORKSPACE=fake))
    assert r.returncode == 0, "import agent 就崩了,这条判据什么也没验:" + r.stderr[-800:]
    assert "GITHUB_TOKEN" in r.stdout, (
        "`.env` 里的 GITHUB_TOKEN 没进 _ENV_SECRETS —— 挑选规则对了,但没接上去,"
        "于是 `echo %GITHUB_TOKEN%` 的输出照样明文回到对话里。stdout=" + r.stdout)


def test_a_dotenv_secret_never_mangles_normal_output(ws, monkeypatch):
    """抹除的**假阳性**一侧 —— 上面那条只测了「该抹的抹掉了」。

    真事,而且是这套判据自己放进去的:`.env` 的值并进 `_KEYS` 之后跟着走了 `_scrub`
    的 10 字符滑窗。于是一行几乎每个项目都有的

        DATABASE_URL=postgresql://user:pw@localhost:5432/app

    让 `postgresql`(一个普通英文单词,正好 10 字符)和 `localhost:` 在**任何**工具输出里
    被换成「[已抹掉:这是你的 API key]」。模型收到的东西被静默改写,还贴了一句假话:
    `edit_file` 因此报「找不到原文」,而模型照着抹花的内容写回去,标记就落进真文件。

    **这个害处被它替换掉的那段注释原样预言过**(「会把 deepseek 一起抹掉,毁正常输出」)。
    当时只反驳了一半:短值那半靠 `len(v) >= _RUN` 挡住,长值的常见子串那半没想到。
    所以这条判据钉的是**两侧**:正常输出一个字不动,整值命中照样抹。

    留下的天花板写在 `_scrub` 里:**被切开的 `.env` 秘密抹不掉。** 那要一次刻意的攻击,
    而误伤是每次运行都在发生的确定损害 —— 两害相权。"""
    import agent as A
    monkeypatch.setattr(A, "_KEYS", {"deepseek": "sk-" + "a1b2c3d4e5" * 4})
    monkeypatch.setattr(A, "_ENV_SECRETS", {
        "DATABASE_URL": "postgresql://user:pw@localhost:5432/app",
        "API_BASE": "https://api.internal.example.com/v1"})

    for clean in ("Server listening on http://localhost:5432 (press Ctrl+C)",
                  "conftest.py: could not connect, is postgresql running?",
                  "curl https://api.example.org/ok",
                  "def apply(self): return self.host"):
        assert A._scrub(clean) == clean, (
            "正常输出被抹花了 —— 模型收到的不是它请求的东西,而且被贴上一句假话:\n  "
            + A._scrub(clean))

    # 该抹的两侧都还在
    assert "已抹掉" in A._scrub("DB=postgresql://user:pw@localhost:5432/app"), \
        ".env 秘密的整值命中没抹掉"
    assert "已抹掉" in A._scrub("sk-a1b2c3d4e5a1b2c3d4e5\na1b2c3d4e5a1b2c3d4e5"), \
        "被切开的 provider key 没抹掉 —— 滑窗该只对 _KEYS 生效,不是被一起删了"


def _boom(key):
    raise RuntimeError("自造工具报错时把配置抄了进来:" + key)


def test_edit_file_writes_back_in_the_encoding_it_read(ws):
    """改一个字,不许顺手把整个文件的编码换掉。

    PowerShell 的 `>` 默认写 UTF-16LE,不少编辑器给 UTF-8 加 BOM。`edit_file` 原来
    读得对(`_read_full` 专门处理过这两种),**写回时一律 utf-8 无 BOM** —— 一次编辑之后
    PowerShell 再读那份日志就是乱码,而屏幕上只说了一句 `edited`。
    `_read_full` 的 docstring 当年已经写着「别把乱码写回原文件」,**想到了内容,没想到编码。**

    判据是**字节级**的:解码之后比对会让这个 bug 完全隐形 —— 两边解出来都是同一串文本。"""
    import agent as A, os
    for enc, bom in (("utf-16", b"\xff\xfe"), ("utf-8-sig", b"\xef\xbb\xbf"), ("utf-8", b"")):
        p = os.path.join(ws, "note-%s.txt" % enc)
        with open(p, "w", encoding=enc, newline="") as f:
            f.write("旧值 = 1\n第二行\n")
        A.edit_file(p, "旧值 = 1", "新值 = 2")
        raw = open(p, "rb").read()
        assert raw.startswith(bom), \
            f"{enc} 的文件被 edit 之后 BOM/字节序标记没了(开头是 {raw[:4]!r})—— 编码被换掉了"
        assert open(p, encoding=enc).read().startswith("新值 = 2"), f"{enc}: 内容没改对"


def test_a_dotenv_line_with_no_key_does_not_take_the_whole_program_down(tmp_path):
    """`.env` 里手滑写成 `=value`(等号在行首)—— 原来整个 `import agent` 当场失败。

    `os.environ.setdefault("", v)` 在 Windows 上抛 `ValueError: illegal environment
    variable name`,而 `_load_dotenv()` 是在**模块导入时**跑的、外面没有 try。
    于是 Talos 连启动都启动不了,而 traceback 指着 `os.environ` 那一行,
    **看不出病根是 `.env` 里的哪一行**。"""
    import agent as A, os
    p = tmp_path / ".env"
    p.write_text("=oops\nTALOS_DEMO_OK=1\n", encoding="utf-8")
    A._load_dotenv(str(p))                                   # 不许抛
    assert os.environ.get("TALOS_DEMO_OK") == "1", "跳过坏行之后,后面的行还得照常读进来"
    os.environ.pop("TALOS_DEMO_OK", None)
    assert "" not in os.environ


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe 特有")
def test_a_quoted_search_string_is_not_mistaken_for_a_bash_command(ws, monkeypatch):
    """引号里的是**要搜的字符串**,不是要执行的命令。

    `findstr /c:"$(" app.js`(在 js 里找 jQuery 的 `$(`)和 `findstr "| grep" notes.txt`
    都是完全合法的 cmd,原来被这道闸一律拒掉 —— 而拒绝的话术是「这是 cmd.exe,不是 bash」,
    模型照着改也改不出能跑的命令来。

    另一半同样重要:**引号外面的照旧要拦。** 只断言前一半的话,把整条 `_BASHISM` 删掉
    也会绿。

    `skipif` 是补上去的 —— 这道闸整个包在 `run_bash` 的 `if os.name == "nt":` 里面,
    在 Linux 上 `cat notes.txt` 本来就是合法命令、本来就不该拦,于是第二个循环必红。
    **紧挨着的三条判据各自都带着守卫**(两条 `skipif`、一条早返回),我加这条时一条都没看,
    于是 CI 的两个 ubuntu 格子红了两轮,而我本机四步全绿 —— workflow 的注释就写着
    「**凡是只在我本机跑过的判据,都要当成没跑过**」,这次就是那句话本身。"""
    import agent as A
    monkeypatch.setattr(A, "WORKSPACE", ws)
    for ok in ('findstr /c:"$(" app.js', 'findstr "| grep" notes.txt', 'echo "ls -la" > x.txt'):
        A.run_bash(ok)                                       # 不许抛
    for bad in ("echo hi & pwd", "dir | wc -l", "cat notes.txt", "echo $(dir)"):
        try:
            A.run_bash(bad)
        except ValueError:
            continue
        raise AssertionError(f"`{bad}` 是真的 bashism,这道闸放过去了")


def test_a_tool_whose_required_is_not_a_list_is_refused_at_registration(ws, monkeypatch):
    """`required` / `parameters` 的**值**是模型现写的,写歪了有三种死法,都要在注册这一步拦。

    `'required': None` 最狠:`_bad_args` 里 `for k in None` 抛 TypeError,而 `_bad_args`
    **跑在 `run_tool` 的 try 之外**(它必须在 `check_permission` 之前)—— 整轮当场没了。
    `'required': 'path'` 不崩,更阴:`_bad_args` 逐字符迭代,回给模型
    「少了必填参数 ['p','a','t','h']」,一句它没法照着改的话。
    两者再加 dict,还都会被 `tool_specs()` 原样发给接口,严格的 provider 直接 400。"""
    import agent as A, os
    monkeypatch.setattr(A, "TOOLS_DIR", ws)
    monkeypatch.setattr(A, "TOOLS", dict(A.TOOLS))
    for i, bad in enumerate(('"path"', "None", '{"path": 1}', "42")):
        p = os.path.join(ws, "bad%d.py" % i)
        with open(p, "w", encoding="utf-8") as f:
            f.write("TOOL = {'description': 'd', 'parameters': {'path': {'type': 'string'}}, "
                    "'required': %s}\ndef run(args): return 'ok'\n" % bad)
        try:
            A._load_tool(p)
        except ValueError as e:
            assert "required" in str(e), f"required={bad} 拒对了,但报错没告诉模型该改哪个键"
            continue
        raise AssertionError(f"required={bad} 被注册进去了 —— 下游三处各有一种坏法")
    # 正常的那个照旧过 —— 只断言上面的话,把这道检查写成 `raise` 也会绿
    good = os.path.join(ws, "good.py")
    with open(good, "w", encoding="utf-8") as f:
        f.write("TOOL = {'description': 'd', 'parameters': {'path': {'type': 'string'}}, "
                "'required': ['path']}\ndef run(args): return 'ok'\n")
    assert A._load_tool(good) == "good"

def test_a_tool_that_calls_sys_exit_does_not_take_the_agent_with_it(ws):
    """`SystemExit` 不是 `Exception` —— 它从 `run_tool` 的 except 里**穿过去**,
    而 `repl()` 只接 `Exception` 和 `KeyboardInterrupt`。于是自建工具里一句
    `sys.exit(1)`(模型处理坏输入时的常规写法)会让整个进程退出,这一轮还没落盘。
    一个工具的错误不该有权力结束宿主。

    顺带钉住空消息:`raise ValueError()` 原来产出光秃秃的 `"error: "`,
    模型看着它只能猜是什么错。"""
    import agent as A
    A.TOOLS["_boom"] = (lambda a: sys.exit(2), {}, [], "d", "bash")
    A.TOOLS["_mute"] = (lambda a: (_ for _ in ()).throw(ValueError()), {}, [], "d", "bash")
    try:
        out, err = A.run_tool("_boom", {})            # 逃出来的话这一行直接 SystemExit
        assert err and "2" in out, f"工具 sys.exit 之后应该是一条普通的工具错误:{out!r}"
        out, err = A.run_tool("_mute", {})
        assert out.strip() != "error:", "消息为空时得说清是什么错,不能只留一个 error:"
        assert "ValueError" in out, out
    finally:
        del A.TOOLS["_boom"], A.TOOLS["_mute"]

def test_autocommit_looks_at_the_file_write_file_actually_wrote(ws, monkeypatch):
    """`_autotest` 原来自己 `realpath(args["path"])`,而 `write_file` 走 `_in_workspace`
    (它还会剥掉 `workspace/` 前缀)。模型写 `workspace/x.py` 时两边差一层目录 ——
    自动提交对着一个不存在的路径问「变了吗」,答「没变」,一声不吭地什么都不提交。

    同一条规则两处实现,这个仓库记过四次。判据钉的是「两边拿到同一个路径」。"""
    import agent as A
    # **工作区必须真的叫 `workspace`,cwd 也得就在里面** —— 前缀剥离只在
    # `head == basename(WORKSPACE)` 时发生,两边不分叉的话摘掉修复照样绿(第一版就是这样)。
    # 这两条现在都由 `ws` 自己保证。
    seen = []
    monkeypatch.setattr(A, "_autotest", lambda full: seen.append(full) or "")
    A.run_tool("write_file", {"path": "workspace/nested.py", "content": "v1\n"})
    assert seen, "write_file 之后没调 _autotest"
    assert os.path.exists(seen[0]), \
        f"_autotest 拿到的是一个不存在的路径 {seen[0]!r} —— 它和真正被写的那个不是同一个"

def test_the_trash_covers_every_file_the_write_tools_may_touch(ws, monkeypatch):
    """可写的集合和被保护的集合必须是同一个。`_in_workspace` 明确允许 `write_file`
    改自建工具(`tools/`),而回收站的保护根是 `[WORKSPACE, SKILLS_DIR]` ——
    于是点名要存的那个文件如果是个工具,解析出来又被「不在保护根之下」悄悄丢掉。
    **网看着在,那一份没存。**"""
    import agent as A
    # **`tools/` 必须在工作区外面**,否则保护根少一个也照样存得下,摘掉修复这条仍然绿
    # (第一版就是这样)。这一条现在由 `ws` 自己保证 —— 它的布局跟生产同构。
    os.makedirs(A.TOOLS_DIR, exist_ok=True)
    p = os.path.join(A.TOOLS_DIR, "mytool.py")
    with open(p, "w", encoding="utf-8") as f:
        f.write("要被覆盖的好代码")
    assert A._in_workspace(p), "前提没成立:工具文件本来就不该可写"
    A.archive_workspace(must=p)
    dumped = "".join(open(os.path.join(A.TRASH_DIR, n), encoding="utf-8").read()
                     for n in os.listdir(A.TRASH_DIR)) if os.path.isdir(A.TRASH_DIR) else ""
    assert "要被覆盖的好代码" in dumped, "自建工具被覆盖前没有副本 —— 它是可写的,就该被保护"


def test_no_module_level_constant_is_silently_redefined():
    """`_QUOTED` 被定义了两次,`run_bash` 那道闸运行时拿到的是**权限闸那一版**。

    776 行:`re.compile(r'"[^"\n]*"')`,注释写「成对的双引号段 —— 里面是搜索串」,
    docstring 写「**先把双引号里的内容挖空再找**」。
    1725 行同名重定义:带捕获组、双引号**和单引号**、内容 1~260 字符,给 `_targets` 用。

    实测(不是推断)`echo it's here; ls x's`:
      · 776 那版挖空后 -> 命中 `; ls`(按注释,该拦)
      · 实际生效的那版 -> None(放行)
    命令里出现两个单引号,夹在中间的整段被当引号内容挖空,里面的 bashism 就看不见了。
    776 行是彻底执行不到的死定义,而注释描述的正是它。

    判据是**一般形式**:模块级不许有同名赋值。下一个重名不需要有人恰好读到这两行。
    """
    import ast
    import inspect
    import agent as A
    tree = ast.parse(inspect.getsource(A))
    names = [t.id for n in tree.body if isinstance(n, ast.Assign)
             for t in n.targets if isinstance(t, ast.Name)]
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, (f"模块级重名(后一个无声盖掉前一个,而注释还在描述前一个):{dupes}")


def test_every_entry_point_announces_a_disabled_skill():
    """技能被红旗隔离时,`-p` 一个字都不说。

    `技能已停用` 全仓库只出现在 `repl()` 里。`-p` 上被命中的技能从常驻清单
    (`retrieve` 把它筛掉)和检索(`recall(blocked=…)`)里**同时消失**,没有任何输出。
    大小写那道闸是启发式(`_SKILL_RED_FLAGS` 七条正则全带 `re.I`),误伤过真技能:
    `mutation-testing.md` 因为正文一句「natural_key 的数字转 int」被整条隔离。

    对照组就在同一个启动序列里:**工具**那条隔离告警发在 `load_dynamic_tools()`
    **函数内部**(靠 `ui is not None` 守着),所以两条路都打得出来;技能那条写在
    `repl()` 的调用方。同一类事件,两种接法 —— 不对称就是这么来的。

    后果不在「有没有人写过承诺」上:EXAM / benchmark 跑在 `-p`,一条技能悄悄消失
    会让分数变了而没人看得出原因;SECURITY.md 把这道启动扫描列为恶意技能的缓解措施,
    而排除真发生了、没人被告知去看那个文件,于是它永远留在 `skills/` 里没人复核。

    判据**从「谁调 load_dynamic_tools」推导**出启动路径,不写死 repl/once 两个名字。
    """
    import ast
    import inspect
    import agent as A
    tree = ast.parse(inspect.getsource(A))
    # **查 Call 节点,不查子串。** 第一版用 `"load_dynamic_tools()" in ast.unparse(fn)`,
    # 而 `ast.unparse` 出来的 `def load_dynamic_tools() -> list:` 自己就含这个子串,
    # 于是它把被调的那个函数当成了启动路径 —— 判据写成文本形状,第二次栽在同一件事上。
    def _calls(fn, name):
        return any(isinstance(n, ast.Call) and getattr(n.func, "id", "") == name
                   for n in ast.walk(fn))
    entries = [fn for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef)
               and _calls(fn, "load_dynamic_tools")]
    assert entries, "没找到任何启动路径 —— 这条判据自己瞎了"
    missing = [fn.name for fn in entries if not _calls(fn, "_note_disabled_skills")]
    assert not missing, (f"这些启动路径不会说技能被停用了:{missing} —— "
                         f"被误伤的技能从常驻清单和检索里同时消失,而没人被告知")
