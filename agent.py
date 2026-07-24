"""
Talos — a minimal, self-extending coding agent.  [STEP 4: self-learning]

The core loop is unchanged from Step 1:

    你 ──▶ 模型(只是"说") ──▶ 代码执行工具("做") ──▶ 结果回传 ──▶ … ──▶ 回答
                     └──────────────── 循环,直到模型不再要工具 ─────────────┘

Step 2 adds a **permission gate modeled on Claude Code** — 4 permission *tiers*
(modes) plus a per-call approve/deny prompt. Reads are never gated; anything that
mutates (write / edit / run_bash) is previewed and must be approved.

权限分级 (the tiers), exactly like Claude Code's modes:

    plan         只读:拒绝一切改动(写/改/命令),适合先规划
    default      每次改动都先问你 (y/a/N)          ← 启动默认
    acceptEdits  自动放行 写/改文件,但命令(bash)仍要问
    bypass       全部放行(危险,"yolo")

Step 4 adds **self-learning**: after a substantive task the agent reflects and
writes reusable *skills* (skills/*.md) or durable *facts* (memory.md) for itself,
then loads them next time. Learning = notes-to-self, not model training; every
save goes through the same permission gate. See SELF_LEARNING.md.

⚠️  SAFETY: the gate is a CHECK, not a sandbox. Once you approve a run_bash, it
    still runs on your real machine. This matches Claude Code on native Windows
    (no OS sandbox there either — the permission prompt IS the protection).
    Real isolation = a later step (WSL2/Docker), only when you actually need it.
"""

from __future__ import annotations

import glob
import os
import subprocess
import sys

MODEL = "claude-opus-4-8"     # cheap tinkering: swap to "claude-haiku-4-5"
MAX_TOKENS = 16000
REFLECT_AFTER = 5             # after a task with >= this many tool calls, auto-run a learning pass

ui = None                    # 界面 handle, set by repl() via `import console_ui`. Kept out of
                             # module scope so `--selfcheck` stays dependency-free (no rich needed).

SYSTEM = (
    "You are Talos, a minimal coding agent working inside the user's project "
    "directory. Use the tools to read, write, and edit files and to run shell "
    "commands. Prefer small, verifiable steps. Some actions need the user's "
    "approval and may be denied — if denied, read the reason and either adjust "
    "your approach or ask the user. When the task is done, reply with a one- or "
    "two-sentence summary of what you did."
)

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
    # ponytail: runs on the HOST, unsandboxed. The permission gate below is the
    # guard for now; real isolation (WSL2/Docker) is a later step, if ever needed.
    p = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=120)
    return (p.stdout + p.stderr).strip() or f"(exit {p.returncode}, no output)"

# Registry: name -> (fn, input-properties, required-keys, description, PERM-CLASS).
# perm-class is one of: "read" (never gated) | "edit" (write/edit files) | "bash".
# Adding a tool = adding one row; its perm-class decides how the gate treats it.
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
    return [
        {
            "name": name,
            "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": required},
        }
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
    one-line description — progressive disclosure; the model reads a skill's
    body on demand with read_file. So context cost = memory + N one-liners,
    NOT N full skills. That's how it stays minimal while the library grows."""
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

# ── the loop ──────────────────────────────────────────────────────────────────

def agent_turn(client, messages: list, state: dict) -> str:
    """Drive one user request to completion, looping over tool calls."""
    learned = retrieve()                               # inject memory + skill descriptions
    system = SYSTEM + ("\n\n" + learned if learned else "")
    calls = 0
    while True:
        with ui.thinking():
            reply = client.messages.create(
                model=MODEL, max_tokens=MAX_TOKENS,
                system=system, tools=tool_specs(), messages=messages,
            )
        messages.append({"role": "assistant", "content": reply.content})

        if reply.stop_reason != "tool_use":     # no tool wanted -> final answer
            state["last_calls"] = calls
            return "".join(b.text for b in reply.content if b.type == "text")

        results = []
        for block in reply.content:
            if block.type == "tool_use":
                calls += 1
                cls = TOOLS[block.name][4]
                allowed, reason = check_permission(state, cls, block.name, block.input)
                if not allowed:
                    out, is_error = f"permission denied: {reason}", True
                    ui.denied(block.name, reason)
                else:
                    out, is_error = run_tool(block.name, block.input)
                    ui.show_tool(block.name, block.input, out, is_error)
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": out, "is_error": is_error,
                })
        messages.append({"role": "user", "content": results})   # results are a USER turn

# ── the learning write-back: reflect (save) + consolidate (tidy) ──────────────
# Both are just another agent_turn with a special prompt — so saving reuses the
# same gated write_file/edit_file. That's why Step 4 is mostly prompts, not code.

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

def reflect(client, messages: list, state: dict) -> str:
    """One extra learning turn — saves skills/facts, reusing the gated tools.
    Runs on a COPY of messages so the reflection prompt never pollutes memory."""
    return agent_turn(client, messages + [{"role": "user", "content": REFLECT_PROMPT}], state)

def consolidate(client, state: dict) -> str:
    return agent_turn(client, [{"role": "user", "content": CONSOLIDATE_PROMPT}], state)

def repl() -> None:
    global ui
    import console_ui as ui             # the 界面 (needs rich); lazy so --selfcheck stays dep-free
    import anthropic                     # only talking to the model needs the SDK
    client = anthropic.Anthropic()      # reads ANTHROPIC_API_KEY from the env
    messages: list = []                 # ← this list IS the memory; it grows every turn
    state = {"mode": "default", "allow": set()}   # ← permission state for the session

    ui.banner(state["mode"])
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
            reflect(client, messages, state)
            continue
        if task == "/consolidate":
            ui.note("🧹 整理技能中…")
            consolidate(client, state)
            continue
        messages.append({"role": "user", "content": task})
        result = agent_turn(client, messages, state)
        ui.answer(result)
        if state.get("last_calls", 0) >= REFLECT_AFTER:
            ui.note(f"🧠 这次用了 {state['last_calls']} 步 — 复盘看有没有值得记的…")
            reflect(client, messages, state)

# ── offline self-check (no API key needed):  python agent.py --selfcheck ───────

def _selfcheck() -> None:
    import tempfile
    p = os.path.join(tempfile.mkdtemp(), "t.txt")
    # tools
    assert "wrote" in write_file(p, "hello world")
    assert read_file(p) == "hello world"
    assert edit_file(p, "world", "talos").startswith("edited")
    assert read_file(p) == "hello talos"
    for bad in ("missing",):                       # 0 matches -> error
        try:
            edit_file(p, bad, "x"); assert False
        except ValueError:
            pass
    write_file(p, "aa")                            # 2 matches -> not-unique error
    try:
        edit_file(p, "a", "b"); assert False
    except ValueError:
        pass
    # permission tiers (Claude-Code-style)
    assert _policy("default", "read", "read_file", set()) == "allow"     # reads never gated
    assert _policy("plan", "edit", "write_file", set()) == "deny"        # plan = read-only
    assert _policy("plan", "bash", "run_bash", set()) == "deny"
    assert _policy("bypass", "bash", "run_bash", set()) == "allow"       # yolo
    assert _policy("acceptEdits", "edit", "write_file", set()) == "allow"
    assert _policy("acceptEdits", "bash", "run_bash", set()) == "ask"    # edits auto, bash still asks
    assert _policy("default", "edit", "write_file", set()) == "ask"
    assert _policy("default", "bash", "run_bash", {"run_bash"}) == "allow"  # session-allowed tool
    # learned-knowledge frontmatter parsing
    m, b = _parse_frontmatter("---\nname: run-tests\ndescription: 何时用\n---\nstep 1\nstep 2")
    assert m["name"] == "run-tests" and m["description"] == "何时用" and b.strip() == "step 1\nstep 2"
    m, b = _parse_frontmatter("no frontmatter")
    assert m == {} and b == "no frontmatter"
    print("selfcheck ok ✅  (tools + permission tiers + skill parsing verified)")

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # selfcheck prints emoji; rich manages its own console
    except Exception:
        pass
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        repl()
