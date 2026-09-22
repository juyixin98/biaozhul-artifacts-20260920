# Multi-stage build; the final image contains only the static binary and
# migration/sample files. Build context is the repository root.
FROM golang:1.22 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /out/server ./cmd/server
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /out/importer ./cmd/importer

FROM gcr.io/distroless/static-debian12:nonroot
WORKDIR /app
COPY --from=build /out/server /app/server
COPY --from=build /out/importer /app/importer
COPY migrations /app/migrations
COPY samples /app/samples
EXPOSE 8080
USER nonroot:nonroot
ENTRYPOINT ["/app/server"]
CMD ["--migrations", "/app/migrations"]
