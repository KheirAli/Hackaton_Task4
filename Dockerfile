# Competition-run environment downloads the base from public Docker Hub at build time.
FROM python:3.11-slim
WORKDIR /app
COPY --chown=65534:65534 selector.py /app/selector.py
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp
USER 65534:65534
ENTRYPOINT ["python3", "-B", "/app/selector.py"]
