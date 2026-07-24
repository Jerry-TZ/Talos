"""
console_ui.py — Talos 的界面 (a Claude-Code-style terminal UI, built on `rich`).

The CORE (agent.py) never prints or reads input itself — it calls the functions
here. So this file *is* the "界面",连到内核只靠这几个函数名。想换皮(甚至换成
网页版)只改这个文件,`agent.py` 一行不用动 —— 跟 Pi 把 `tui` 包和 `agent` 包
分开是同一个思路。

需要 `rich`:  pip install rich
"""

import sys

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

try:
    sys.stdout.reconfigure(encoding="utf-8")   # GBK Windows consoles can't encode emoji (⚙ 🤖) otherwise
except Exception:
    pass

console = Console()

# 每个"角色"一个颜色,和你在网页解释里看到的心智模型一致:
#   模型=紫(说) · 代码=橙(做) · 你=青(人)
C_MODEL = "medium_purple"
C_CODE = "dark_orange"
C_YOU = "turquoise2"

def banner(mode: str, provider: str, model: str) -> None:
    console.print(Panel.fit(
        Text.assemble(("Talos ", f"bold {C_MODEL}"), ("· 最小自学习编程 agent", "dim")),
        border_style=C_MODEL, padding=(0, 2)))
    console.print(f"[dim]模型[/] [bold {C_CODE}]{provider}[/] · {model}    "
                  "[dim]命令[/] [bold]/mode[/]·[bold]/reflect[/]·[bold]/consolidate[/]·[bold]quit[/]")
    console.print("[dim]权限档: plan(只读)·default(每次问)·acceptEdits(自动改文件)·bypass(全放行)   "
                  "自学习: 复杂任务后自动复盘并存 skills/[/]\n")

def read_task(mode: str) -> str:
    return console.input(f"[bold {C_YOU}]你[/] [dim]({mode})[/] › ").strip()

def thinking():
    """上下文管理器:模型思考时转个圈。"""
    return console.status(f"[{C_MODEL}]模型思考中…[/]", spinner="dots")

def show_tool(name: str, args: dict, result: str, is_error: bool) -> None:
    mark = "[red]✗[/]" if is_error else f"[{C_CODE}]⚙[/]"
    console.print(f"  {mark} [{C_CODE}]{name}[/][dim]({_short(args)})[/]")
    if result:
        console.print(Text(_short(result, 240), style=("red" if is_error else "dim")), highlight=False)

def preview(name: str, args: dict) -> None:
    console.print(f"  [bold]● {name}[/] 想执行:")
    if name == "run_bash":
        console.print(Syntax(args.get("command", ""), "bash", theme="ansi_dark",
                             background_color="default", word_wrap=True))
    elif name == "write_file":
        c = args.get("content", "")
        console.print(f"    [dim]写入 {args.get('path', '?')} ({len(c)} chars)[/]")
        console.print(Syntax(_clip(c, 500), "text", theme="ansi_dark", background_color="default"))
    elif name == "edit_file":
        console.print(f"    [dim]改 {args.get('path', '?')}[/]")
        console.print(Text.assemble(("    - ", "red"), (_short(args.get("old", ""), 120), "red")))
        console.print(Text.assemble(("    + ", "green"), (_short(args.get("new", ""), 120), "green")))
    else:
        console.print(f"    [dim]{_short(args)}[/]")

def ask() -> str:
    return console.input("  [yellow]允许? [y]一次  [a]本会话都允许该工具  [N]拒绝(默认) ›[/] ").strip()

def denied(name: str, reason: str) -> None:
    console.print(f"  [red]⛔ {name} 被拒绝 — {reason}[/]")

def answer(text: str) -> None:
    console.print(Panel(Markdown(text or "*(无文本回复)*"),
                        border_style=C_MODEL, title="🤖 Talos", title_align="left"))

def note(text: str) -> None:
    console.print(f"[dim]  {text}[/]")

def error(e) -> None:
    msg = str(e)
    if len(msg) > 320:
        msg = msg[:320] + " …"
    console.print(Panel(msg, border_style="red", title=f"⚠ {type(e).__name__}", title_align="left"))
    console.print("[dim]  常见原因: key 错 · 额度用尽(429)· 模型名不对(404)· 网络。可换 TALOS_MODEL / TALOS_PROVIDER 再试。[/]")

def mode_set(mode: str) -> None:
    console.print(f"  [green]→ 已切到 {mode}[/]\n")

def mode_help(current: str, modes) -> None:
    console.print(f"  [dim]当前: {current}。可选: {' · '.join(modes)}[/]\n")

def _short(x, n: int = 70) -> str:
    s = str(x).replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"

def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "\n…(略)"
