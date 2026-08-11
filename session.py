"""
session.py — Talos 的本地会话存储 (仿 Claude Code:一个会话一个 JSONL,存在项目里)。

文件名是 `<时间戳id>__<标题slug>.jsonl`,标题由第一句 prompt 自动生成,所以
`.talos/sessions/` 里一眼能看出每个会话在聊什么。这是可替换的存储层 —— 想换 SQLite
只改这个文件,`agent.py` 不用动。
"""

from __future__ import annotations

import glob
import json
import os
import re
import time

HOME = os.path.realpath(os.environ.get("TALOS_HOME") or os.path.dirname(os.path.abspath(__file__)))
SESS_DIR = os.path.join(HOME, ".talos", "sessions")   # sessions follow the agent, not the cwd

def _slug(text: str, n: int = 24) -> str:
    """把第一句 prompt 压成文件名安全的短标题(保留中英数字)。"""
    s = re.sub(r"[^\w一-鿿]+", "-", (text or "").strip())[:n].strip("-")
    return s or "chat"

def _first_user(messages: list) -> str:
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return ""

def _parse_name(path: str) -> tuple[str, str]:
    """'<id>__<slug>.jsonl' -> (id, slug);  旧格式 '<id>.jsonl' -> (id, '')。"""
    base = os.path.splitext(os.path.basename(path))[0]
    sid, _sep, slug = base.partition("__")
    return sid, slug

class Session:
    def __init__(self, sid: str, slug: str = ""):
        self.sid = sid
        self.slug = slug

    @classmethod
    def new(cls) -> "Session":
        """秒级时间戳**不保证唯一**,而这里的 id 是会话的全部身份。

        同一秒起两个会话就是同一个 sid。两个文件都写得出来(slug 不同),
        `list_sessions()` 里两条都看得见 —— 但 `_path_for` 是前缀匹配、只返回排序第一个,
        于是 **`/resume` 谁都只能回到第一个,第二个会话永远打不开**。它没丢,是够不着。
        撞了就往后加序号;顺带 `_path_for` 改成优先精确匹配,否则 `-2` 会排在 `__` 前面,
        拿完整 sid 去找反而找到那个带序号的。"""
        base = sid = time.strftime("%Y%m%d-%H%M%S")
        n = 1
        while _path_for(sid) is not None:
            n += 1
            sid = f"{base}-{n}"
        return cls(sid)

    @property
    def path(self) -> str:
        name = f"{self.sid}__{self.slug}" if self.slug else self.sid
        return os.path.join(SESS_DIR, name + ".jsonl")

    def save(self, messages: list) -> None:
        """先写临时文件再原子替换,**最后**才删旧的。

        上一版的顺序是「先 `os.remove(old)`,再 `open(new, "w")`」—— 中间任何一次失败
        (磁盘满、权限、被占用)都是**两个文件都没有**,整个会话没了。而且 `"w"` 是
        先截断再写,写到一半崩掉留下的是半截文件:`load()` 会跳过坏行,所以它不报错,
        它只是**安静地少了后半段**。删除是这个项目里唯一没有撤销的动作,顺序不能反。"""
        old = self.path
        if not self.slug:                                  # 首次保存 → 用第一句 prompt 起名
            self.slug = _slug(_first_user(messages))
        os.makedirs(SESS_DIR, exist_ok=True)
        tmp = self.path + ".tmp"                           # 不以 .jsonl 结尾,不会被任何 glob 捡走
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for m in messages:
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)                     # 原子:要么旧的完整,要么新的完整
        except BaseException:
            # 半成品自己收走。save() 每轮都跑,一个稳定复现的序列化失败会把目录堆满。
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        if old != self.path and os.path.exists(old):       # 起名后文件名变了(含旧格式迁移)
            os.remove(old)

    def load(self) -> list:
        # A single corrupt line must not brick a resume — skip it, don't crash the whole session.
        # Only dict messages survive; a poisoned file can't smuggle in a bare string or list.
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                if not ln.strip():
                    continue
                try:
                    m = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if isinstance(m, dict) and isinstance(m.get("role"), str):
                    out.append(m)
        return out

def _path_for(sid: str) -> str | None:
    # escape: a sid of "*" would otherwise match every session and pick an arbitrary one.
    hits = sorted(glob.glob(os.path.join(SESS_DIR, glob.escape(sid) + "*.jsonl")))
    # 精确的 sid 优先于前缀命中。前缀匹配是给用户敲一半 id 用的,可它同时让**完整**的
    # sid 也变成前缀:`20260811-120000` 会命中 `20260811-120000-2__x.jsonl`,而 `-`(0x2D)
    # 排在 `_`(0x5F)前面 —— 拿完整 id 去找,`sorted()[0]` 给回来的是那个带序号的别人。
    exact = [p for p in hits if _parse_name(p)[0] == sid]
    return (exact or hits or [None])[0]

def open_session(sid: str) -> "Session | None":
    """Bind to an existing session (recovering its slug from the filename)."""
    path = _path_for(sid)
    if not path:
        return None
    return Session(*_parse_name(path))

def list_sessions() -> list:
    """[(sid, mtime, title, n_msgs), ...] newest first."""
    rows = []
    for path in glob.glob(os.path.join(SESS_DIR, "*.jsonl")):
        sid, slug = _parse_name(path)
        first, n = "", 0
        # 坏行**跳过**,不是就此不数。上一版把 `json.loads` 放在 `try` 里、`except` 在整个
        # 循环外面:第 3 行坏掉,后面 96 行一条都不数,`/history` 报 3 条而 `load()` 读回 99 条。
        # 同一个文件在两个地方给出两个数,而这里没有任何报错 —— 它只是**默默少数**。
        # 判据(能不能读)和读法必须跟 `load()` 一致:那边跳过坏行,这边也跳过。
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    if not ln.strip():
                        continue
                    try:
                        m = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(m, dict) or not isinstance(m.get("role"), str):
                        continue
                    n += 1
                    if not first and m["role"] == "user" and isinstance(m.get("content"), str):
                        first = m["content"]
        except OSError:
            pass                                    # 文件读不了:这一条仍然列出来,只是没有摘要
        rows.append((sid, os.path.getmtime(path), first or slug, n))
    return sorted(rows, key=lambda r: r[1], reverse=True)

def resolve(arg: str) -> str | None:
    """Turn a /history index (1-based) or an id prefix into a session id."""
    arg = (arg or "").strip()
    if not arg:
        return None
    rows = list_sessions()
    if arg.isdigit() and 1 <= int(arg) <= len(rows):      # small number = /history index
        return rows[int(arg) - 1][0]
    for sid, *_ in rows:                                  # else match by id prefix (e.g. 20260724)
        if sid.startswith(arg):
            return sid
    return None

def delete(sid: str) -> bool:
    path = _path_for(sid)
    if path and os.path.exists(path):
        os.remove(path)
        return True
    return False

def latest_sid() -> str | None:
    rows = list_sessions()
    return rows[0][0] if rows else None
