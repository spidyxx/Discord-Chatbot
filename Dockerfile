FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy bot code
COPY bot.py .

# Create data directory for persistent memory
RUN mkdir -p /app/data

CMD ["python", "-u", "bot.py"]
