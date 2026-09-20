# SiteVitals single-image build: static Go binary + Debian Chromium.
# Build:
#   docker build -t sitevitals:latest .

FROM golang:1.22-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/sitevitals ./cmd/sitevitals

FROM debian:bookworm-slim
ENV TZ=UTC \
    CHROME_PATH=/usr/bin/chromium \
    HTTP_ADDR=:8080 \
    DEMO_ADDR=:8090
RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium \
      ca-certificates \
      fonts-liberation fonts-noto-cjk fonts-noto-color-emoji \
      libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
      libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
      libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /out/sitevitals /app/sitevitals
EXPOSE 8080
ENTRYPOINT ["/app/sitevitals"]
CMD ["serve"]
