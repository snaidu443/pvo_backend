# PVO API image. Python 3.12 (well supported by all pinned packages); runs as a non-root user.
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
RUN useradd --create-home --uid 10001 pvo

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY scripts ./scripts
# Local upload folder until S3 / UC Volume storage is added (mount a volume here on Kubernetes).
RUN mkdir /app/uploads && chown pvo /app/uploads

USER pvo
EXPOSE 8000
# 2 workers per pod; scale with more pods (HPA) rather than more workers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers"]
