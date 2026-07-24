"""
session.py — Talos 的本地会话存储 (仿 Claude Code:一个会话一个 JSONL,存在项目里)。

存在 <cwd>/.talos/sessions/<id>.jsonl,一行一条消息。每轮整体覆盖写(简单、够用;
compaction 会让文件保持小)。这是可替换的一层 —— 想换 SQLite/远端只改这个文件,
`agent.py` 不用动(跟 console_ui.py 是界面层一样,这里是存储层)。
"""

from __future__ import annotations

import glob
import json
import os
import time

SESS_DIR = os.path.join(".talos", "sessions")

class Session:
    def __init__(self, sid: str):
        self.sid = sid
        self.path = os.path.join(SESS_DIR, sid + ".jsonl")

    @classmethod
    def new(cls) -> "Session":
        return cls(time.strftime("%Y%m%d-%H%M%S"))

    def save(self, messages: list) -> None:
        os.makedirs(SESS_DIR, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            for m in messages:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")

    def load(self) -> list:
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding="utf-8") as f:
            return [json.loads(ln) for ln in f if ln.strip()]

def list_sessions() -> list:
    """[(sid, mtime, first_user_text, n_msgs), ...] newest first."""
    rows = []
    for path in glob.glob(os.path.join(SESS_DIR, "*.jsonl")):
        sid = os.path.splitext(os.path.basename(path))[0]
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
        rows.append((sid, os.path.getmtime(path), first, n))
    return sorted(rows, key=lambda r: r[1], reverse=True)

def latest_sid() -> str | None:
    rows = list_sessions()
    return rows[0][0] if rows else None
