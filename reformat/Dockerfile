FROM python:3.12-slim

WORKDIR /workspace

COPY pyproject.toml /workspace/pyproject.toml
COPY server/ /workspace/server/

RUN pip install --no-cache-dir -e ".[dev]"

COPY reformat/reformat.sh /reformat.sh
RUN chmod +x /reformat.sh

CMD ["/reformat.sh"]
