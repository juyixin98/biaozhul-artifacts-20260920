# syntax=docker/dockerfile:1

# ---- build stage ----
FROM golang:1.22-bookworm AS build
WORKDIR /src

# Cache dependencies first.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/geoterritory ./cmd/server

# ---- runtime stage ----
FROM debian:bookworm-slim
RUN apt-get update \
	&& apt-get install -y --no-install-recommends ca-certificates \
	&& rm -rf /var/lib/apt/lists/* \
	&& useradd --system --uid 10001 appuser
WORKDIR /app
COPY --from=build /out/geoterritory /app/geoterritory
COPY migrations /app/migrations
COPY examples /app/examples

ENV HTTP_ADDR=":8080" \
    MIGRATION_DIR="/app/migrations" \
    AUTO_MIGRATE="true" \
    WORKER_ENABLED="true"

EXPOSE 8080
USER appuser
ENTRYPOINT ["/app/geoterritory"]
