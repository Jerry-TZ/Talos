import re

# 工具里的正则
pattern = r'^\s*def\s+\w+'

# agent.py 前 50 行
with open('agent.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()[:50]

matches = re.findall(pattern, ''.join(lines), re.MULTILINE)
print(f'前 50 行匹配到 {len(matches)} 个函数定义')
print('前几个:', matches[:10])
