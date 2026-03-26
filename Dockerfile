FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy source
COPY engine /app/engine
COPY config /app/config

# Create data directory (SQLite)
RUN mkdir -p /data

ENV PYTHONUNBUFFERED=1

# Default runtime args
CMD ["python", "-m", "engine.main"]

