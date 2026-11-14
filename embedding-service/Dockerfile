FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ curl \
    && rm -rf /var/lib/apt/lists/*

# Install torch explicitly, before the requirements that pull it in transitively.
#
# FlagEmbedding depends on torch, and on Linux the default PyPI wheel is the CUDA build:
# resolving it that way drags in ~20 nvidia-* packages plus triton — several GB that never
# execute here, because docker-compose.yml reserves no GPU and the service runs on CPU.
# Measured before this pin: a 9.49 GB image.
#
# Pinning it first means the later `-r requirements.txt` finds torch already satisfied and
# leaves it alone. Build with --build-arg TORCH_INDEX_URL=https://pypi.org/simple for the
# CUDA build, and add a GPU reservation to compose to go with it.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG TORCH_VERSION=2.13.0
RUN pip install --no-cache-dir --index-url ${TORCH_INDEX_URL} "torch==${TORCH_VERSION}"

COPY requirements.txt .
# Every FlagEmbedding dependency is pinned in requirements.txt, transformers included, so
# one resolver pass picks a consistent set. Forcing transformers in a later layer, as this
# file used to, skipped the resolver and left whatever it had already chosen incompatible.
RUN pip install --no-cache-dir -r requirements.txt && pip check
COPY app ./app
EXPOSE 8000
# First start downloads model weights (cache the HF dir via a volume).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
