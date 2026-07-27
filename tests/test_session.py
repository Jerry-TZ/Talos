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
