r"""
本地测试脚本：不依赖 npx / Inspector，直接验证 MCP Server 是否能正常响应。
用法：.venv\Scripts\python.exe test_server.py
"""
import subprocess
import json
import sys
import os
import time
import threading


def test_server():
    # 使用真实的数据库连接信息（从 .env 加载）
    env = os.environ.copy()

    proc = subprocess.Popen(
        [sys.executable, 'server.py'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace',
        env=env,
    )

    responses = []
    stderr_lines = []

    def read_stdout():
        try:
            for line in proc.stdout:
                line = line.strip()
                if line:
                    try:
                        responses.append(json.loads(line))
                    except json.JSONDecodeError:
                        responses.append({'raw': line})
        except Exception as e:
            stderr_lines.append(f'stdout reader error: {e}')

    def read_stderr():
        try:
            for line in proc.stderr:
                stderr_lines.append(line.rstrip())
        except Exception as e:
            stderr_lines.append(f'stderr reader error: {e}')

    t_out = threading.Thread(target=read_stdout, daemon=True)
    t_err = threading.Thread(target=read_stderr, daemon=True)
    t_out.start()
    t_err.start()

    messages = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
         'params': {'protocolVersion': '2024-11-05', 'capabilities': {},
                    'clientInfo': {'name': 'test', 'version': '1.0'}}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
    ]

    print('Step 1: 发送 initialize / tools/list 给 MCP Server...')
    for msg in messages:
        proc.stdin.write(json.dumps(msg) + '\n')
    proc.stdin.flush()

    time.sleep(2)
    proc.stdin.close()

    t_out.join(timeout=5)
    t_err.join(timeout=5)
    proc.terminate()

    print(f'\nStep 2: 收到 {len(responses)} 条响应')
    tool_names = []
    for i, data in enumerate(responses):
        if 'result' in data:
            result = data['result']
            if 'tools' in result:
                tools = result['tools']
                tool_names = [t['name'] for t in tools]
                print(f'  [{i + 1}] tools/list -> 共 {len(tools)} 个工具:')
                for t in tools:
                    desc = t.get('description', '')[:70]
                    print(f'      - {t["name"]}: {desc}')
            elif 'serverInfo' in result:
                print(f'  [{i + 1}] initialize -> server={result["serverInfo"]}')
            elif 'protocolVersion' in result:
                print(f'  [{i + 1}] initialize -> protocol={result["protocolVersion"]}')
            else:
                print(f'  [{i + 1}] result: {json.dumps(result, ensure_ascii=False)[:150]}')
        elif 'raw' in data:
            print(f'  [{i + 1}] raw: {data["raw"][:150]}')
        else:
            print(f'  [{i + 1}] msg: {json.dumps(data, ensure_ascii=False)[:150]}')

    if stderr_lines:
        print(f'\nStep 3: 标准错误输出（最后 15 行）:')
        for line in stderr_lines[-15:]:
            print(f'  {line}')
    else:
        print('\nStep 3: 没有标准错误输出')

    if tool_names:
        print(f'\n结论：MCP Server 启动成功，注册了 {len(tool_names)} 个工具。')
        return 0
    else:
        print('\n结论：MCP Server 没有返回工具列表，请检查上面的错误输出。')
        return 1


if __name__ == '__main__':
    sys.exit(test_server())
