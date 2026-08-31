# ---------- 前端构建 ----------
FROM node:22-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---------- 运行时 ----------
FROM python:3.13-slim
WORKDIR /app

# ffmpeg 用于导出 mp3
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./backend/app
COPY --from=ui /ui/dist ./frontend/dist

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/app/data

EXPOSE 8756
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8756"]
