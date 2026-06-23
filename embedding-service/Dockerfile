FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ curl \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# FlagEmbedding 1.3.4 imports is_torch_fx_available, which newer transformers removed.
# Pin a compatible transformers (separate layer keeps the cached torch install intact).
RUN pip install --no-cache-dir "transformers==4.45.2"
COPY app ./app
EXPOSE 8000
# First start downloads model weights (cache the HF dir via a volume).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
