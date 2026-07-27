import re
import os

def count_funcs(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    matches = re.findall(r'^\s*def\s+\w+', ''.join(lines), re.MULTILINE)
    return len(matches), matches[:5]

path = 'test_regex.py'
count, names = count_funcs(path)
print(f'{path}: {count} 个函数')
print('前几个:', names)
