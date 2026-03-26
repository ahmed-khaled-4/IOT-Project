FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY engine /app/engine
COPY config /app/config

RUN mkdir -p /data

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "engine.main"]
