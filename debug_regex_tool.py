import re

pattern = r'^\s*def\s+\w+'

with open('agent.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# 模拟工具里的逻辑
matches = re.findall(pattern, ''.join(lines), re.MULTILINE)
print(f'全部文件匹配到 {len(matches)} 个函数定义')
print('前 20 个:', matches[:20])
