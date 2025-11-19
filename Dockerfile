FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps for audio/transcoding libs and build tooling for cffi extensions
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg libsndfile1 build-essential python3-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.prod.txt .
RUN pip install --upgrade pip && pip install -r requirements.prod.txt

COPY . .

RUN python manage.py collectstatic --noinput

EXPOSE 8000

CMD ["daphne", "hearhelper.asgi:application", "--bind", "0.0.0.0", "--port", "8000"]
