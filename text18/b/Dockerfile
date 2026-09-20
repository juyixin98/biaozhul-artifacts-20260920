# Build stage
FROM golang:1.25-bookworm AS build
WORKDIR /src

# Cache dependencies first.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/sircc ./cmd/server

# Runtime stage
FROM gcr.io/distroless/static-debian12:nonroot
WORKDIR /app
COPY --from=build /out/sircc /app/sircc
COPY --from=build /src/migrations /app/migrations

ENV HTTP_ADDR=":8080" \
    MIGRATIONS_DIR="/app/migrations" \
    REMINDER_INTERVAL="10s" \
    REMINDER_BATCH_SIZE="100"

EXPOSE 8080
USER nonroot:nonroot
ENTRYPOINT ["/app/sircc"]
