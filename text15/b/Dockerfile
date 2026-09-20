# ---- build stage ----
FROM golang:1.22-bookworm AS build
WORKDIR /src

# Cache dependencies first.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/proofcycle ./cmd/proofcycle

# ---- runtime stage ----
FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 proofcycle \
    && useradd  --system --uid 10001 --gid proofcycle --home-dir /app proofcycle

WORKDIR /app
COPY --from=build /out/proofcycle /app/proofcycle

RUN mkdir -p /app/data/storage && chown -R proofcycle:proofcycle /app
USER proofcycle

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["/app/proofcycle"]
