# Talos 开发文档 — 实现逻辑详解

这份文档解释 `agent.py` 里每一块**为什么这么写、怎么运作**。目标读者是想搞懂
"一个 agent 在本地到底是怎么跑起来的"的人。全部实现就一个文件,不到 300 行。

---

## 1. 一句话:agent = 一个循环

大模型只会**文字进、文字出**,它碰不到你的电脑。所谓 "agent",就是你写一段代码,
把模型的输出**当指令来执行**,再把执行结果喂回去,如此循环:

```
你 ──▶ 模型(说"调用工具X") ──▶ 你的代码执行X("做") ──▶ 结果回传 ──▶ 模型 ──▶ … ──▶ 回答
              └──────────────────── 循环,直到模型不再要工具 ────────────────────┘
```

Talos 就是这个循环 + 4 个工具 + 一个权限门。

---

## 2. 四个组成部分(对照代码)

| 部分 | 代码位置 | 作用 |
|------|---------|------|
| **模型** | `agent_turn()` 里的 `client.messages.create(...)` | 大脑:决定调哪个工具、够了没、怎么回答 |
| **工具** | `TOOLS` 字典 + 4 个函数 | 手:真正读写文件、跑命令 |
| **循环** | `agent_turn()` 的 `while True` | 心跳:串起"模型说→代码做→回传" |
| **记忆** | `repl()` 里的 `messages` 列表 | 每轮把全部历史再发给模型 |

关键点:**模型本身没有记忆。** 它"记得"上文,只是因为你每次把整个 `messages`
列表重新发过去。记忆 = 一个不断变长的列表,仅此而已。

---

## 3. 循环细节:`agent_turn()` 一步步

```python
def agent_turn(client, messages, state):
    while True:
        reply = client.messages.create(model=..., tools=tool_specs(), messages=messages)
        messages.append({"role": "assistant", "content": reply.content})   # 记下模型说了啥

        if reply.stop_reason != "tool_use":          # 模型不要工具了 → 收尾
            return 提取文本(reply)

        results = []
        for block in reply.content:                  # 模型这一轮可能要调多个工具
            if block.type == "tool_use":
                过权限门 → 执行 or 拒绝 → 收集 tool_result
        messages.append({"role": "user", "content": results})   # ★ 结果作为 user 消息回传
```

三个必须理解的点:

1. **`stop_reason` 是分叉口。** 模型返回时会带一个停止原因:
   - `tool_use` → 它想调工具,我们执行完**继续循环**;
   - 其它(`end_turn` 等)→ 它给最终答案了,**跳出循环**返回文本。

2. **一轮可以调多个工具。** `reply.content` 是一串 block,可能有多个 `tool_use`。
   我们遍历全部,把每个的结果都收进 `results`,**一次性**回传。

3. **工具结果的角色是 `user`(★ 最反直觉的一点)。** Anthropic 的接口里,工具执行
   结果不是 assistant 说的,而是作为一条 `role: "user"` 的 `tool_result` 发回去 ——
   相当于"用户(其实是你的代码)把工具跑出来的东西告诉模型"。每个 `tool_result`
   必须带上对应的 `tool_use_id` 配对。

---

## 4. 工具系统:加一个工具 = 加一行

工具就是普通 Python 函数(`read_file` / `write_file` / `edit_file` / `run_bash`),
再在 `TOOLS` 字典里登记:

```python
TOOLS = {
  "read_file": (fn, 参数属性, 必填key, 描述, 权限类别),
  ...
}
```

五元组的最后一项是**权限类别**(`read` / `edit` / `bash`),权限门靠它决定要不要拦。

- `tool_specs()` 把字典转成 API 要的 JSON schema(告诉模型有哪些工具、参数长啥样)。
- `run_tool()` 真正执行,并且**把异常转成错误字符串回传给模型,而不是让程序崩**
  —— 这样模型看到 `error: ...` 能自己改。

`edit_file` 特意要求 `old` 在文件里**唯一**(0 个或多个都报错),逼模型给足上下文,
避免改错地方。这是从真实编码 agent 学来的小设计。

---

## 5. 权限门:对齐 Claude Code 的"分级"

这是 Step 2 的核心,也是 Talos 和"裸 Pi"的区别。

### 4 个档位(modes / tiers)

| mode | 读 | 写/改 | 命令 | 何时用 |
|------|----|------|------|--------|
| `plan` | ✅ 自动 | ❌ 拒 | ❌ 拒 | 只读,先让它规划 |
| `default` | ✅ 自动 | ❓ 问 | ❓ 问 | **默认** |
| `acceptEdits` | ✅ 自动 | ✅ 自动 | ❓ 问 | 信任改文件、仍盯命令 |
| `bypass` | ✅ 自动 | ✅ 自动 | ✅ 自动 | 全放行(危险) |

运行时用 `/mode <名字>` 切换,提示符会显示当前档:`你 (default) ›`。

### 决策与交互分离(为什么这么拆)

```python
def _policy(mode, cls, name, allow) -> "allow" | "deny" | "ask":   # 纯函数,无 I/O
def check_permission(state, cls, name, args):                       # 决策 + 真正弹窗
```

`_policy()` 是**纯函数**——只根据 (档位, 权限类别, 是否已放行) 返回三选一,不碰输入
输出。好处:**可以单元测试**,`--selfcheck` 就把 8 种组合全断言了一遍,不用 mock 键盘。

`check_permission()` 拿到 `_policy` 的决定:
- `allow` → 直接放行;
- `deny` → 挡下,把"plan 模式禁止 X"作为错误回传给模型;
- `ask` → 打印预览(命令/写入内容/改动 diff),等你敲 `[y]一次` / `[a]本会话都允许该工具`
  / `[N]拒绝`。**直接打字 = 拒绝并把你的话回传给模型**(等于"不行,你该这样做")。

### 拒绝是怎么"教"模型的

被拒时,我们不是抛异常,而是回一条 `is_error: True` 的 `tool_result`,内容是拒绝理由。
模型下一轮就看到"上次被拒,原因是X",于是自己调整。**权限门既是给你的闸,也是给模型
的反馈通道。**

⚠️ 注意:门是**检查**,不是**沙箱**。你一旦对某条 `run_bash` 点了允许,它仍然真的在你
机器上跑。真隔离(WSL2 / Docker)是以后的 Step 3,用得上再加。

---

## 6. 几个工程决策(为什么这么写)

- **冻结极简核**:内核永远只有"循环 + 4 工具 + messages"。以后的成长(沙箱、自学习技能)
  加在**外围**,不往这个文件里塞。这是 Pi 的哲学。
- **`anthropic` 懒加载**:`import anthropic` 放在 `repl()` 内部,不在文件顶部。这样纯逻辑
  (工具、权限)零依赖,`--selfcheck` 不装任何包、不联网、不用 key 就能跑。
- **Windows 控制台**:`__main__` 里先 `sys.stdout.reconfigure(encoding="utf-8")`(否则 GBK
  控制台遇到 emoji/中文直接崩),再用 ctypes 开 ANSI 颜色(VT 模式)。
- **颜色按 `isatty` 开关**:被管道捕获(比如测试)时自动关掉颜色,输出干净。
- **`# ponytail:` 标记**:`run_bash` 上留了注释,写明"当前在宿主机裸跑,升级路径是沙箱"。
  故意留的技术债,标注清楚以后能找到。

---

## 7. 测试:`--selfcheck`

```bash
python agent.py --selfcheck    # 零依赖、零网络、零 key
```

覆盖:
- 4 个工具的读/写/改,以及 `edit_file` 的"找不到 / 不唯一"两种报错;
- 权限 `_policy()` 的 8 种档位×类别组合。

交互分支(y/a/N/自由文本)因为要读键盘,用 monkeypatch `input` 的小脚本单独验证过
(见提交历史)。原则:**有分支的逻辑,至少留一个能跑的断言。**

---

## 8. 运行 & 配置

```bash
pip install -r requirements.txt
# PowerShell: $env:ANTHROPIC_API_KEY="sk-..."
python agent.py
```

- 换模型:改文件顶部 `MODEL`(省钱用 `claude-haiku-4-5`);换 Gemini 只需改那一处
  `client.messages.create` 调用,循环/工具/权限都不动。
- 输出长度:`MAX_TOKENS`。

---

## 9. 路线图 & 已知取舍

| Step | 加什么 | 状态 |
|------|-------|------|
| 1 | 极简核:循环 + 4 工具 + 记忆 | ✅ |
| 2 | 权限门 + 分级 + 终端 UI | ✅ |
| 3 | 沙箱(run_bash 进 Docker/WSL2) | 待定,用得上再做 |
| 4 | 自学习:reflect 写 SKILL.md / memory.md | 计划中(Hermes 的魂) |

- **门 ≠ 沙箱**:允许后仍在宿主机跑。
- **单文件**:刻意的。等它真的塞不下了再拆。
- **记忆无上限**:`messages` 一直变长,长对话会烧 token;以后要加"上下文压缩",现在不需要。
