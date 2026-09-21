# syntax=docker/dockerfile:1

# ---- build ----
FROM golang:1.22-alpine AS build
WORKDIR /src
RUN apk add --no-cache git ca-certificates
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -o /out/api ./cmd/api
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -o /out/worker ./cmd/worker

# ---- api ----
FROM gcr.io/distroless/static-debian12:nonroot AS api
COPY --from=build /out/api /usr/local/bin/clearsettle-api
EXPOSE 8080
USER nonroot:nonroot
ENTRYPOINT ["/usr/local/bin/clearsettle-api"]

# ---- worker ----
FROM gcr.io/distroless/static-debian12:nonroot AS worker
COPY --from=build /out/worker /usr/local/bin/clearsettle-worker
USER nonroot:nonroot
ENTRYPOINT ["/usr/local/bin/clearsettle-worker"]
