"""
Talos — a minimal, self-extending coding agent.  [multi-provider]

The core loop never changed:

    你 ──▶ 模型(只是"说") ──▶ 代码执行工具("做") ──▶ 结果回传 ──▶ … ──▶ 回答
                     └──────────────── 循环,直到模型不再要工具 ─────────────┘

Talos talks to any model through the **OpenAI-compatible** chat API, so ONE code
path drives Claude / GPT / Gemini / DeepSeek / GLM / Kimi. Pick a provider with
the TALOS_PROVIDER env var (default "claude"); each reads its own *_API_KEY.

  权限分级 (Claude-Code-style tiers), /mode 切换:  plan · default · acceptEdits · bypass
  自学习: 复杂任务后 reflect 写 skills/*.md + memory.md,下次自动加载 (见 SELF_LEARNING.md)
  界面: 终端 UI 在 console_ui.py (rich);内核不直接 print。

⚠️  SAFETY: the permission gate is a CHECK, not a sandbox. Once you allow a
    run_bash it runs on your real machine. Real isolation = a later step.
"""

from __future__ import annotations

import asyncio
import glob
import importlib.util
import inspect
import json
import os
import platform
import sys
import time

def _load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from a .env file into the environment (real env vars win),
    so you set provider + key ONCE in .env instead of every shell session."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

_load_dotenv()   # BEFORE reading TALOS_PROVIDER / keys below

# ── providers: 大家都讲 OpenAI 兼容 API,只有 base_url + 模型名不同 ────────────────
# base_url 稳定;模型名常变 —— 用 TALOS_MODEL 环境变量覆盖成你有权限的那个。
PROVIDERS = {
    #  name        key 环境变量          base_url (None = OpenAI 官方)                                默认模型
    "claude":   ("ANTHROPIC_API_KEY", "https://api.anthropic.com/v1/",                            "claude-haiku-4-5-20251001"),
    "openai":   ("OPENAI_API_KEY",    None,                                                        "gpt-4o-mini"),
    "gemini":   ("GEMINI_API_KEY",    "https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-2.0-flash"),
    "deepseek": ("DEEPSEEK_API_KEY",  "https://api.deepseek.com/v1",                               "deepseek-chat"),
    "glm":      ("ZHIPUAI_API_KEY",   "https://open.bigmodel.cn/api/paas/v4",                      "glm-4.7-flash"),
    "kimi":     ("MOONSHOT_API_KEY",  "https://api.moonshot.cn/v1",                                "moonshot-v1-8k"),
}
PROVIDER = os.environ.get("TALOS_PROVIDER", "claude").lower()

SYSTEM = (
    "You are Talos, a minimal coding agent working inside the user's project "
    "directory. Use the tools to read, write, and edit files and to run shell "
    "commands. Prefer small, verifiable steps. Some actions need the user's "
    "approval and may be denied — if denied, read the reason and either adjust "
    "your approach or ask the user. When the task is done, reply with a one- or "
    "two-sentence summary of what you did.\n\n"
    "Read as little as possible: everything you read is re-sent to the model on "
    "every later step of the task, so an early full-file read costs many times its "
    "size. Never read a file just to feed it to a tool that takes a path — pass the "
    "path. Read only the part you must understand yourself, using offset/limit.\n\n"
    "Before you start, look at the skill list below. If any description is even close to "
    "the task, read_file its body FIRST — the descriptions are one-liners; the facts that "
    "save you a dozen steps are in the body.\n\n"
    "Never claim you are done on the grounds that nothing errored. Actually call what you "
    "built and check the VALUES: an empty string, N/A, 0, or an empty list in a field the "
    "user asked for is a failure, not a pass. Go back and fix it. If you cannot get real "
    "values, say which fields are still wrong — do not report success."
)

def _env_block() -> str:
    """Tell the model what machine it is on — it guessed bash on Windows and burned steps."""
    sh = "cmd.exe (NOT bash — no ls/pwd/grep/source/&&-across-lines)" if os.name == "nt" else "sh"
    return ("\n\n<environment>\n"
            f"OS: {platform.system()} {platform.release()}\n"
            f"Shell used by run_bash: {sh}\n"
            f"Working directory (already correct — never cd into the project): {WORKSPACE}\n"
            f"Python: {sys.executable}\n"
            "run_bash commands must be ONE line. For multi-line code, write_file a .py file "
            "and run that file instead.\n</environment>")

REFLECT_AFTER = 5    # after a task with >= this many tool calls, auto-run a learning pass
COMPACT_AT = 30000   # ponytail: char-count proxy for tokens; compact history past this (add tiktoken for precision)
MAX_STEPS = int(os.environ.get("TALOS_MAX_STEPS", "25"))   # loop safety cap (guards against 空转)

ui = None            # 界面 handle, set by repl(); kept out of module scope so --selfcheck is dep-free
_RUNTIME = {}        # live client/model/state (+ subagent depth), set in agent_turn so tools like
                     # spawn_subagent can reach the loop without threading them through run_tool.

def make_client():
    """Build an OpenAI-compatible client for the selected provider. Returns (client, model)."""
    if PROVIDER not in PROVIDERS:
        raise SystemExit(f"未知 TALOS_PROVIDER: {PROVIDER}。可选: {', '.join(PROVIDERS)}")
    key_env, base_url, default_model = PROVIDERS[PROVIDER]
    key = os.environ.get(key_env)
    if not key:
        raise SystemExit(f"缺少环境变量 {key_env} —— 设置你的 {PROVIDER} API key(或换 TALOS_PROVIDER)")
    from openai import OpenAI                       # lazy: only needed to actually talk to a model
    return OpenAI(api_key=key, base_url=base_url), (os.environ.get("TALOS_MODEL") or default_model)

# ── tools: just plain Python functions ────────────────────────────────────────
# 工作目录限制:文件工具只能在 WORKSPACE 内活动(默认当前目录,TALOS_WORKSPACE 可改)。
# ⚠️ 只锁得住文件工具;run_bash 里一条 cd 仍能出去 —— 彻底隔离要 Step 3 沙箱。

WORKSPACE = os.path.realpath(os.environ.get("TALOS_WORKSPACE", "."))

def _in_workspace(path: str) -> str:
    full = os.path.realpath(path)
    try:
        inside = os.path.commonpath([full, WORKSPACE]) == WORKSPACE
    except ValueError:                        # 不同盘符(Windows)= 肯定在外面
        inside = False
    if not inside:
        raise ValueError(f"越界:{path} 不在工作目录内({WORKSPACE})")
    return full

READ_MAX_LINES = 250   # cap lines returned to the model (token saver); page with offset/limit
BASH_MAX_CHARS = 4000  # cap run_bash output sent to the model

def _read_full(path: str) -> str:
    """Read a text file, tolerating what Windows tools actually produce.

    PowerShell's `>` writes UTF-16LE by default, and plenty of editors add a
    UTF-8 BOM — decode those properly rather than crashing (or worse, handing
    edit_file mojibake it would then write back over the original)."""
    with open(_in_workspace(path), "rb") as f:
        b = f.read()
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):        # UTF-16 first: it is full of NUL bytes
        return b.decode("utf-16", errors="replace")
    if b"\x00" in b[:8192]:
        # Decoding this would hand back mojibake the model reads as content — it once
        # "verified" a .docx that way and never noticed it had read nothing.
        raise ValueError(
            f"{path} 是二进制文件,read_file 读不了。用对应的库在 run_bash 里读,例如 "
            "docx: `python -c \"import docx; print(docx.Document('f.docx').tables[0].rows[0].cells[0].text)\"`;"
            "xlsx 用 openpyxl、图片用 PIL。想改它也不能用 edit_file —— 用库写。")
    return b.decode("utf-8-sig", errors="replace")

def read_file(path: str, offset: int = 0, limit=None) -> str:
    lines = _read_full(path).splitlines(keepends=True)
    total = len(lines)
    start = max(0, offset)
    end = min(total, start + limit) if limit is not None else min(total, start + READ_MAX_LINES)
    out = "".join(lines[start:end])
    if start > 0 or end < total:
        out += f"\n…(显示第 {start + 1}-{end} 行 / 共 {total} 行;用 offset/limit 翻页,或 grep 定位)"
    return out

def write_file(path: str, content: str) -> str:
    full = _in_workspace(path)
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    return f"wrote {len(content)} chars to {path}"

def edit_file(path: str, old: str, new: str) -> str:
    text = _read_full(path)                             # FULL read — a truncated read would corrupt the edit
    n = text.count(old)
    if n == 0:
        raise ValueError("`old` string not found in file")
    if n > 1:
        raise ValueError(f"`old` string is not unique ({n} matches) — add more surrounding context")
    write_file(path, text.replace(old, new, 1))
    return f"edited {path}"

def run_bash(command: str) -> str:
    # ponytail: runs on the HOST, unsandboxed. The permission gate is the guard
    # for now; real isolation (WSL2/Docker) is a later step, if ever needed.
    import subprocess
    if os.name == "nt" and "\n" in command:
        # cmd.exe runs only the first line and exits 0 with no output — silent, and the
        # model reads that as success. Refuse loudly instead.
        raise ValueError("Windows 的 cmd 只会执行多行命令的第一行,剩下的被丢掉(而且不报错)。"
                         "请把命令写成一行;多行代码先 write_file 存成 .py,再 `python 那个文件`。")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")     # else a GBK console kills any child that prints 中文
    # cwd=WORKSPACE keeps relative paths consistent with the file tools' jail. It is NOT a
    # boundary: the command can still cd out or use absolute paths. Only a sandbox fixes that.
    p = subprocess.run(command, shell=True, capture_output=True, text=True, cwd=WORKSPACE,
                       encoding="utf-8", errors="replace", timeout=120, env=env)
    out = (p.stdout + p.stderr).strip() or f"(exit {p.returncode}, no output)"
    if len(out) > BASH_MAX_CHARS:
        out = out[:BASH_MAX_CHARS] + f"\n…(输出共 {len(out)} 字符,已截断到 {BASH_MAX_CHARS};用更精确的命令/grep 缩小范围)"
    return out

# ── self-extension: the agent writes NEW tools for itself (Pi-style) ───────────
# ⚠️ create_tool exec()s model-written code IN THIS PROCESS — scarier than
# run_bash's subprocess. It's gated (perm-class "bash"); real isolation = Step 3.

TOOLS_DIR = "tools"      # agent-written tools live here; auto-loaded on startup, so they persist

def _load_tool(path: str) -> str:
    """Import a tool file and register it. File must define TOOL(dict) + run(args)->str."""
    name = os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location("talos_tool_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                       # runs the file -> defines TOOL + run
    meta = getattr(mod, "TOOL", None)
    if not isinstance(meta, dict) or not callable(getattr(mod, "run", None)):
        raise ValueError(                              # actionable: a bare AttributeError taught the model nothing
            "工具文件缺少必需的两样东西。必须在**模块最外层**(不缩进)定义:\n"
            "TOOL = {'description': '一句话说明何时用', 'parameters': {'参数名': {'type': 'string'}}, "
            "'required': ['参数名']}\n"
            "def run(args: dict) -> str: ...\n"
            "请补上后重新调用 create_tool。")
    TOOLS[name] = (mod.run, meta.get("parameters", {}), meta.get("required", []),
                   meta["description"], "bash")
    return name

def create_tool(name: str, code: str) -> str:
    path = os.path.join(TOOLS_DIR, name + ".py")
    write_file(path, code)
    try:
        _load_tool(path)                               # load NOW so it's callable this turn
    except Exception:
        os.remove(path)                                # don't leave a broken tool to fail on every startup
        raise
    return f"工具 {name} 已创建并加载,现在可以直接调用它"

def load_dynamic_tools() -> list:
    """Load all previously-created tools on startup (skip broken ones)."""
    out = []
    for path in sorted(glob.glob(os.path.join(TOOLS_DIR, "*.py"))):
        try:
            out.append(_load_tool(path))
        except Exception:
            pass
    return out

# ── delegation: spawn a sub-agent (广度, not 深度) ─────────────────────────────
# A sub-agent is a fresh agent_turn with its OWN isolated context — only its final
# answer returns, so the parent's context stays clean. Reuses agent_turn, like
# reflect/consolidate. Same tools + permission state as the parent.

def spawn_subagent(task: str) -> str:
    depth = _RUNTIME.get("depth", 0)
    if depth >= 2:
        return "error: 子agent 嵌套太深了,这个子任务请自己直接做,别再派子agent"
    if ui is not None:
        ui.note("↳ 派出子agent: " + (task[:60] + "…" if len(task) > 60 else task))
    _RUNTIME["depth"] = depth + 1
    try:
        return agent_turn(_RUNTIME["client"], _RUNTIME["model"],
                          [{"role": "user", "content": task}], _RUNTIME["state"])
    finally:
        _RUNTIME["depth"] = depth

# Registry: name -> (fn, input-properties, required-keys, description, PERM-CLASS).
# perm-class is one of: "read" (never gated) | "edit" (write/edit files) | "bash".
TOOLS = {
    "read_file": (
        lambda a: read_file(a["path"], a.get("offset", 0), a.get("limit")),
        {"path": {"type": "string"},
         "offset": {"type": "integer", "description": "起始行(0 起),用于分页"},
         "limit": {"type": "integer", "description": "读多少行;省略则用默认上限"}},
        ["path"],
        "Read a text file. Long files are truncated — use offset/limit to page to the part you need.", "read",
    ),
    "write_file": (
        lambda a: write_file(a["path"], a["content"]),
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"],
        "Create or overwrite a text file with the given content.", "edit",
    ),
    "edit_file": (
        lambda a: edit_file(a["path"], a["old"], a["new"]),
        {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
        ["path", "old", "new"],
        "Replace exactly one unique occurrence of `old` with `new` in a file.", "edit",
    ),
    "run_bash": (
        lambda a: run_bash(a["command"]),
        {"command": {"type": "string"}}, ["command"],
        "Run a shell command and return its combined stdout+stderr.", "bash",
    ),
    "create_tool": (
        lambda a: create_tool(a["name"], a["code"]),
        {"name": {"type": "string"}, "code": {"type": "string"}}, ["name", "code"],
        "Create a NEW tool for yourself when no built-in tool fits the job. `code` is a full Python "
        "file that defines TOOL = {'description': str, 'parameters': {<json-schema properties>}, "
        "'required': [<param names>]} and def run(args: dict) -> str. After creating it, call the "
        "new tool by its `name` on your next step. IMPORTANT: parameters must be paths or short "
        "identifiers — NEVER a parameter that carries file contents or a large blob. Have `run` open "
        "the file itself. Passing content through a tool argument means you retype the whole file as "
        "output tokens, which is the single most expensive mistake you can make.", "bash",
    ),
    "spawn_subagent": (
        lambda a: spawn_subagent(a["task"]),
        {"task": {"type": "string"}}, ["task"],
        "Delegate a self-contained subtask to a fresh sub-agent that has its own isolated context and "
        "the same tools; only its final result returns to you (keeps your own context clean). Give a "
        "complete, standalone task, e.g. 'read agent.py and report how the permission gate works'.",
        "read",   # not itself a side effect: the subagent shares `state`, so each of ITS tool calls is gated
    ),
}

def tool_specs() -> list[dict]:
    """OpenAI-compatible function-tool schema."""
    return [
        {"type": "function", "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        }}
        for name, (_fn, props, required, desc, _cls) in TOOLS.items()
    ]

def run_tool(name: str, args: dict) -> tuple[str, bool]:
    if name not in TOOLS:                       # a bare KeyError told nobody anything
        return f"error: 没有名叫 {name} 的工具。现有工具:{', '.join(sorted(TOOLS))}", True
    try:
        out = TOOLS[name][0](args)
        if inspect.iscoroutine(out):            # a self-written tool may use an async lib (playwright, httpx)
            out = asyncio.run(out)              # — run it rather than handing back a coroutine repr
        return str(out), False
    except Exception as e:                      # tool errors go back to the model, not crash
        return f"error: {e}", True

# ── learned knowledge: memory (facts) + skills (procedures) ───────────────────
# Learning = notes the agent writes for itself, read back later. Not training.

SKILLS_DIR = "skills"
MEMORY_FILE = "memory.md"

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a leading `---`-fenced block from the body. Minimal `key: value`."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            meta = {}
            for line in text[3:end].strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            return meta, text[end + 4:].lstrip("\n")
    return {}, text

def retrieve() -> str:
    """Build the 'what I've learned' block injected into the system prompt.
    memory.md loads in FULL (small, always-on). Skills contribute only their
    one-line description — the model reads a skill's body on demand with
    read_file. So context cost = memory + N one-liners, NOT N full skills."""
    parts = []
    if os.path.exists(MEMORY_FILE):
        mem = _read_full(MEMORY_FILE).strip()
        if mem:
            parts.append("# 记住的事实 (memory.md)\n" + mem)
    skills = sorted(glob.glob(os.path.join(SKILLS_DIR, "*.md")))
    if skills:
        lines = []
        for path in skills:
            meta, _ = _parse_frontmatter(_read_full(path))
            name = meta.get("name") or os.path.splitext(os.path.basename(path))[0]
            lines.append(f"- {name} — {meta.get('description', '')}  (需要时 read_file `{path}` 看步骤)")
        parts.append("# 可用技能 (skills/) — 相关时才读正文\n" + "\n".join(lines))
    return "\n\n".join(parts)

# ── permission gate (modeled on Claude Code) ──────────────────────────────────

MODES = ("plan", "default", "acceptEdits", "bypass")

def _policy(mode: str, cls: str, name: str, allow: set) -> str:
    """Pure decision — 'allow' | 'deny' | 'ask'. No I/O, so it's unit-testable."""
    if cls == "read":                               return "allow"   # reads never gated
    if mode == "bypass":                            return "allow"   # yolo
    if mode == "plan":                              return "deny"    # read-only
    if mode == "acceptEdits" and cls == "edit":     return "allow"   # auto-accept file edits
    if name in allow:                               return "allow"   # user chose "allow this tool"
    return "ask"

def check_permission(state: dict, cls: str, name: str, args: dict) -> tuple[bool, str]:
    """Decide + (if needed) prompt. Returns (allowed, reason-when-denied)."""
    decision = _policy(state["mode"], cls, name, state["allow"])
    if decision == "allow":
        return True, ""
    if decision == "deny":
        return False, f"{state['mode']} 模式禁止 {cls} 操作"
    # decision == "ask"
    ui.preview(name, args)
    try:
        ans = ui.ask()
    except (KeyboardInterrupt, EOFError):
        return False, "用户中断"
    low = ans.lower()
    if low == "a":
        state["allow"].add(name)
        return True, ""
    if low == "y":
        return True, ""
    if low in ("", "n"):
        return False, "用户拒绝了这次调用"
    return False, f"用户拒绝,并说:{ans}"          # like Claude Code's "No, and tell it what to do"

# ── the loop (OpenAI-compatible chat format) ──────────────────────────────────

def _reasoning(msg) -> str:
    """A model's thinking, IF the provider returns it (reasoning models expose reasoning_content)."""
    for a in ("reasoning_content", "reasoning"):
        v = getattr(msg, a, None)
        if v:
            return str(v)
    extra = getattr(msg, "model_extra", None) or {}
    return str(extra.get("reasoning_content") or extra.get("reasoning") or "")

def _usage(resp):
    """Token usage from a response (0s if the provider doesn't report it). -> (in, out, cached)."""
    u = getattr(resp, "usage", None)
    if not u:
        return (0, 0, 0)
    d = getattr(u, "prompt_tokens_details", None)
    cached = (getattr(d, "cached_tokens", 0) or 0) if d is not None else 0
    return (getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0, cached)

def _chat(client, **kwargs):
    """Call the model, retrying briefly on rate-limit / 'busy' / transient errors.
    Free tiers (esp. glm-4.7-flash) get congested — '当前模型用户多' is just a busy signal."""
    for attempt in range(3):
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as e:
            s = str(e).lower()
            transient = (any(k in s for k in ("429", "rate limit", "ratelimit", "timeout",
                        "overload", "too many", "busy", "503", "502", "并发", "繁忙"))
                        or "用户多" in str(e))
            if attempt < 2 and transient:
                if ui is not None:
                    ui.note(f"模型繁忙,{2 ** attempt}s 后重试 ({attempt + 1}/2)…")
                time.sleep(2 ** attempt)
                continue
            raise

def agent_turn(client, model: str, messages: list, state: dict) -> str:
    """Drive one user request to completion, looping over tool calls."""
    _RUNTIME.update(client=client, model=model, state=state)   # let tools (spawn_subagent) reach the loop
    learned = retrieve()                               # always-on: memory + skill descriptions
    query = next((m["content"] for m in reversed(messages)
                  if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
    recalled = ""
    if query:
        try:
            import recall                              # 联想回忆:按当前任务捞相关记忆(spreading activation)
            recalled = recall.recall(query)
        except Exception:
            recalled = ""
    system = (SYSTEM + _env_block()                # stable -> stays inside the cached prefix
              + ("\n\n" + learned if learned else "")
              + ("\n\n" + recalled if recalled else ""))
    state.setdefault("tok", {"in": 0, "out": 0, "cached": 0, "steps": 0, "calls": 0})
    base = dict(state["tok"])    # a subagent shares `state`, so measure this turn as end-minus-start:
    steps = 0                    # nested work then lands in the caller's total instead of being lost
    while True:
        steps += 1
        if steps > MAX_STEPS:                          # safety cap — don't spin forever
            state["last_tok"] = {k: state["tok"][k] - v for k, v in base.items()}
            state["last_calls"] = state["last_tok"]["calls"]
            state["capped"] = True                     # a flailing turn must not teach itself its own workarounds
            return (f"(已到 {MAX_STEPS} 步上限,停下了。历史都还在 —— 直接说「继续」就接着做,"
                    f"或者拆小些重来;想放宽上限设 TALOS_MAX_STEPS。)")
        if steps == MAX_STEPS - 4:                     # let it land the plane instead of being cut mid-flight
            messages.append({"role": "user", "content":
                             f"[系统] 还剩 4 步就到上限了。如果快好了就收尾;如果这条路走不通,"
                             f"别再试变体了 —— 直接说清卡在哪、你试过什么。"})
        with ui.thinking():
            resp = _chat(client, model=model,
                         messages=[{"role": "system", "content": system}] + messages,
                         tools=tool_specs())
        state["tok"]["steps"] += 1
        for _k, _v in zip(("in", "out", "cached"), _usage(resp)):
            state["tok"][_k] += _v
        msg = resp.choices[0].message
        view = state.get("view", "normal")
        if view in ("verbose", "transcript"):
            r = _reasoning(msg)
            if r:
                ui.think(r)                                # show the model's thinking, if any
        if view == "transcript" and msg.content and (msg.tool_calls or []):
            ui.assistant_text(msg.content)                 # inline commentary alongside tool calls
        tool_calls = msg.tool_calls or []

        entry = {"role": "assistant", "content": msg.content or ""}   # record the assistant turn
        if tool_calls:
            entry["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in tool_calls
            ]
        messages.append(entry)

        if not tool_calls:                              # no tool wanted -> final answer
            state["last_tok"] = {k: state["tok"][k] - v for k, v in base.items()}
            state["last_calls"] = state["last_tok"]["calls"]
            return msg.content or ""

        for c in tool_calls:
            state["tok"]["calls"] += 1
            name = c.function.name
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            cls = TOOLS[name][4] if name in TOOLS else "bash"
            allowed, reason = check_permission(state, cls, name, args)
            if not allowed:
                out, is_error = f"permission denied: {reason}", True
                if view != "quiet":
                    ui.denied(name, reason)
            elif name not in TOOLS:
                out, is_error = f"error: unknown tool {name}", True
            else:
                out, is_error = run_tool(name, args)
                if view != "quiet":
                    ui.show_tool(name, args, out, is_error, full=(view in ("verbose", "transcript")))
            messages.append({"role": "tool", "tool_call_id": c.id, "content": out})

# ── the learning write-back: reflect (save) + consolidate (tidy) ──────────────
# Both are just another agent_turn with a special prompt — so saving reuses the
# same gated write_file/edit_file. That's why self-learning is mostly prompts.

REFLECT_PROMPT = (
    "复盘刚才的对话。如果有**可复用的做法**值得留下,用 write_file 存成 "
    "skills/<kebab-name>.md:开头用 --- 包住 frontmatter(name、description=何时用),"
    "再写步骤。如果有关于用户/项目的**持久事实或教训**,用 edit_file 往 memory.md 追加"
    "一行(没有该文件就 write_file 新建)。只存下次真能帮上忙的,一次性的别存。没有值得"
    "存的就直说、别写文件。\n"
    "同名技能**已经存在时,先 read_file 读它,再用 edit_file 改**,绝不许 write_file 盖掉 —— "
    "旧版里可能有你这次没遇到、上次辛苦踩出来的经验,盖了就没了。\n"
    "技能要小而精、要能复用。只对这一个任务有用的(比如「验证 xx 工具」)不许写成技能。"
    "拿不准就不写 —— 匹配不上的技能只会白占上下文。\n"
    "复盘前先把这次产生的临时文件删掉(调试脚本、验证脚本、一次性的中间产物),"
    "用 run_bash `del`。工作目录是用户的,别留垃圾。\n"
    "技能正文必须有一段 `## 何时用`,列 3~5 条**具体场景**,照着用户会怎么开口去写"
    "(例:「要拿 B站视频的播放量/UP主」「工具报错说字段不存在」),别写「处理 API 相关任务」"
    "这种概括。检索是按正文关键词匹配的 —— 场景写得越具体,下次越捞得出来。\n"
    "写技能前先自查:**只写你亲眼看着成功的步骤**。刚才报过错的命令、你猜的、没验证的,"
    "一律不许写进步骤 —— 技能会注入到以后每一轮,一条错步骤会被反复照做。\n"
    "写 memory.md 前先自查,以下一律不许写:(1) 你自己刚才的做法 —— 你用 `python -c` 测试"
    "不等于用户习惯这么做;(2) 从一两次任务归纳出的'用户喜欢…';(3) 用户的错别字、手误、"
    "临时输入。只写用户**明确说出口**的偏好、约束、纠正。拿不准就别写 —— 错的记忆会被注入"
    "到以后每一轮,比没有记忆更糟。"
)
CONSOLIDATE_PROMPT = (
    "用 run_bash `ls skills` 列出所有技能,再 read_file 逐个看。合并重复、删掉太窄或"
    "没用的(用 run_bash 删文件),让每条 description 更好匹配。保持这套技能小而精。"
)

def _seal(messages: list) -> None:
    """Make history valid again after a turn died mid-flight, WITHOUT discarding the work.

    An assistant message carrying tool_calls must be followed by a result for every call,
    or the next request 400s. Rolling the whole turn back satisfies that but throws away
    everything the turn accomplished — and then "继续" has nothing to continue from."""
    last = next((i for i in range(len(messages) - 1, -1, -1)
                 if messages[i].get("role") == "assistant" and messages[i].get("tool_calls")), None)
    if last is None:
        return
    done = {m.get("tool_call_id") for m in messages[last + 1:] if m.get("role") == "tool"}
    for c in messages[last]["tool_calls"]:
        if c["id"] not in done:
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": "(这一步被中断了 — 没有结果)"})

def reflect(client, model: str, messages: list, state: dict) -> str:
    """One extra learning turn — saves skills/facts, reusing the gated tools.
    Runs on a COPY of messages so the reflection prompt never pollutes memory."""
    return agent_turn(client, model, messages + [{"role": "user", "content": REFLECT_PROMPT}], state)

def consolidate(client, model: str, state: dict) -> str:
    return agent_turn(client, model, [{"role": "user", "content": CONSOLIDATE_PROMPT}], state)

# ── context compression (仿 Claude Code 的 auto-compact) ───────────────────────

def _ctx_chars(messages: list) -> int:
    return sum(len(str(m.get("content") or "")) for m in messages)

def maybe_compact(client, model: str, messages: list, force: bool = False) -> list:
    """History got long? Replace it with a summary + a continue marker. Returns the new list."""
    if len(messages) < 3 or (not force and _ctx_chars(messages) < COMPACT_AT):
        return messages
    clean = [{"role": m["role"], "content": m["content"]} for m in messages
             if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
             and m["content"] and "tool_calls" not in m]
    with ui.thinking():
        resp = _chat(client, model=model, messages=(
            [{"role": "system", "content": "你在压缩一段编程 agent 的对话历史。产出一段简报,保留:"
              "当前任务、已做的决定、改动过的文件、还没完成的线索。简洁、要点式。"}]
            + clean + [{"role": "user", "content": "把以上压成简报。"}]))
    summary = resp.choices[0].message.content or "(空)"
    ui.note(f"🗜 上下文已压缩({len(messages)} 条 → 2 条)")
    return [{"role": "user", "content": "【早前对话的压缩摘要】\n" + summary},
            {"role": "assistant", "content": "了解,以上是之前的进展,我们继续。"}]

def _prune_old_tool_results(messages: list, keep: int = 8) -> None:
    """Stub OLD, bulky tool outputs in place so they stop getting resent every step (token saver).
    Keeps the last `keep` messages untouched. Note: this rewrites saved history (/view shows stubs)."""
    old = messages[:-keep] if len(messages) > keep else []
    for m in old:
        if (m.get("role") == "tool" and isinstance(m.get("content"), str)
                and len(m["content"]) > 600 and not m["content"].startswith("[已省略")):
            m["content"] = f"[已省略工具输出,{len(m['content'])} 字符 — 需要就重新读]"

_CORRECTION_MARKERS = ("不对", "错了", "搞错", "不是这样", "不应该", "不该", "重来", "别这样",
                       "不是这个", "写错", "wrong", "incorrect", "not what", "should have", "instead")
def _is_correction(text: str) -> bool:
    """用户在纠正吗?—— 最强的'该记下教训'信号。关键词启发式;误判 = 多复盘一次,无害。"""
    t = (text or "").lower()
    return any(m in t for m in _CORRECTION_MARKERS)

def once(task: str, mode: str = "bypass") -> str:
    """Run one task and exit — the non-interactive twin of repl(), for scripting and benchmarks.

    Defaults to bypass because nobody is at the keyboard to answer a permission prompt."""
    global ui
    import console_ui as ui
    client, model = make_client()
    load_dynamic_tools()
    state = {"mode": mode, "allow": set(), "view": "normal"}
    messages: list = [{"role": "user", "content": task}]
    try:
        result = agent_turn(client, model, messages, state)
    except Exception as e:                        # unattended: report and exit non-zero, don't traceback
        ui.error(e)
        sys.exit(1)
    ui.answer(result)
    t = state.get("last_tok") or {}
    if t.get("in") or t.get("out"):
        ui.note(f"🎫 {t.get('steps', 1)} 次调用 · {t['in']}+{t['out']}={t['in'] + t['out']} tok"
                + (f" · 缓存命中 {t['cached']}" if t.get("cached") else ""))
    return result

def repl(resume=None) -> None:
    global ui
    import console_ui as ui              # the 界面 (needs rich); lazy so --selfcheck stays dep-free
    import session as S                  # local conversation storage (Claude-Code-style)
    client, model = make_client()        # OpenAI-compatible client for the chosen provider
    if resume is not None:               # --continue (latest) / --resume <id>
        sid = S.latest_sid() if resume is True else S.resolve(resume)
        sess = S.open_session(sid) if sid else S.Session.new()
        messages: list = sess.load()
    else:
        sess = S.Session.new()
        messages = []
    state = {"mode": "default", "allow": set(), "view": "normal"}   # ← permission + display state

    ui.banner(state["mode"], PROVIDER, model)
    ui.note(f"会话 {sess.sid}" + (f" · 续上 {len(messages)} 条消息" if messages else " · 存于 .talos/sessions/"))
    made = load_dynamic_tools()                        # re-load tools the agent built in past sessions
    if made:
        ui.note(f"已加载 {len(made)} 个自建工具: {', '.join(made)}")
    while True:
        try:
            task = ui.read_task(state["mode"])
        except (EOFError, KeyboardInterrupt):
            break
        if task in ("quit", "exit"):
            break
        if not task:
            continue
        if task.startswith("/mode"):
            arg = task[5:].strip()
            if arg in MODES:
                state["mode"] = arg
                ui.mode_set(arg)
            else:
                ui.mode_help(state["mode"], MODES)
            continue
        if task.startswith("/show"):
            arg = task[5:].strip()
            if arg in ("quiet", "normal", "verbose", "transcript"):
                state["view"] = arg
                ui.note(f"显示模式 → {arg}")
            else:
                ui.note(f"当前显示: {state.get('view', 'normal')}。可选: quiet · normal · verbose · transcript")
            continue
        if task == "/reflect":
            reflect(client, model, messages, state)
            continue
        if task == "/consolidate":
            ui.note("🧹 整理技能中…")
            consolidate(client, model, state)
            continue
        if task == "/history":
            ui.sessions_list(S.list_sessions())
            continue
        if task == "/compact":
            try:
                messages[:] = maybe_compact(client, model, messages, force=True)
                sess.save(messages)
            except Exception as e:
                ui.error(e)
            continue
        if task == "/tokens":
            t = state.get("tok", {"in": 0, "out": 0, "cached": 0})
            ui.note(f"本会话累计:{t.get('steps', 0)} 次调用 · 输入 {t['in']} + 输出 {t['out']}"
                    f" = {t['in'] + t['out']} tok"
                    + (f",缓存命中 {t['cached']}" if t.get("cached") else ""))
            continue
        if task.startswith("/recall"):                 # 看联想回忆的激活分数
            try:
                import recall
                rows = recall.explain(task[7:].strip())
            except Exception as e:
                ui.error(e); continue
            if rows:
                for s, kind, text in rows:
                    ui.note(f"{s:.2f} · [{kind}] {text}")
            else:
                ui.note("没联想到相关记忆(关键词没匹配上,或长期记忆还太少)")
            continue
        if task == "/forget":                          # usage-based 遗忘:删从没被想起过的死记忆
            import recall
            d = recall.dead()
            if not d:
                ui.note("没有'见过多次却从没被想起'的死记忆(或使用数据还不够)")
                continue
            for kind, text in d:
                ui.note(f"[{kind}] {text}")
            if ui.ask_yes(f"删掉这 {len(d)} 条从没被想起的记忆?(不可恢复)"):
                recall.forget(d)
                ui.note(f"已遗忘 {len(d)} 条")
            continue
        if task.startswith("/view"):
            sid = S.resolve(task[5:])
            ui.show_session(S.open_session(sid).load() if sid else [])
            continue
        if task.startswith("/resume"):
            sid = S.resolve(task[7:])
            if sid:
                sess = S.open_session(sid)
                messages[:] = sess.load()
                ui.note(f"已切到会话 {sess.sid} · {len(messages)} 条消息,可继续编辑")
            else:
                ui.note("没找到该会话 — 用 /history 看编号")
            continue
        if task.startswith("/delete"):
            sid = S.resolve(task[7:])
            if not sid:
                ui.note("没找到该会话 — 用 /history 看编号")
            elif ui.ask_yes(f"确认删除会话 {sid}?(不可恢复)"):
                if sid == sess.sid:                    # 删的是当前会话 → 开一个新空会话
                    sess = S.Session.new()
                    messages[:] = []
                S.delete(sid)
                ui.note(f"已删除 {sid}")
            continue
        mark = len(messages)
        messages.append({"role": "user", "content": task})
        try:
            result = agent_turn(client, model, messages, state)
        except Exception as e:
            _seal(messages)                        # keep the work; just make the history valid again
            ui.error(e)
            ui.note("这一轮的进度都还在 —— 说「继续」可以接着做。")
            continue
        ui.answer(result)
        _tk = state.get("last_tok") or {}
        if _tk.get("in") or _tk.get("out"):
            ui.note(f"🎫 本轮 {_tk.get('steps', 1)} 次调用 · {_tk['in']}+{_tk['out']}={_tk['in'] + _tk['out']} tok"
                    + (f" · 缓存命中 {_tk['cached']}" if _tk.get("cached") else ""))
        _corr = _is_correction(task)
        if state.pop("capped", False):                 # hit the step cap: it was flailing, so whatever it
            ui.note("⏭ 这轮撞了步数上限,跳过复盘(别把瞎试出来的做法学成技能)")   # settled on is not a lesson
        elif _corr or state.get("last_calls", 0) >= REFLECT_AFTER:
            ui.note("🧠 你纠正了它 — 复盘把这条教训记下…" if _corr
                    else f"🧠 这次用了 {state['last_calls']} 步 — 复盘看有没有值得记的…")
            try:
                reflect(client, model, messages, state)
            except Exception as e:
                ui.error(e)
        try:
            messages[:] = maybe_compact(client, model, messages)   # auto-compact if history got long
        except Exception as e:
            ui.error(e)
        _prune_old_tool_results(messages)                          # stub old bulky tool outputs (token saver)
        sess.save(messages)                                        # persist after every turn

# ── offline self-check (no key / no deps):  python agent.py --selfcheck ────────

def _selfcheck() -> None:
    import tempfile
    global WORKSPACE
    WORKSPACE = os.path.realpath(tempfile.mkdtemp())      # jail file ops into a temp dir for the test
    p = os.path.join(WORKSPACE, "t.txt")
    # tools
    assert "wrote" in write_file(p, "hello world")
    assert read_file(p) == "hello world"
    assert edit_file(p, "world", "talos").startswith("edited")
    assert read_file(p) == "hello talos"
    for bad in ("missing",):
        try:
            edit_file(p, bad, "x"); assert False
        except ValueError:
            pass
    write_file(p, "aa")
    try:
        edit_file(p, "a", "b"); assert False
    except ValueError:
        pass
    # permission tiers
    assert _policy("default", "read", "read_file", set()) == "allow"
    assert _policy("plan", "edit", "write_file", set()) == "deny"
    assert _policy("plan", "bash", "run_bash", set()) == "deny"
    assert _policy("bypass", "bash", "run_bash", set()) == "allow"
    assert _policy("acceptEdits", "edit", "write_file", set()) == "allow"
    assert _policy("acceptEdits", "bash", "run_bash", set()) == "ask"
    assert _policy("default", "edit", "write_file", set()) == "ask"
    assert _policy("default", "bash", "run_bash", {"run_bash"}) == "allow"
    # learned-knowledge frontmatter parsing
    m, b = _parse_frontmatter("---\nname: run-tests\ndescription: 何时用\n---\nstep 1\nstep 2")
    assert m["name"] == "run-tests" and m["description"] == "何时用" and b.strip() == "step 1\nstep 2"
    m, b = _parse_frontmatter("no frontmatter")
    assert m == {} and b == "no frontmatter"
    # OpenAI-compatible tool schema shape
    spec = tool_specs()[0]
    assert spec["type"] == "function" and "parameters" in spec["function"]
    # workspace jail: paths outside WORKSPACE are rejected
    outside = os.path.join(tempfile.mkdtemp(), "evil.txt")
    try:
        write_file(outside, "x"); assert False
    except ValueError as e:
        assert "越界" in str(e)
    print("selfcheck ok ✅  (tools + tiers + skills + schema + workspace jail verified)")

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # selfcheck prints emoji; rich manages its own console
    except Exception:
        pass
    argv = sys.argv[1:]
    if "--selfcheck" in argv:
        _selfcheck()
    elif "--list" in argv:
        import console_ui as ui, session as S
        ui.sessions_list(S.list_sessions())
    elif "--view" in argv:
        import console_ui as ui, session as S
        i = argv.index("--view")
        sid = S.resolve(argv[i + 1]) if len(argv) > i + 1 else S.latest_sid()
        ui.show_session(S.open_session(sid).load() if sid else [])
    elif "--delete" in argv:
        import session as S
        i = argv.index("--delete")
        sid = S.resolve(argv[i + 1]) if len(argv) > i + 1 else None
        print(f"deleted {sid}" if (sid and S.delete(sid)) else "not found")
    elif "-p" in argv or "--print" in argv:
        i = argv.index("-p" if "-p" in argv else "--print")
        if len(argv) <= i + 1:
            print('用法: agent.py -p "任务"')
            sys.exit(2)
        once(argv[i + 1])
    elif "--continue" in argv:
        repl(resume=True)
    elif "--resume" in argv:
        i = argv.index("--resume")
        sid = argv[i + 1] if len(argv) > i + 1 and not argv[i + 1].startswith("-") else True
        repl(resume=sid)
    else:
        repl()
