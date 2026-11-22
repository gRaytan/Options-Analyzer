# Image for the trading engine and the web UI (same code, different command).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY templates ./templates
COPY run.py .
COPY data/config ./data/config

# Run as an unprivileged user; data/ is mounted as a volume.
RUN useradd --create-home --uid 1000 app && mkdir -p data/trading && chown -R app /app
USER app

CMD ["python", "-m", "src.trading.run"]
