"""talos_watch.py — 只读观察窗:看 Talos 正在干什么、记忆网络长什么样。

    python talos_watch.py

**它不 import agent.py,也不 import recall.py。** 只读 `.talos/` 下的 jsonl 和 json,
每两秒刷一次。这不是偷懒,是这个文件存在的前提:

  权限确认框是整个安全边界所在,而今天在那条路径上审出了四个 bug(抢答、按 a 拒绝删除
  不记 denied、不存在的工具也弹框、stdin 清空时机)。**再实现一套输入界面 = 那四个 bug
  有第二次发生的机会**,而全部测试都是针对 console 那一套写的。

  所以这个窗口**只看,不碰**:没有输入框、不弹确认、不写任何文件、不调 agent 的任何函数。
  想换输入界面的话,`console_ui.py` 才是那个位置(agent.py 从不自己 print 或读输入)。

窗口里有 memory.md 的片段和会话正文 —— **别对着它截图外发。**
"""
import collections
import json
import os
import tkinter as tk
from tkinter import ttk

HOME = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HOME, ".talos")
TICK = 2000                                    # 刷新间隔(毫秒)
FEED = 40                                      # 实时区最多显示多少条


def _rows(name, limit=None):
    p = os.path.join(D, name)
    if not os.path.exists(p):
        return []
    out = []
    try:
        with open(p, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    for ln in (lines[-limit:] if limit else lines):
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue                           # 坏行跳过 —— 观察窗不该被一行脏数据弄崩
    return out


def _latest_session():
    d = os.path.join(D, "sessions")
    if not os.path.isdir(d):
        return None, []
    files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl")]
    if not files:
        return None, []
    newest = max(files, key=os.path.getmtime)
    msgs = []
    try:
        with open(newest, encoding="utf-8") as f:
            for ln in f:
                try:
                    m = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(m, dict) and isinstance(m.get("role"), str):
                    msgs.append(m)
    except OSError:
        pass
    return os.path.basename(newest), msgs


def _one_line(s, n=110):
    s = " ".join(str(s or "").split())
    return s[:n] + ("…" if len(s) > n else "")


def _bar(frac, w=22):
    return "█" * max(0, round(frac * w))


class Watch:
    def __init__(self, root):
        root.title("Talos — 只读观察窗")
        root.geometry("1180x720")
        self.root = root
        self.sig = None                        # 上次的数据指纹,没变就不重绘

        bar = ttk.Frame(root, padding=(10, 8))
        bar.pack(fill="x")
        self.head = ttk.Label(bar, text="等待数据…", font=("", 10))
        self.head.pack(side="left")
        ttk.Label(bar, text="只读 · 不写任何文件 · 别截图外发",
                  foreground="#888", font=("", 9)).pack(side="right")

        pane = ttk.PanedWindow(root, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        left = ttk.LabelFrame(pane, text=" 当前会话 ", padding=6)
        self.feed = tk.Text(left, wrap="none", font=("Consolas", 9),
                            bg="#1b1b1b", fg="#ddd", insertwidth=0, height=10)
        sb = ttk.Scrollbar(left, command=self.feed.yview)
        self.feed.configure(yscrollcommand=sb.set, state="disabled")
        sb.pack(side="right", fill="y")
        self.feed.pack(fill="both", expand=True)
        for tag, col in (("user", "#5fd7ff"), ("assistant", "#c9a0ff"),
                         ("tool", "#ffb86c"), ("dim", "#777")):
            self.feed.tag_configure(tag, foreground=col)
        pane.add(left, weight=3)

        right = ttk.Frame(pane)
        self.panels = {}
        for key, title in (("hits", " 记忆命中排行(捞到次数 / 其中给正文) "),
                           ("dist", " 激活分数分布 "),
                           ("cache", " KV 缓存 ")):
            box = ttk.LabelFrame(right, text=title, padding=6)
            t = tk.Text(box, wrap="none", font=("Consolas", 9), height=9,
                        bg="#1b1b1b", fg="#ddd", insertwidth=0)
            t.configure(state="disabled")
            t.pack(fill="both", expand=True)
            box.pack(fill="both", expand=True, pady=(0, 6))
            self.panels[key] = t
        pane.add(right, weight=2)

        self.refresh()

    def _set(self, widget, lines):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        for text, tag in lines:
            widget.insert("end", text + "\n", tag)
        widget.configure(state="disabled")

    def refresh(self):
        try:
            self._draw()
        except Exception as e:                 # noqa: BLE001 — 观察窗崩了不该影响任何东西
            self.head.config(text=f"读数据出错(不影响 Talos):{e}")
        self.root.after(TICK, self.refresh)

    def _draw(self):
        name, msgs = _latest_session()
        trace = _rows("recall_trace.jsonl")
        cache = _rows("cache_trace.jsonl")
        sig = (name, len(msgs), len(trace), len(cache))
        if sig == self.sig:
            return                             # 没有新东西就别重绘,省得光标乱跳
        self.sig = sig

        self.head.config(text=f"会话 {name or '(无)'} · {len(msgs)} 条消息 · "
                              f"{len(trace)} 轮检索 · {len(cache)} 轮记了缓存")

        feed = []
        for m in msgs[-FEED:]:
            role = m.get("role")
            if role == "user":
                feed.append(("你  " + _one_line(m.get("content")), "user"))
            elif role == "assistant":
                for c in (m.get("tool_calls") or []):
                    fn = (c.get("function") or {})
                    feed.append(("  ⚙ " + str(fn.get("name")) + " "
                                 + _one_line(fn.get("arguments"), 80), "tool"))
                if m.get("content"):
                    feed.append(("🤖 " + _one_line(m.get("content")), "assistant"))
            elif role == "tool":
                feed.append(("     ← " + _one_line(m.get("content"), 90), "dim"))
        self._set(self.feed, feed or [("(还没有消息)", "dim")])
        self.feed.see("end")

        picked, bodied, scores = collections.Counter(), collections.Counter(), []
        for r in trace:
            for p in r.get("picked", []):
                k = p.get("key", "")[:46]
                picked[k] += 1
                if p.get("body"):
                    bodied[k] += 1
                if isinstance(p.get("score"), (int, float)):
                    scores.append(p["score"])
        top = max(picked.values()) if picked else 1
        self._set(self.panels["hits"],
                  [(f"{_bar(n / top):<22} {n:>3}{'  正文' + str(bodied[k]) if bodied[k] else '':<7} {k}",
                    "tool" if k.startswith("技能") else "dim")
                   for k, n in picked.most_common(12)] or [("(还没有检索记录)", "dim")])

        if scores:
            b = collections.Counter(min(9, int(s * 10)) for s in scores)
            hi = max(b.values())
            self._set(self.panels["dist"],
                      [(f"{i/10:.1f}–{(i+1)/10:.1f}  {_bar(b.get(i,0)/hi):<22} {b.get(i,0):>4}", "dim")
                       for i in range(10)])
        else:
            self._set(self.panels["dist"], [("(还没有分数)", "dim")])

        if cache:
            out = []
            for label, want in (("system 没变", False), ("system 变了", True)):
                g = [r["hit"] for r in cache
                     if r.get("sys_changed") is want and r.get("hit") is not None]
                if g:
                    g.sort()
                    med = g[len(g) // 2]
                    out.append((f"{label:<12} n={len(g):<3} 中位 {med:.0%}  "
                                f"范围 {min(g):.0%}~{max(g):.0%}", "tool"))
            out.append(("", "dim"))
            out.append(("差值 <10 个百分点、或任一组 n<8 时别下结论", "dim"))
            self._set(self.panels["cache"], out)
        else:
            self._set(self.panels["cache"],
                      [("(还没有数据 —— 正常用几轮再看)", "dim")])


if __name__ == "__main__":
    if not os.path.isdir(D):
        raise SystemExit("没有 .talos/ —— 先正常用几轮。")
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass
    Watch(root)
    root.mainloop()
