FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml .
COPY README.md .
COPY server/__init__.py ./server/__init__.py
COPY dashboard/src/ ./dashboard/

RUN pip install --no-cache-dir ".[dashboard]"

EXPOSE 8501

CMD ["streamlit", "run", "dashboard/app.py", "--server.port=8501", "--server.address=0.0.0.0"]
