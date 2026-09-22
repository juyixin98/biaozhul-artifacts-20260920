# Build the server binary in a small image.
FROM golang:1.22-bookworm AS build
WORKDIR /src

# Cache dependencies first.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/renderq ./cmd/server
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/genpng  ./cmd/genpng

FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /out/renderq /app/renderq
COPY --from=build /out/genpng  /app/genpng
COPY migrations /app/migrations
RUN mkdir -p /data
ENV HTTP_ADDR=:8080 \
    DATA_DIR=/data \
    DATABASE_URL=postgres://renderq:renderq@db:5432/renderq?sslmode=disable
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=10 \
  CMD curl -fsS http://localhost:8080/healthz || exit 1
ENTRYPOINT ["/app/renderq"]
