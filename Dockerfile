# syntax=docker/dockerfile:1

# ---- build stage ----
FROM golang:1.25-bookworm AS build
WORKDIR /src

# Cache module downloads independently of source changes.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
# Static binary (CGO disabled; the sqlite driver is pure-Go).
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath \
    -ldflags "-s -w" \
    -o /out/server ./cmd/server \
 && CGO_ENABLED=0 GOOS=linux go build -trimpath \
    -ldflags "-s -w" \
    -o /out/genseed ./cmd/genseed

# ---- runtime stage ----
FROM debian:bookworm-slim AS runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system forensic \
 && useradd --system --gid forensic --home-dir /app --create-home forensic \
 && mkdir -p /data/samples \
 && chown -R forensic:forensic /data

COPY --from=build /out/server /app/server
COPY --from=build /out/genseed /app/genseed
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

USER forensic
WORKDIR /app

# The image is read from /data; mount your evidence directory read-only there.
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["serve"]
