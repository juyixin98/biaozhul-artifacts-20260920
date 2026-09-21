# Build stage
FROM golang:1.22-alpine AS build
WORKDIR /src

# Cache module downloads first.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/server ./cmd/server

# Runtime stage
FROM alpine:3.20
RUN adduser -D -u 10001 app && apk add --no-cache ca-certificates
USER app
COPY --from=build /out/server /usr/local/bin/communityvault
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/communityvault"]
