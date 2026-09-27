FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for PyNaCl (voice support)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libffi-dev \
    libnacl-dev \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*



COPY requirements.txt .
# Force Portainer to bypass the cache
RUN echo "Force Cache Bust 2026-09-27"
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8080

CMD ["python", "bot.py"]
