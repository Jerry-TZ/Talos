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

import glob
import json
import os
import sys

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
    "two-sentence summary of what you did."
)
REFLECT_AFTER = 5    # after a task with >= this many tool calls, auto-run a learning pass
COMPACT_AT = 30000   # ponytail: char-count proxy for tokens; compact history past this (add tiktoken for precision)

ui = None            # 界面 handle, set by repl(); kept out of module scope so --selfcheck is dep-free

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

def read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def write_file(path: str, content: str) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"wrote {len(content)} chars to {path}"

def edit_file(path: str, old: str, new: str) -> str:
    text = read_file(path)
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
    p = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=120)
    return (p.stdout + p.stderr).strip() or f"(exit {p.returncode}, no output)"

# Registry: name -> (fn, input-properties, required-keys, description, PERM-CLASS).
# perm-class is one of: "read" (never gated) | "edit" (write/edit files) | "bash".
TOOLS = {
    "read_file": (
        lambda a: read_file(a["path"]),
        {"path": {"type": "string"}}, ["path"],
        "Read a text file and return its contents.", "read",
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
    try:
        return str(TOOLS[name][0](args)), False
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
        mem = read_file(MEMORY_FILE).strip()
        if mem:
            parts.append("# 记住的事实 (memory.md)\n" + mem)
    skills = sorted(glob.glob(os.path.join(SKILLS_DIR, "*.md")))
    if skills:
        lines = []
        for path in skills:
            meta, _ = _parse_frontmatter(read_file(path))
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

def agent_turn(client, model: str, messages: list, state: dict) -> str:
    """Drive one user request to completion, looping over tool calls."""
    learned = retrieve()                               # inject memory + skill descriptions
    system = SYSTEM + ("\n\n" + learned if learned else "")
    calls = 0
    while True:
        with ui.thinking():
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}] + messages,
                tools=tool_specs(),
            )
        msg = resp.choices[0].message
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
            state["last_calls"] = calls
            return msg.content or ""

        for c in tool_calls:
            calls += 1
            name = c.function.name
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            cls = TOOLS[name][4] if name in TOOLS else "bash"
            allowed, reason = check_permission(state, cls, name, args)
            if not allowed:
                out, is_error = f"permission denied: {reason}", True
                ui.denied(name, reason)
            elif name not in TOOLS:
                out, is_error = f"error: unknown tool {name}", True
            else:
                out, is_error = run_tool(name, args)
                ui.show_tool(name, args, out, is_error)
            messages.append({"role": "tool", "tool_call_id": c.id, "content": out})

# ── the learning write-back: reflect (save) + consolidate (tidy) ──────────────
# Both are just another agent_turn with a special prompt — so saving reuses the
# same gated write_file/edit_file. That's why self-learning is mostly prompts.

REFLECT_PROMPT = (
    "复盘刚才的对话。如果有**可复用的做法**值得留下,用 write_file 存成 "
    "skills/<kebab-name>.md:开头用 --- 包住 frontmatter(name、description=何时用),"
    "再写步骤。如果有关于用户/项目的**持久事实或教训**,用 edit_file 往 memory.md 追加"
    "一行(没有该文件就 write_file 新建)。只存下次真能帮上忙的,一次性的别存。没有值得"
    "存的就直说、别写文件。"
)
CONSOLIDATE_PROMPT = (
    "用 run_bash `ls skills` 列出所有技能,再 read_file 逐个看。合并重复、删掉太窄或"
    "没用的(用 run_bash 删文件),让每条 description 更好匹配。保持这套技能小而精。"
)

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
        resp = client.chat.completions.create(model=model, messages=(
            [{"role": "system", "content": "你在压缩一段编程 agent 的对话历史。产出一段简报,保留:"
              "当前任务、已做的决定、改动过的文件、还没完成的线索。简洁、要点式。"}]
            + clean + [{"role": "user", "content": "把以上压成简报。"}]))
    summary = resp.choices[0].message.content or "(空)"
    ui.note(f"🗜 上下文已压缩({len(messages)} 条 → 2 条)")
    return [{"role": "user", "content": "【早前对话的压缩摘要】\n" + summary},
            {"role": "assistant", "content": "了解,以上是之前的进展,我们继续。"}]

def repl(resume=None) -> None:
    global ui
    import console_ui as ui              # the 界面 (needs rich); lazy so --selfcheck stays dep-free
    import session as S                  # local conversation storage (Claude-Code-style)
    client, model = make_client()        # OpenAI-compatible client for the chosen provider
    if resume is not None:               # --continue (latest) / --resume <id>
        sid = S.latest_sid() if resume is True else resume
        sess = S.Session(sid) if sid else S.Session.new()
        messages: list = sess.load()
    else:
        sess = S.Session.new()
        messages = []
    state = {"mode": "default", "allow": set()}   # ← permission state for the session

    ui.banner(state["mode"], PROVIDER, model)
    ui.note(f"会话 {sess.sid}" + (f" · 续上 {len(messages)} 条消息" if messages else " · 存于 .talos/sessions/"))
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
        mark = len(messages)
        messages.append({"role": "user", "content": task})
        try:
            result = agent_turn(client, model, messages, state)
        except Exception as e:
            del messages[mark:]                    # roll back the failed turn; keep history valid
            ui.error(e)
            continue
        ui.answer(result)
        if state.get("last_calls", 0) >= REFLECT_AFTER:
            ui.note(f"🧠 这次用了 {state['last_calls']} 步 — 复盘看有没有值得记的…")
            try:
                reflect(client, model, messages, state)
            except Exception as e:
                ui.error(e)
        try:
            messages[:] = maybe_compact(client, model, messages)   # auto-compact if history got long
        except Exception as e:
            ui.error(e)
        sess.save(messages)                                        # persist after every turn

# ── offline self-check (no key / no deps):  python agent.py --selfcheck ────────

def _selfcheck() -> None:
    import tempfile
    p = os.path.join(tempfile.mkdtemp(), "t.txt")
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
    print("selfcheck ok ✅  (tools + permission tiers + skill parsing + tool schema verified)")

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
        sid = argv[i + 1] if len(argv) > i + 1 else S.latest_sid()
        ui.show_session(S.Session(sid).load() if sid else [])
    elif "--continue" in argv:
        repl(resume=True)
    elif "--resume" in argv:
        i = argv.index("--resume")
        sid = argv[i + 1] if len(argv) > i + 1 and not argv[i + 1].startswith("-") else True
        repl(resume=sid)
    else:
        repl()
