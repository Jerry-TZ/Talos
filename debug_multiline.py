import re

pattern = r'^\s*def\s+\w+'

with open('agent.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# 工具里的写法
matches1 = re.findall(pattern, ''.join(lines), re.MULTILINE)
print(f'"".join(lines): {len(matches1)} 个')

# 正确的写法
matches2 = re.findall(pattern, lines, re.MULTILINE)
print(f'lines (not joined): {len(matches2)} 个')
