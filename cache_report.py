"""读 .talos/cache_trace.jsonl,按 system 变没变分组算命中率。用法:python 这个文件"""
import json, os, statistics, sys
p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".talos", "cache_trace.jsonl")
rows = [json.loads(l) for l in open(p, encoding="utf-8")] if os.path.exists(p) else []
if not rows:
    sys.exit("还没有数据 —— 正常用几轮再来看。")
for label, want in (("system 没变", False), ("system 变了(复盘写过技能)", True)):
    g = [r["hit"] for r in rows if r["sys_changed"] is want and r["hit"] is not None]
    if g:
        print(f"{label:26} n={len(g):<3} 命中中位数 {statistics.median(g):.0%}  "
              f"范围 {min(g):.0%}~{max(g):.0%}")
print(f"\n共 {len(rows)} 轮。差值小于 10 个百分点、或任一组 n<8 时,别下结论。")
