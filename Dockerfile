# Build stage
FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /out/targetcraft ./cmd/server

# Run stage
FROM alpine:3.20
RUN apk add --no-cache ca-certificates tzdata
COPY --from=build /out/targetcraft /usr/local/bin/targetcraft
EXPOSE 8080
ENTRYPOINT ["targetcraft"]
