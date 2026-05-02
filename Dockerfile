FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY bot.py .
COPY version.py .
COPY plugins/ ./plugins/

# Create data directory for persistent memory
RUN mkdir -p /app/data

# hf_xet (faster-whisper dependency) writes logs here; directory must exist
RUN mkdir -p /.cache/huggingface/xet/logs

CMD ["python", "-u", "bot.py"]
