import re

pattern = r'^\s*def\s+\w+'

with open('agent.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# 工具里的写法：join
content = ''.join(lines)
matches = re.findall(pattern, content, re.MULTILINE)
print(f'join + MULTILINE: {len(matches)} 个')

# 不 join，直接用 lines
matches2 = re.findall(pattern, lines, re.MULTILINE)
print(f'lines (not joined) + MULTILINE: {len(matches2)} 个')

# 不用 MULTILINE
matches3 = re.findall(pattern, content)
print(f'join (no MULTILINE): {len(matches3)} 个')
