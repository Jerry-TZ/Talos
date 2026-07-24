# Talos 🛡️

A minimal, self-extending coding agent — **Pi's tiny core + a sandbox/permission
layer Pi deliberately skips + (later) Hermes's self-learning.**

> In Greek myth, **Talos** was a giant bronze **automaton** that guarded Crete.
> An automaton that protects = an agent with safety built in.

## Where we are: STEP 2 — permission gate + Claude-Code-style terminal

`agent.py` is still the whole thing (~230 lines). It's the loop you understand:

```
你 → 模型(说) → 代码执行工具(做) → 结果回传 → … → 回答
```

**Step 1** gave it 4 tools (`read_file`, `write_file`, `edit_file`, `run_bash`)
+ `messages[]` memory. **Step 2** adds:

- **Permission gate** — mutating tools (write / edit / run_bash) show a preview
  and ask before running. `[y]` allow once · `[N]` deny (default) · `[a]` allow
  this tool for the session. A denial is fed back to the model so it adapts.
  Reads are auto-allowed.
- **Terminal UI** — colored output, previews, and a diff-style view for edits.
  This *is* the "Claude-Code-like UI": Claude Code is a terminal app, so the
  right UI for a coding agent lives in the terminal, not a web page.

### 权限分级 (tiers) — 对齐 Claude Code

Four session modes, switch at runtime with `/mode <name>`:

| mode | reads | write / edit | run_bash | 何时用 |
|------|-------|--------------|----------|--------|
| `plan` | ✅ auto | ❌ 拒绝 | ❌ 拒绝 | 只读,先让它规划 |
| `default` | ✅ auto | ❓ 问 | ❓ 问 | **启动默认** |
| `acceptEdits` | ✅ auto | ✅ auto | ❓ 问 | 信任改文件、仍盯着命令 |
| `bypass` | ✅ auto | ✅ auto | ✅ auto | 全放行(危险) |

Reads are never gated. On a prompt: `[y]` 一次 · `[a]` 本会话都允许该工具 · `[N]`
拒绝(默认) · 或直接打字 = 拒绝并把理由回传给模型。

### Why not a web GUI?
A separate web frontend teaches you nothing about the agent — it's just plumbing
around the same loop, and it belongs to the much-later "channels" step (à la
Hermes: Telegram/Slack/web). The terminal is the correct home for a coding agent
now. Build the web layer only when you want to use it from your phone or share it.

## Run it

```bash
pip install -r requirements.txt

# verify tools + the permission gate WITHOUT an API key or network:
python agent.py --selfcheck

# then talk to it (needs an Anthropic API key):
#   PowerShell:  $env:ANTHROPIC_API_KEY="sk-..."
#   bash:        export ANTHROPIC_API_KEY=sk-...
python agent.py
```

Try: `列出当前目录文件,然后新建 hello.txt 写一句话`. You'll get a `● write_file
想执行:` prompt before anything touches disk.

## ⚠️ Safety status

The permission gate is a **check, not a sandbox**. Once you allow a `run_bash`,
it still runs directly on your machine. Only allow commands you understand.
**Step 3** moves tool execution into Docker — real isolation.

## Roadmap

| Step | Adds | Whose idea |
|------|------|-----------|
| 1 ✅ | minimal loop + 4 tools + memory | Pi (简洁) |
| 2 ✅ | **permission gate + terminal UI** | 你要的安全 |
| 3 | **sandbox** — run tools inside Docker (no host mounts, limited net) | 你要的安全 |
| 4 | **self-learning** — a `reflect` step writes `SKILL.md` + `memory.md`, loaded on demand | Hermes (成长) |
| 5 | (optional) channels/web UI — only when you want phone/sharing | Hermes (全能) |

## Swapping the model

Everything lives behind one `client.messages.create(...)` call. To use a free
**Gemini** key instead, that one function changes (Gemini's SDK + tool-call
field shapes differ); the loop, tools, memory, and permission gate stay identical.

The Pi source is cloned in `../PI_Agent` for reference.
