# Build stage: compile a static binary with embedded migrations.
FROM golang:1.25-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" \
    -o /out/synapticgod ./cmd/server

# Runtime stage: only the binary; data lives on a mounted volume.
FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --create-home --home-dir /data synapticgo
WORKDIR /app
COPY --from=build /out/synapticgod /app/synapticgod
COPY examples /app/examples
RUN mkdir -p /data/blobs /data/tmp && chown -R synapticgo:synapticgo /data
USER synapticgo
EXPOSE 8080
ENV SYN_HTTP_ADDR=:8080 \
    SYN_DATA_DIR=/data \
    SYN_DATABASE_URL=postgres://synapticgo:synapticgo@db:5432/synapticgo?sslmode=disable
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD ["/app/synapticgod", "-healthcheck"]
ENTRYPOINT ["/app/synapticgod"]
