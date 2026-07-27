"""
recall.py — 联想回忆:把长期记忆连成一张网,用"扩散激活"(spreading activation)
按当前任务捞出相关的几条,注入上下文。

  节点 = 记忆(memory.md 的事实 / skills / 过往会话)
  边   = 共享关键词的关联(突触)
  回忆 = 当前任务点亮匹配的节点 → 沿边衰减扩散 1~2 跳 → 取最亮的几个

这是可替换的一层(跟 session.py / console_ui.py 一样)—— 想升级成 embedding/向量检索,
只改这个文件,agent.py 不用动。纯 stdlib。
# ponytail: 便宜的 bigram 分词 + O(n²) 建边,记忆几百条以内够用;要更准就换 embedding。
"""

from __future__ import annotations

import glob
import json
import os
import re

HOME = os.path.realpath(os.environ.get("TALOS_HOME") or os.path.dirname(os.path.abspath(__file__)))
MEMORY_FILE = os.path.join(HOME, "memory.md")
SKILLS_DIR = os.path.join(HOME, "skills")
SESS_DIR = os.path.join(HOME, ".talos", "sessions")

DECAY = 0.6      # 每跳衰减
HOPS = 2         # 扩散跳数
THRESH = 0.05    # 低于此激活不算"想起来"
EDGE_MIN = 2     # 至少共享这么多关键词才连边(保持稀疏,防止全连成一团)
SKILL_BODIES = 2      # 最多注入几条技能正文(再多就把上下文撑爆了)
SKILL_BODY_MAX = 1200 # 每条正文截断长度
_STOP = set("的 了 和 是 在 我 你 它 也 就 都 一个 the a an and or to of is it".split())

def _keywords(text: str) -> set:
    text = (text or "").lower()
    kw = set(re.findall(r"[a-z0-9]{2,}", text))          # 英文/数字词
    for run in re.findall(r"[一-鿿]+", text):    # 中文按 bigram 切
        for i in range(len(run) - 1):
            kw.add(run[i:i + 2])
        if len(run) == 1:
            kw.add(run)
    return kw - _STOP

def _frontmatter_desc(txt: str) -> str:
    name = desc = ""
    if txt.startswith("---"):
        end = txt.find("\n---", 3)
        if end != -1:
            for line in txt[3:end].splitlines():
                if line.startswith("name:"):
                    name = line[5:].strip()
                elif line.startswith("description:"):
                    desc = line[12:].strip()
    return (name + " — " + desc).strip(" —") or txt[:60]

def _first_user(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                m = json.loads(ln)
                if m.get("role") == "user" and isinstance(m.get("content"), str):
                    return m["content"]
    except Exception:
        pass
    return ""

def _load_nodes() -> list:
    nodes = []
    try:                                          # best-effort layer: a garbled memory.md
        with open(MEMORY_FILE, encoding="utf-8") as f:   # means no recall, never a crash
            for ln in f:
                s = ln.strip().lstrip("-*# ").strip()
                if len(s) >= 4:
                    nodes.append({"kind": "事实", "text": s})
    except (OSError, UnicodeDecodeError):
        pass
    for p in sorted(glob.glob(os.path.join(SKILLS_DIR, "*.md"))):
        try:
            with open(p, encoding="utf-8") as f:
                raw = f.read()
            # 正文也参与匹配:关键事实(字段名、坑)都在正文里,只按描述匹配会捞不到
            nodes.append({"kind": "技能", "text": _frontmatter_desc(raw), "body": raw, "path": p})
        except Exception:
            pass
    for p in sorted(glob.glob(os.path.join(SESS_DIR, "*.jsonl"))):
        f = _first_user(p)
        if f:
            nodes.append({"kind": "往事", "text": f[:80]})
    for n in nodes:
        n["kw"] = _keywords(n.get("body") or n["text"])
    return [n for n in nodes if n["kw"]]

def _edges(nodes: list) -> dict:
    E = {}
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            shared = len(nodes[i]["kw"] & nodes[j]["kw"])
            if shared >= EDGE_MIN:
                w = shared / max(len(nodes[i]["kw"]), len(nodes[j]["kw"]))
                E.setdefault(i, {})[j] = w
                E.setdefault(j, {})[i] = w
    return E

def _activate(nodes: list, edges: dict, query: str) -> dict:
    qk = _keywords(query)
    if not qk:
        return {}
    act = {}
    for i, n in enumerate(nodes):                 # 种子:query 点亮直接匹配的节点
        ov = len(n["kw"] & qk)
        if ov:
            act[i] = ov / len(qk)
    if not act:
        return {}
    for _ in range(HOPS):                          # 扩散:激活沿边衰减着传给邻居
        nxt = dict(act)
        for i, a in list(act.items()):
            for j, w in edges.get(i, {}).items():
                nxt[j] = nxt.get(j, 0.0) + a * DECAY * w
        act = nxt
    return act

def _rank(query: str):
    nodes = _load_nodes()
    act = _activate(nodes, _edges(nodes), query)
    ranked = [(round(a, 2), i) for i, a in sorted(act.items(), key=lambda x: -x[1]) if a > THRESH]
    return nodes, ranked

def explain(query: str, k: int = 8) -> list:
    """[(score, kind, text), ...] top-k —— 给 /recall 调试用(无副作用)。"""
    nodes, ranked = _rank(query)
    return [(a, nodes[i]["kind"], nodes[i]["text"]) for a, i in ranked][:k]

def recall(query: str, k: int = 5) -> str:
    """注入上下文的"联想记忆"文本块;顺便记录命中(供 usage-based 遗忘)。"""
    nodes, ranked = _rank(query)
    top = ranked[:k]
    _record_usage(nodes, {_key(nodes[i]) for _a, i in top})
    if not top:
        return ""
    out, bodies = [], 0
    for _a, i in top:
        n = nodes[i]
        # 技能命中就直接给正文 —— 光给一行描述,模型多半懒得再 read_file 去看,
        # 而该省你十步的字段名、坑,全在正文里。限量,别把上下文撑爆。
        if n["kind"] == "技能" and bodies < SKILL_BODIES:
            bodies += 1
            out.append(f"- [技能 {n['path']}]\n{n['body'][:SKILL_BODY_MAX]}")
        else:
            out.append(f"- [{n['kind']}] {n['text']}")
    return "# 回忆(联想到的相关记忆)\n" + "\n".join(out)

# ── usage tracking:让"用没用到"决定记忆去留 ─────────────────────────────────────
HITS_FILE = os.path.join(HOME, ".talos", "recall_hits.json")

def _key(node: dict) -> str:
    return node["kind"] + ":" + node["text"][:80]

def _load_hits() -> dict:
    try:
        with open(HITS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _record_usage(nodes: list, recalled: set) -> None:
    h = _load_hits()
    for n in nodes:
        if n["kind"] in ("事实", "技能"):          # 只统计知识;往事(会话)是原始记录,不参与遗忘
            k = _key(n)
            seen, hits = h.get(k, [0, 0])
            h[k] = [seen + 1, hits + (1 if k in recalled else 0)]
    try:
        os.makedirs(os.path.dirname(HITS_FILE), exist_ok=True)
        with open(HITS_FILE, "w", encoding="utf-8") as f:
            json.dump(h, f, ensure_ascii=False)
    except Exception:
        pass

def dead(min_seen: int = 8) -> list:
    """[(kind, text), ...] —— 出现过 >= min_seen 次、却从没被想起的知识 = 死重。"""
    h = _load_hits()
    out = []
    for n in _load_nodes():
        if n["kind"] in ("事实", "技能"):
            seen, hits = h.get(_key(n), [0, 0])
            if seen >= min_seen and hits == 0:
                out.append((n["kind"], n["text"]))
    return out

def forget(items: list) -> None:
    """删掉这些从没被想起的事实(从 memory.md 移除行)和技能(删文件)。"""
    facts = {t for k, t in items if k == "事实"}
    skills = {t for k, t in items if k == "技能"}
    if facts and os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, encoding="utf-8") as f:
            lines = f.readlines()
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            f.writelines(ln for ln in lines if ln.strip().lstrip("-*# ").strip() not in facts)
    for p in glob.glob(os.path.join(SKILLS_DIR, "*.md")):
        try:
            with open(p, encoding="utf-8") as f:
                doomed = _frontmatter_desc(f.read()) in skills
            if doomed:
                os.remove(p)
        except Exception:
            pass
