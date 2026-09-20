# syntax=docker/dockerfile:1

# ---- build stage ----
FROM golang:1.22-alpine AS build
WORKDIR /src

# Cache dependencies first.
COPY go.mod go.sum ./
RUN go mod download

COPY . .

# Fully static binary: no cgo, stripped, embed migrations + tzdata.
ENV CGO_ENABLED=0 GOOS=linux
RUN go build -trimpath -ldflags="-s -w" -o /out/server ./cmd/server \
 && go build -trimpath -ldflags="-s -w" -o /out/seed   ./cmd/seed

# ---- runtime stage ----
FROM gcr.io/distroless/static-debian12:nonroot
WORKDIR /
COPY --from=build /out/server /usr/local/bin/signalboard
COPY --from=build /out/seed   /usr/local/bin/signalboard-seed

# Migrations are embedded in the binary; no filesystem copy needed.
EXPOSE 8080
USER nonroot:nonroot
ENTRYPOINT ["/usr/local/bin/signalboard"]
