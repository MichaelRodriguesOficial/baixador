FROM python:3.12-slim-bookworm

# 7z (zip, 7z, gz, bz2, xz, partes .001), unar (rar e rar em partes), tar
RUN apt-get update \
 && apt-get install -y --no-install-recommends p7zip-full unar xz-utils bzip2 ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY templates ./templates

ENV DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
CMD ["python", "app.py"]
