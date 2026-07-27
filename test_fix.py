with open('agent.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()
print('行数:', len(lines))

import re
matches = re.findall(r'^\s*def\s+\w+', ''.join(lines), re.MULTILINE)
print('函数数:', len(matches))
