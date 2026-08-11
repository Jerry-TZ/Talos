"""会话存储:自动起名 / 往返 / 编号+前缀解析 / 删除 / 旧格式迁移。"""
import os

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
