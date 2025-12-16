FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir requests fastapi uvicorn

COPY app.py .

CMD ["python", "app.py"]
