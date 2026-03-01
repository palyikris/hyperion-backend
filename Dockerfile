# using a slim Python image for a smaller footprint
# NOTE: Ensure your PostgreSQL database uses postgis/postgis:latest image for PostGIS support
FROM python:3.10-slim

WORKDIR /app

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