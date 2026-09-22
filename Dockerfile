# Build stage
FROM golang:1.22-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" \
    -o /out/damsserver ./cmd/damsserver && \
    CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" \
    -o /out/damsctl ./cmd/damsctl

# Runtime
FROM gcr.io/distroless/static-debian12:nonroot
WORKDIR /
COPY --from=build /out/damsserver /usr/local/bin/damsserver
COPY --from=build /out/damsctl /usr/local/bin/damsctl
ENV DAMS_HTTP_ADDR=":8080"
EXPOSE 8080
USER nonroot:nonroot
ENTRYPOINT ["/usr/local/bin/damsserver"]
