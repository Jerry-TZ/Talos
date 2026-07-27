import re

with open('agent.py', 'r', encoding='utf-8') as f:
    content = f.read()

print('前 500 字符:', repr(content[:500]))
print('换行符:', repr(content[:200].split('\n')[-1]))
