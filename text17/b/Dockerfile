# syntax=docker/dockerfile:1

# Build stage: static binary.
FROM golang:1.22-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -o /bin/synapticgo ./cmd/server

# Runtime stage: non-root user, no shell baggage.
FROM debian:bookworm-slim
RUN useradd -r -u 10001 synapticgo && mkdir -p /data && chown synapticgo /data
COPY --from=build /bin/synapticgo /bin/synapticgo
USER synapticgo
ENV ADDR=:8080 \
    DATA_DIR=/data \
    DATABASE_URL=postgres://postgres:postgres@db:5432/synapticgo?sslmode=disable
VOLUME /data
EXPOSE 8080
ENTRYPOINT ["/bin/synapticgo"]
