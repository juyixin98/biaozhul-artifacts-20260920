# syntax=docker/dockerfile:1
# Multi-stage build: compile the static binary, then run it on a slim
# Chromium base. No cloud services are used at any point.
FROM golang:1.22-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/sitevitals ./cmd/sitevitals

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium \
      ca-certificates \
      fonts-liberation \
      libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
      libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
      libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app && useradd --system --gid app --home-dir /home/app --create-home app
USER app
WORKDIR /app
COPY --from=build /out/sitevitals /app/sitevitals
ENV SV_CHROME_BIN=/usr/bin/chromium \
    SV_HTTP_ADDR=:8080 \
    SV_TESTSITE_ADDR=:8093
EXPOSE 8080 8093
ENTRYPOINT ["/app/sitevitals"]
CMD ["serve", "--testsite"]
