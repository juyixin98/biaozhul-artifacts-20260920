# 多阶段构建：构建静态二进制，运行镜像仅含 ca-certificates 与数据目录
FROM golang:1.22-bookworm AS build

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/proofcycle-server ./cmd/server

FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home --home-dir /app proofcycle

WORKDIR /app
COPY --from=build /out/proofcycle-server /app/proofcycle-server
COPY migrations /app/migrations
COPY testdata /app/testdata
RUN mkdir -p /data/files && chown -R proofcycle:proofcycle /data /app

USER proofcycle
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=10 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1
ENTRYPOINT ["/app/proofcycle-server"]
