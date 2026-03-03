# using a slim Python image for a smaller footprint
# NOTE: Ensure your PostgreSQL database uses postgis/postgis:latest image for PostGIS support
FROM python:3.11-slim

WORKDIR /app

# Install build dependencies for packages that need compilation
RUN apt-get update && apt-get install -y gcc g++ python3-dev pkg-config libcairo2-dev && rm -rf /var/lib/apt/lists/*

# Creating a non-root user and switch to it
# Hugging Face requires UID 1000
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade -r requirements.txt

COPY --chown=user . .

# MUST use port 7860 for Hugging Face Spaces.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]