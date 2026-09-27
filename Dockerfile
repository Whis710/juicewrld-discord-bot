FROM python:3.12-slim

WORKDIR /app

# Install system dependencies for PyNaCl (voice support)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libffi-dev \
    libnacl-dev \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*



COPY requirements.txt .
COPY . .

EXPOSE 8080

ENTRYPOINT ["sh", "-c", "pip install --no-cache-dir --upgrade 'discord.py[voice]>=2.7.0' && python bot.py"]
