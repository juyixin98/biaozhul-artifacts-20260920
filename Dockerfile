# Multi-stage build for the local domain lifecycle engine.
FROM golang:1.22-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/server ./cmd/server && \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/simtimedemo ./cmd/simtimedemo

FROM gcr.io/distroless/static-debian12:nonroot
WORKDIR /app
COPY --from=build /out/server /app/server
COPY --from=build /out/simtimedemo /app/simtimedemo
# Migrations are embedded in the binary (go:embed), so no SQL copy is needed.
EXPOSE 8080
USER nonroot:nonroot
ENTRYPOINT ["/app/server"]
