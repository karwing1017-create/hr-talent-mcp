"""
端到端测试脚本：实际调用 MCP 工具的 get_talent_overview，验证数据库连接和 SQL 是否正确。
用法：.venv\\Scripts\\python.exe test_e2e.py
"""
import subprocess
import json
import sys
import os
import time
import threading


def send_and_receive(proc, request):
    proc.stdin.write(json.dumps(request) + '\n')
    proc.stdin.flush()
    # 读取直到收到对应 id 的响应
    deadline = time.time() + 15
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            time.sleep(0.05)
            continue
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if data.get('id') == request['id']:
                return data
        except json.JSONDecodeError:
            print(f'  [raw] {line[:200]}')
    return None


def main():
    env = os.environ.copy()

    print('启动 MCP Server...')
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

    stderr_lines = []

    def read_stderr():
        try:
            for line in proc.stderr:
                stderr_lines.append(line.rstrip())
        except Exception:
            pass

    t_err = threading.Thread(target=read_stderr, daemon=True)
    t_err.start()

    time.sleep(1)

    print('初始化连接...')
    init_req = {
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {
            'protocolVersion': '2024-11-05',
            'capabilities': {},
            'clientInfo': {'name': 'e2e-test', 'version': '1.0'}
        }
    }
    init_resp = send_and_receive(proc, init_req)
    if not init_resp or 'result' not in init_resp:
        print('初始化失败:', init_resp)
        proc.terminate()
        return 1
    print('初始化成功:', init_resp['result'].get('serverInfo'))

    # 发送 initialized 通知
    proc.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}) + '\n')
    proc.stdin.flush()

    print('\n调用 get_talent_overview，参数：date=2026-07-31...')
    tool_req = {
        'jsonrpc': '2.0', 'id': 2,
        'method': 'tools/call',
        'params': {
            'name': 'get_talent_overview',
            'arguments': {'date': '2026-07-31'}
        }
    }
    tool_resp = send_and_receive(proc, tool_req)

    if not tool_resp or 'result' not in tool_resp:
        print('工具调用失败或无响应:', tool_resp)
        proc.terminate()
        return 1

    result = tool_resp['result']
    print(f'工具调用完成，isError={result.get("isError")}')

    content = result.get('content', [])
    if content:
        text = content[0].get('text', '')
        try:
            data = json.loads(text)
            print('\n返回数据摘要：')
            if 'snapshot_date' in data:
                print(f'  快照日期: {data["snapshot_date"]}')
            if 'total_employees' in data:
                print(f'  在职人数: {data["total_employees"]}')
            if 'avg_age' in data:
                print(f'  平均年龄: {data["avg_age"]}')
            if 'manager_count' in data:
                print(f'  管理干部人数: {data["manager_count"]}')
            if 'key_position_count' in data:
                print(f'  关键岗位人数: {data["key_position_count"]}')

            print('\n完整返回数据（已格式化）：')
            print(json.dumps(data, ensure_ascii=False, indent=2))
        except json.JSONDecodeError:
            print('返回内容不是 JSON：')
            print(text[:1000])
    else:
        print('返回内容为空')

    proc.stdin.close()
    proc.terminate()

    if stderr_lines:
        print('\n--- Server 日志（最后 20 行） ---')
        for line in stderr_lines[-20:]:
            print(line)

    print('\n结论：端到端测试完成。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
