# syntax=docker/dockerfile:1

# ---- build stage ----
FROM golang:1.22-alpine AS build
WORKDIR /src

# Cache dependencies first.
COPY go.mod go.sum ./
RUN go mod download

# Copy source and generated sqlc code.
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/sircc ./cmd/sircc

# ---- runtime stage ----
FROM alpine:3.20
RUN adduser -D -u 10001 sircc
USER sircc
WORKDIR /app
COPY --from=build /out/sircc /app/sircc

# Migrations are embedded in the binary, so no SQL files are needed at runtime.
EXPOSE 8080
ENTRYPOINT ["/app/sircc"]
