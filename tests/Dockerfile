FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY server/ ./server/
COPY tests/ ./tests/

RUN pip install --no-cache-dir -e ".[test]"

CMD ["pytest", "--mcp-tools=http://mcp-server:8000", "tests/"]
