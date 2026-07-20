FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir tzdata

COPY app.py /app/app.py

EXPOSE 8080

CMD ["python", "/app/app.py"]
