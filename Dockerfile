FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py web.py .

RUN useradd --system --create-home appuser && mkdir -p /data && chown appuser:appuser /data
USER appuser

CMD ["python", "app.py"]

