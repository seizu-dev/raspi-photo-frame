FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      libsdl2-2.0-0 libsdl2-image-2.0-0 libsdl2-ttf-2.0-0 \
      libdrm2 libgbm1 libegl1 libgles2 \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir --only-binary=:all: pygame-ce
