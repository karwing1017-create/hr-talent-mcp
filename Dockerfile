FROM python:3.12-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制源码
COPY config.py db.py metrics.py server.py auth.py ./

# 暴露 HTTP 端口（stdio 模式不需要，但保留以便统一使用）
EXPOSE 8000

# 默认以 streamable-http 模式启动
# 通过环境变量覆盖: MCP_TRANSPORT=stdio 可切回本地模式
ENV MCP_TRANSPORT=streamable-http
ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8000

CMD ["python", "server.py"]
