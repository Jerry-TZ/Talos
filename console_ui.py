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
from rich.markup import escape
from rich.syntax import Syntax
from rich.table import Table
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
                  "[dim]命令[/] [bold]/workspace[/]·[bold]/model[/]·[bold]/mode[/]·[bold]/show[/]·"
                  "[bold]/history[/]·[bold]/tokens[/]·[bold]/reflect[/]·[bold]/compact[/]·[bold]quit[/]\n"
                  "[dim]中断[/] Ctrl+C 停下当前这轮(做过的都留着,可以直接说新要求)")
    console.print("[dim]权限档: plan(只读)·default(每次问)·acceptEdits(自动改文件)·bypass(全放行)   "
                  "自学习: 复杂任务后自动复盘并存 skills/[/]\n")

def read_task(mode: str) -> str:
    return console.input(f"[bold {C_YOU}]你[/] [dim]({mode})[/] › ").strip()

def thinking():
    """上下文管理器:模型思考时转个圈。"""
    return console.status(f"[{C_MODEL}]模型思考中…[/]", spinner="dots")

def took(seconds: float) -> None:
    """调用回来之后报一次耗时。**不要**试着把秒数放进转圈里实时更新 —— 试过,更糟:
    Windows 传统控制台(`console.legacy_windows`)没法可靠地原地重绘,静态文字宽度不变
    时能覆盖住,而秒数宽度会变(9s → 10s → 100s),一变就退化成一行行往下刷屏。

    事后报一次同样解决那个问题:头两次调用就知道这个模型一次要几十秒,之后转圈转到
    远超那个数,你自己就知道该 Ctrl-C 了。而且这条路不碰 Live,任何终端上都不会刷屏。"""
    console.print(f"[dim]  ⏱ {seconds:.0f}s[/]")

def show_tool(name: str, args: dict, result: str, is_error: bool, full: bool = False) -> None:
    mark = "[red]✗[/]" if is_error else f"[{C_CODE}]⚙[/]"
    argstr = escape(str(args) if full else _short(args))
    console.print(f"  {mark} [{C_CODE}]{name}[/][dim]({argstr})[/]")
    if result:
        shown = result if full else _short(result, 240)
        console.print(Text(shown, style=("red" if is_error else "dim")), highlight=False)

def think(text: str) -> None:
    console.print(Panel(Text(text.strip()), border_style="grey42", title="💭 思考", title_align="left"))

def assistant_text(text: str) -> None:
    line = Text("🤖 ", style=C_MODEL)
    line.append(text)
    console.print(line)

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
    elif name == "create_tool":
        # This code is exec'd in-process the moment you approve it, so it is the one preview
        # that must never be clipped — it used to fall through to the 70-char summary below.
        code = str(args.get("code", ""))
        console.print(f"    [dim]造工具 {escape(str(args.get('name', '?')))} ({len(code)} chars) — "
                      f"批准后会在 Talos 进程内立即执行[/]")
        console.print(Syntax(code, "python", theme="ansi_dark",
                             background_color="default", word_wrap=True))
    else:
        console.print(f"    [dim]{_short(args)}[/]")

def ask() -> str:
    return console.input(r"  [yellow]允许? \[y]一次  \[a]本会话都允许该工具  \[N]拒绝(默认) ›[/] ").strip()

def ask_again(typed: str) -> str:
    console.print(f"  [yellow]没看懂 {escape(repr(typed))} —— 打错了?[/]")
    return console.input(r"  [yellow]\[y]允许一次  \[a]本会话都允许  \[n]拒绝  "
                         r"或者直接说你想让它怎么做 ›[/] ").strip()

def ask_yes(prompt: str) -> bool:
    return console.input(f"  [yellow]{escape(prompt)} \\[y/N] ›[/] ").strip().lower() == "y"

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
    console.print(Panel(Text(msg), border_style="red", title=f"⚠ {type(e).__name__}", title_align="left"))
    console.print("[dim]  常见原因: key 错 · 额度用尽(429)· 模型名不对(404)· 网络。可换 TALOS_MODEL / TALOS_PROVIDER 再试。[/]")

def sessions_list(rows) -> None:
    if not rows:
        console.print("[dim]  还没有历史会话。[/]")
        return
    import time as _t
    t = Table(title="历史会话 (.talos/sessions/)", title_style=f"bold {C_MODEL}",
              title_justify="left", border_style="dim")
    t.add_column("#", justify="right"); t.add_column("会话 id"); t.add_column("时间")
    t.add_column("条", justify="right"); t.add_column("标题 / 开头")
    for i, (sid, mtime, first, n) in enumerate(rows, 1):
        head = (first[:36] + "…") if len(first) > 36 else (first or "—")
        t.add_row(str(i), sid, _t.strftime("%m-%d %H:%M", _t.localtime(mtime)), str(n), head)
    console.print(t)
    console.print("[dim]  /resume <#或id> 继续 · /view <#> 看内容 · /delete <#> 删除[/]")

def show_session(messages) -> None:
    if not messages:
        console.print("[dim]  (空会话或不存在)[/]")
        return
    for m in messages:
        role = m.get("role", "?")
        color = {"user": C_YOU, "assistant": C_MODEL, "tool": C_CODE, "system": "dim"}.get(role, "white")
        body = m.get("content") or ("[tool_calls]" if m.get("tool_calls") else "")
        line = Text()
        line.append(f"{role}: ", style=f"bold {color}")
        line.append(str(body))                      # literal — safe from rich markup like [y]
        console.print(line)

def mode_set(mode: str) -> None:
    console.print(f"  [green]→ 已切到 {mode}[/]\n")

def mode_help(current: str, modes) -> None:
    console.print(f"  [dim]当前: {current}。可选: {' · '.join(modes)}[/]\n")

def _short(x, n: int = 70) -> str:
    s = str(x).replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"

def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "\n…(略)"
