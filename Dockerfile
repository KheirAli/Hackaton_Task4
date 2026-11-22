# Same base image (by digest) as the organisers' starter kit.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
# selector.py needs only the standard library. blast_index.zip is a map of the
# pinned sqlglot commit (see tools/); both must be readable by user `nobody`.
COPY selector.py blast_index.zip /app/
RUN chmod 0644 /app/selector.py /app/blast_index.zip
ENV PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-B", "/app/selector.py"]
