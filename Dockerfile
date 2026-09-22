# Build the API server.
FROM golang:1.22-bookworm AS build
WORKDIR /src

# Cache modules first.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath \
    -o /out/server  ./cmd/server && \
    CGO_ENABLED=0 GOOS=linux go build -trimpath \
    -o /out/migrate ./cmd/migrate && \
    CGO_ENABLED=0 GOOS=linux go build -trimpath \
    -o /out/seed    ./cmd/seed

FROM gcr.io/distroless/static-debian12:nonroot
WORKDIR /app
COPY --from=build /out/server  /app/server
COPY --from=build /out/migrate /app/migrate
COPY --from=build /out/seed    /app/seed
EXPOSE 8080
USER nonroot:nonroot
ENTRYPOINT ["/app/server"]
