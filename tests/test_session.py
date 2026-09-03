"""会话存储:自动起名 / 往返 / 编号+前缀解析 / 删除 / 旧格式迁移。"""
import contextlib
import os
import sys

import pytest

def test_session_ids_come_from_a_clock_that_never_goes_backwards(ws, monkeypatch):
    """发号、扫占位符、`resolve` 的前缀匹配,整套都建立在「时间只往前走,过去的那一秒
    不会再来」上 —— 而**本地时间每年会违反一次**:夏令时秋季回拨,本地钟真的退回一小时。
    那一小时里起的会话跟一小时前撞号,而扫占位符只认「不是当前这一秒就删」,
    会把一个还活着的号放出来,两个会话写进同一个 sid。

    UTC 没有回拨,所以判据是「sid 来自 `gmtime` 而不是 `localtime`」。
    **不能直接拿两者比相等** —— 本机时区恰好是 UTC 时那句断言恒真、换台机器才红,
    正是「判据烤进本机语义」那种。所以把 `gmtime` 换成一个哨兵:
    代码要是去问 `localtime`,这个哨兵就出不来。"""
    import time
    import session as S
    fake = time.struct_time((2020, 1, 2, 3, 4, 5, 3, 2, 0))
    monkeypatch.setattr(S.time, "gmtime", lambda *a: fake)
    assert S.Session.new().sid.startswith("20200102-030405"), \
        "sid 不是从 UTC 来的 —— 夏令时回拨那一小时会发出重复的号"


def test_roundtrip_and_autotitle(ws):
    import session as S
    s = S.Session.new()
    s.save([{"role": "user", "content": "给你自己造一个 hashify 工具"},
            {"role": "assistant", "content": "好"}])
    assert "__" in os.path.basename(s.path) and "hashify" in s.slug
    assert len(S.open_session(s.sid).load()) == 2

def test_resolve_index_and_prefix(ws):
    import session as S
    s = S.Session.new()
    s.save([{"role": "user", "content": "first"}])
    assert S.resolve("1") == s.sid            # 列表编号
    assert S.resolve(s.sid[:8]) == s.sid      # id 前缀(全数字也认)
    assert S.resolve("") is None
    assert S.resolve("nope") is None

def test_delete(ws):
    import session as S
    s = S.Session.new()
    s.save([{"role": "user", "content": "x"}])
    assert S.delete(s.sid) is True
    assert S.open_session(s.sid) is None

def test_load_survives_poisoned_jsonl(ws):
    """#8:一行畸形 JSON 不能让 /resume 整个崩掉。"""
    import session as S
    os.makedirs(S.SESS_DIR, exist_ok=True)
    p = os.path.join(S.SESS_DIR, "20990101-000000__x.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"role":"user","content":"ok"}\n')
        f.write('{ not json at all \n')                       # 投毒行
        f.write('"just a bare string"\n')                     # 不是 dict
        f.write('{"role":"assistant","content":"fine"}\n')
    msgs = S.open_session("20990101-000000").load()
    assert len(msgs) == 2 and msgs[0]["content"] == "ok" and msgs[1]["content"] == "fine"

def test_session_id_glob_metachars(ws):
    """sid 里的 * 不能当通配符命中别的会话。"""
    import session as S
    s = S.Session.new()
    s.save([{"role": "user", "content": "real"}])
    assert S._path_for("*") is None and S._path_for("?") is None
    assert S._path_for(s.sid) is not None

def test_two_sessions_in_the_same_second_are_both_reachable(ws, monkeypatch):
    """sid 是秒级时间戳,而它是会话的**全部身份**。

    同一秒起两个会话就是同一个 sid。两个文件都写得出来(slug 不同),`/history` 里两条
    都看得见 —— 但 `_path_for` 是前缀匹配、只返回排序第一个,于是 `/resume` 谁都只能
    回到第一个,**第二个会话永远打不开**。它没丢,是够不着。

    时钟冻住再测:靠"跑得够快所以同一秒"是把本机时序烤进判据,今天已经栽过四次。"""
    import session as S
    monkeypatch.setattr(S.time, "strftime", lambda *a: "20260811-120000")
    a = S.Session.new()
    a.save([{"role": "user", "content": "第一个会话"}])
    b = S.Session.new()
    b.save([{"role": "user", "content": "第二个会话"}])
    assert a.sid != b.sid, "同一秒的两个会话拿到了同一个 id"
    assert S.open_session(a.sid).load()[0]["content"] == "第一个会话"
    assert S.open_session(b.sid).load()[0]["content"] == "第二个会话"
    # 完整的 sid 不许被前缀命中抢走:`-2` 里的 `-`(0x2D)排在 `__` 的 `_`(0x5F)前面,
    # 上一版 `sorted()[0]` 会把 20260811-120000 解析成那个带序号的别人。
    assert S._path_for(a.sid) == a.path
    assert len({r[0] for r in S.list_sessions()}) == 2

def test_two_sessions_that_have_not_been_saved_yet_still_get_different_ids(ws, monkeypatch):
    """上一条测试是 new→save→new,**证明不了并发**:它只覆盖了「撞号检查看得见对方的文件」。

    `new()` 从不落盘,而上一版的检查只看已落盘的文件 —— 两个都还没 `save()` 的会话拿到
    同一个 sid,save 完 `/resume` 谁都只能回到第一个,第二个会话永远打不开。并发起会话、
    或者起了会话半天不说话(延迟落盘),长的都是这个样子。
    占位符是**盘上的文件**不是内存里的表,所以这一条同时也是跨进程的判据。
    时钟冻住测:靠"跑得够快所以同一秒"是把本机时序烤进判据。"""
    import session as S
    monkeypatch.setattr(S.time, "strftime", lambda *a: "20260811-120000")
    a = S.Session.new()
    b = S.Session.new()                                    # 两个都还没落盘
    assert a.sid != b.sid, "两个都还没 save() 的会话拿到了同一个 id"
    a.save([{"role": "user", "content": "第一个会话"}])
    b.save([{"role": "user", "content": "第二个会话"}])
    assert S.open_session(a.sid).load()[0]["content"] == "第一个会话"
    assert S.open_session(b.sid).load()[0]["content"] == "第二个会话"
    # 敲全的 id 不许被更长的兄弟抢走:`20260811-120000` 也是 `20260811-120000-2` 的前缀,
    # 而 `resolve` 按 mtime 排 —— 后写的 b 排在最前面,`/resume <a 的完整 id>` 会打开 b。
    assert S.resolve(a.sid) == a.sid and S.resolve(b.sid) == b.sid
    S.Session.new()                                        # 占住号,但一直不 save
    assert len(S.list_sessions()) == 2, f"占位的空会话冒出来了:{S.list_sessions()}"

def test_a_locked_old_file_must_not_hijack_the_new_one(ws, monkeypatch):
    """改名时 `os.remove(old)` 在 try 外面 —— 而它跑在 `os.replace` **之后**。

    旧文件删不掉(Windows 上一个不带 delete-sharing 的句柄就够了),`save()` 就整个抛出去:
    调用方看到「保存失败」,可磁盘上新文件已经完整落盘了,只是旁边还留着旧的那个。两个文件
    的**精确 sid 相同**,而 `.`(0x2E)排在 `_`(0x5F)前面,`_path_for` 按名字排恰好挑中
    旧格式那个 —— `/resume` 读回旧内容:**这一轮的对话存下来了,却看不见。**

    造法用 monkeypatch 让 `os.remove` 抛 `PermissionError`:ubuntu 和 windows 跑的是同一条
    代码路径,不是 skip。Windows 上再拿一个**真句柄**复跑一遍 —— CPython 的 `open()` 不带
    FILE_SHARE_DELETE,`os.remove` 会真的抛,用来验证上面那个模拟没有在测一件不存在的事。"""
    import session as S

    @contextlib.contextmanager
    def refused(legacy):
        """两个平台同一条路径:只对这个文件抛,别的 remove(比如清 .tmp)照常放过去。

        `normcase` 不是为了迁就 Windows —— 它在 POSIX 上是恒等函数,两边都是**正确的**
        同一个文件判断,而不是"在 Linux 上跳过"。"""
        real = os.remove
        def blocked(path, *a, **kw):
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(legacy)):
                raise PermissionError(13, "旧文件被占用(模拟)")
            return real(path, *a, **kw)
        with monkeypatch.context() as m:
            m.setattr(S.os, "remove", blocked)
            yield

    def migrate(sid, blocker):
        os.makedirs(S.SESS_DIR, exist_ok=True)
        legacy = os.path.join(S.SESS_DIR, sid + ".jsonl")
        with open(legacy, "w", encoding="utf-8") as f:
            f.write('{"role": "user", "content": "旧内容"}\n')
        og = S.open_session(sid)
        assert og.slug == ""                    # 旧格式:这次 save 才会改名,才走得到 os.remove(old)
        with blocker(legacy):
            with pytest.warns(UserWarning):     # 删不掉可以,无声无息不行
                og.save([{"role": "user", "content": "旧内容"},
                         {"role": "assistant", "content": "新内容"}])   # 抛出去 = 这条红
        assert os.path.exists(legacy), "这条测试要靠一次真的删不掉,而它删掉了"
        assert os.path.exists(og.path), "新文件没落盘"
        assert [m["content"] for m in S.open_session(sid).load()] == ["旧内容", "新内容"], \
            "/resume 读回了那个没删掉的旧文件 —— 这一轮存下来了却看不见"

    migrate("20200303-000000", refused)
    if sys.platform == "win32":                 # 附加:同一条断言,换成真的被占用的句柄
        migrate("20200304-000000", lambda legacy: open(legacy, "r", encoding="utf-8"))

def test_abandoned_claims_do_not_pile_up_forever(ws, monkeypatch):
    """占位符只在**那一秒**有意义,而遗弃它不罕见:每一次没产生对话就结束的启动都留一个
    (敲错命令、看一眼就退、CLI 参数打错)。实测就是这么撞见的 —— 一条 `--history`
    (它其实不是 CLI 参数,是 REPL 里的 `/history`)启动了 REPL 又退出,当场留下一个。

    判据是精确的、不是启发式的:`20260812-143138.claim` 只可能挡住那一秒的会话,
    而那一秒永远不会再来。所以「不是当前这一秒的占位符」= 对谁都不再有意义,可以删。
    不需要 atexit,也不需要赌进程能正常收尾。"""
    import session as S
    clock = {"t": "20260101-000000"}
    monkeypatch.setattr(S.time, "strftime", lambda *a: clock["t"])
    for _ in range(3):                                # 三个起了就扔的会话
        S.Session.new()
    claims = lambda: sorted(f for f in os.listdir(S.SESS_DIR) if f.endswith(".claim"))
    assert len(claims()) == 3, f"同一秒的三个占位符该都留着:{claims()}"
    clock["t"] = "20260101-000005"                    # 时间往前走 5 秒
    live = S.Session.new()
    assert claims() == [live.sid + ".claim"], f"上一秒的垃圾没清掉:{claims()}"
    # 同一秒的别人不许被清掉 —— 那是活的占位符,清了就撞号
    other = S.Session.new()
    assert set(claims()) == {live.sid + ".claim", other.sid + ".claim"}, \
        "把同一秒里别人正占着的号清掉了"
    # 真会话文件一根手指都不许碰
    live.save([{"role": "user", "content": "真会话"}])
    assert S.open_session(live.sid).load()[0]["content"] == "真会话"


def test_a_failed_save_must_not_destroy_the_previous_one(ws):
    """上一版顺序是「先删旧的,再打开新的写」—— 中间任何一次失败都是两个文件都没有。

    而且 `open(..., "w")` 是先截断再写:崩在半路留下的是半截文件,`load()` 跳过坏行
    所以它不报错,**它只是安静地少了后半段**。删除是这个项目里唯一没有撤销的动作。"""
    import json
    import session as S
    s = S.Session.new()
    s.save([{"role": "user", "content": "写好的正文"}])
    keep = s.path
    assert S.open_session(s.sid).load()[0]["content"] == "写好的正文"
    try:                                        # set 不能 JSON 序列化 —— 写到一半炸掉
        s.save([{"role": "user", "content": "新的第一句"}, {"role": "x", "bad": {1, 2}}])
    except TypeError:
        pass
    else:
        raise AssertionError("这条测试要靠一次真实的写失败,它没炸")
    assert os.path.exists(keep), "写失败之后旧文件没了"
    assert S.open_session(s.sid).load()[0]["content"] == "写好的正文", "旧内容被截断了"
    # 半成品自己收走:save() 每轮都跑,稳定复现的失败会把目录堆满
    assert [f for f in os.listdir(S.SESS_DIR)] == [os.path.basename(keep)], \
        f"写失败之后目录里还剩别的东西:{os.listdir(S.SESS_DIR)}"

    # **真正会丢数据的是改名那一次。** 上面两次 save 的文件名没变(slug 已经定了),
    # 所以 `os.remove(old)` 那条分支根本没走到 —— 退回「先删后写」时这条测试照样绿,
    # 是变异体测出来的。会走到改名的是旧格式迁移:`<sid>.jsonl` -> `<sid>__<slug>.jsonl`。
    os.makedirs(S.SESS_DIR, exist_ok=True)
    legacy = os.path.join(S.SESS_DIR, "20200202-000000.jsonl")
    with open(legacy, "w", encoding="utf-8") as f:
        f.write('{"role": "user", "content": "旧会话的正文"}\n')
    og = S.open_session("20200202-000000")
    try:
        og.save([{"role": "user", "content": "旧会话的正文"}, {"role": "x", "bad": {1, 2}}])
    except TypeError:
        pass
    else:
        raise AssertionError("这条测试要靠一次真实的写失败,它没炸")
    assert os.path.exists(legacy), "改名途中写失败,旧文件已经被删了 —— 这个会话没了"
    assert S.open_session("20200202-000000").load()[0]["content"] == "旧会话的正文"

def test_history_counts_the_same_messages_resume_loads(ws):
    """同一个文件在两个地方给出两个数。

    `load()` 跳过坏行继续读;`list_sessions()` 把 `json.loads` 放在 try 里、except 在整个
    循环外面 —— 第 3 行坏掉,后面全都不数。`/history` 报 2 条,`/resume` 读回 4 条,
    而中间没有任何报错:**它只是默默少数。**"""
    import session as S
    os.makedirs(S.SESS_DIR, exist_ok=True)
    p = os.path.join(S.SESS_DIR, "20990102-000000__x.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"role":"user","content":"第一句"}\n')
        f.write('{"role":"assistant","content":"回答"}\n')
        f.write("{ 坏行 \n")                                   # 投毒
        f.write('"一个裸字符串"\n')                            # 不是 dict
        f.write('{"role":"user","content":"第二句"}\n')
        f.write('{"role":"assistant","content":"再答"}\n')
    row = next(r for r in S.list_sessions() if r[0] == "20990102-000000")
    loaded = S.open_session("20990102-000000").load()
    assert row[3] == len(loaded) == 4, f"/history 数出 {row[3]} 条,/resume 读回 {len(loaded)} 条"
    assert row[2] == "第一句", "坏行把标题也带没了"

def test_delete_removes_every_file_that_is_this_session(ws):
    """删完还能 resume 回来 —— 这是「说删了却没删」,比删多了更糟。

    旧文件删不掉那次(见 `save()` 尾巴)盘上留了两个**精确同 sid** 的文件。上一版
    `delete` 只删 `_path_for` 挑中的那一个(新格式那份),旧格式的残留还在,于是
    `/delete` 印「deleted」,`/resume` 照样读回旧内容 —— 实测过。

    只删**精确**命中:前缀命中是另一个会话的 id,不是这个会话的副本,一根手指都不能碰。"""
    import session as S
    os.makedirs(S.SESS_DIR, exist_ok=True)
    W = lambda n, c: open(os.path.join(S.SESS_DIR, n), "w", encoding="utf-8").write(
        '{"role":"user","content":"%s"}\n' % c)
    W("20200101-000000.jsonl", "旧格式残留")               # 同一个 sid 的两份
    W("20200101-000000__新标题.jsonl", "新写好的")
    W("20200101-000000-2__另一个会话.jsonl", "别人")       # 前缀命中,但**不是**同一个 sid
    assert S.delete("20200101-000000") is True
    assert S._exact("20200101-000000") == [], "旧格式那份残留还在 —— resume 能把它救回来"
    assert S.open_session("20200101-000000-2").load()[0]["content"] == "别人", \
        "把前缀命中的另一个会话也删了"
    # 注意这里**不能**断言 open_session 返回 None:删干净之后它命中的是 `-2` 那条,
    # 那是既有的「敲一半 id」前缀行为,不是残留。我第一版就断言错了这一条。
    # 删不干净就不许报成功
    W("20200101-000000__回来了.jsonl", "又出现了")
    real = os.remove
    try:
        os.remove = lambda p, *a, **k: (_ for _ in ()).throw(PermissionError("占用"))
        with pytest.warns(UserWarning):
            assert S.delete("20200101-000000") is False, "一个都没删掉,却报了成功"
    finally:
        os.remove = real


def test_old_format_migration(ws):
    import session as S
    os.makedirs(S.SESS_DIR, exist_ok=True)
    old = "20200101-000000"
    with open(os.path.join(S.SESS_DIR, old + ".jsonl"), "w", encoding="utf-8") as f:
        f.write('{"role": "user", "content": "旧会话迁移"}\n')
    og = S.open_session(old)
    assert og.slug == ""
    og.save(og.load())                        # 保存时补标题、重命名、删旧文件
    assert not os.path.exists(os.path.join(S.SESS_DIR, old + ".jsonl"))
    assert "__" in os.path.basename(og.path)


def test_a_batch_run_never_becomes_what_continue_lands_on(ws, monkeypatch):
    """`--continue` 只回到人自己的会话 —— 跑批的不许把它抢走。

    `once()` 现在每跑一次留一个会话,而 EXAM 和 benchmarks 一轮能跑几十次,
    它们的时间戳永远比人最后一次对话新。不隔开的话 `--continue` 就变成「续上刚才那个
    benchmark」,而且是**静默**变的:banner 照样印一个会话号,人得读到第二句才发现接错。

    但**不能靠藏起来隔开**:`recall.py` 和 `benchmarks/recall/` 都是平铺 glob
    `sessions/*.jsonl`,挪进子目录这两边就都瞎了 —— 而「跑批的轨迹要能被捞到」
    正是加这个功能的理由。所以隔的只有 `latest_sid` 一处,别的一律照旧。"""
    import glob
    import session as S
    human = S.Session.new()
    human.save([{"role": "user", "content": "人自己开的"}])
    batch = S.Session.new(batch=True)                      # 后写 → mtime 更新
    batch.save([{"role": "user", "content": "跑批开的"}])

    assert S.latest_sid() == human.sid, "--continue 落到跑批会话上了"
    # 平铺可见:换成子目录这一条当场红,而红的地方正是语料抽取会瞎掉的地方
    assert len(glob.glob(os.path.join(S.SESS_DIR, "*.jsonl"))) == 2
    assert len(S.list_sessions()) == 2, "跑批的从 /history 里消失了 —— 它该被看见,只是不该被续上"
    # 点名就打得开:隔开的是「默认落在哪」,不是「够不够得着」
    assert S.open_session(batch.sid).load()[0]["content"] == "跑批开的"
    assert S.resolve(batch.sid) == batch.sid


def test_an_unattended_run_that_crashes_still_leaves_its_transcript(ws, monkeypatch):
    """崩掉的那一份最值钱,所以 `save` 挂在 `finally` 上,不在成功路径上。

    `once()` 的注释写着「无人值守正是『说完成了但没完成』代价最大的地方 ——
    没人在读 transcript」。上一版这条路**一个会话都不开**,所以那句话是真的:
    不是没人读,是没有可读的东西。而崩溃退出(`sys.exit(1)`)恰恰是最需要回读的一次。

    判据钉的是 `finally` 而不是 `save` 存在:把 `save` 挪回成功路径,这条当场红。"""
    import agent as A
    import session as S
    from test_loop import _ui                           # 唯一的跨文件复用,理由同 test_goal
    monkeypatch.setattr(A, "ui", _ui())
    monkeypatch.setattr(A, "make_client", lambda: (object(), "m"))
    monkeypatch.setattr(A, "load_dynamic_tools", lambda: [])

    def boom(*a, **k):
        raise RuntimeError("对面炸了")
    monkeypatch.setattr(A, "agent_turn", boom)

    with pytest.raises(SystemExit) as e:
        A.once("这一轮会崩")
    assert e.value.code == 1

    rows = S.list_sessions()
    assert len(rows) == 1, f"崩掉的那一轮什么都没留下:{rows}"
    assert S.open_session(rows[0][0]).load()[0]["content"] == "这一轮会崩"
