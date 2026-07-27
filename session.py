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
        return cls(time.strftime("%Y%m%d-%H%M%S"))

    @property
    def path(self) -> str:
        name = f"{self.sid}__{self.slug}" if self.slug else self.sid
        return os.path.join(SESS_DIR, name + ".jsonl")

    def save(self, messages: list) -> None:
        old = self.path
        if not self.slug:                                  # 首次保存 → 用第一句 prompt 起名
            self.slug = _slug(_first_user(messages))
        os.makedirs(SESS_DIR, exist_ok=True)
        if old != self.path and os.path.exists(old):       # 起名后文件名变了,清掉旧的(含旧格式迁移)
            os.remove(old)
        with open(self.path, "w", encoding="utf-8") as f:
            for m in messages:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")

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
    hits = glob.glob(os.path.join(SESS_DIR, sid + "*.jsonl"))
    return hits[0] if hits else None

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
        try:
            with open(path, "r", encoding="utf-8") as f:
                for ln in f:
                    if not ln.strip():
                        continue
                    n += 1
                    m = json.loads(ln)
                    if not first and m.get("role") == "user" and isinstance(m.get("content"), str):
                        first = m["content"]
        except Exception:
            pass
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
