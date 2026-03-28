FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y gcc g++ python3-dev pkg-config libcairo2-dev libgl1 ffmpeg libsm6 libxext6 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

COPY . .

# Railway dynamically sets the PORT environment variable.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}