import re
import os

def count_funcs(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    func_count = len(re.findall(r'^\s*def\s+\w+', ''.join(lines), re.MULTILINE))
    func_names = re.findall(r'^\s*def\s+(\w+)', ''.join(lines), re.MULTILINE)
    return func_count, func_names

count, names = count_funcs('agent.py')
print(f'agent.py: {count} 个函数')
print('前几个函数:', names[:10])
