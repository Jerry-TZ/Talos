import os
import ast

py_files = []
for filename in os.listdir('.'):
    if filename.endswith('.py') and not filename.startswith('.'):
        py_files.append(filename)

print(f'发现 {len(py_files)} 个 .py 文件:')
print('-' * 80)

total_lines = 0
total_functions = 0

for filename in sorted(py_files):
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            content = f.read()
        
        lines = len(content.splitlines())
        
        tree = ast.parse(content)
        functions = []
        
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                functions.append(node.name)
        
        func_count = len(functions)
        
        total_lines += lines
        total_functions += func_count
        
        print(f'文件: {filename}')
        print(f'  行数: {lines}')
        print(f'  函数数: {func_count}')
        print(f'  函数列表: {", ".join(functions) if functions else "无"}')
        print()
        
    except Exception as e:
        print(f'文件 {filename} 解析失败: {e}')

print('-' * 80)
print(f'汇总统计:')
print(f'  总文件数: {len(py_files)}')
print(f'  总行数: {total_lines}')
print(f'  总函数数: {total_functions}')