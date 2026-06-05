FROM python:3.12-slim

WORKDIR /workspace

COPY pyproject.toml /workspace/pyproject.toml
COPY README.md /workspace/README.md
COPY server/ /workspace/server/

RUN pip install --no-cache-dir -e ".[docs]"

CMD ["mkdocs", "build", "--strict"]
