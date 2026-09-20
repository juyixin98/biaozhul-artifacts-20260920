# syntax=docker/dockerfile:1
# Build stage: produce a fully static binary (CGO disabled, pure-Go pgx).
FROM golang:1.22-alpine AS build
WORKDIR /src

# Cache dependencies first.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
# Canonical SQL lives in migrations/; keep the embedded copy in sync.
RUN cp migrations/*.sql internal/migrate/sql/
RUN CGO_ENABLED=0 GOOS=linux go build \
    -trimpath -ldflags="-s -w" \
    -o /out/signalboard ./cmd/server

# Runtime stage: static binary on scratch, no shell, no package manager.
FROM scratch
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /out/signalboard /signalboard
# Migrations are embedded into the binary; no copy needed at runtime.
EXPOSE 8080
USER 65534:65534
ENTRYPOINT ["/signalboard"]
