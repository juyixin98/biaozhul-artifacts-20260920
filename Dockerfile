# syntax=docker/dockerfile:1

# ---- build ----
FROM golang:1.22-alpine AS build
WORKDIR /src
RUN apk add --no-cache git
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -o /out/dams-server ./cmd/server \
 && CGO_ENABLED=0 GOOS=linux go build -trimpath -o /out/dams-migrate ./cmd/migrate \
 && CGO_ENABLED=0 GOOS=linux go build -trimpath -o /out/dams-seed ./cmd/seed

# ---- runtime ----
FROM alpine:3.20
RUN apk add --no-cache ca-certificates tzdata wget
COPY --from=build /out/dams-server /usr/local/bin/dams-server
COPY --from=build /out/dams-migrate /usr/local/bin/dams-migrate
COPY --from=build /out/dams-seed /usr/local/bin/dams-seed
EXPOSE 8080
USER 65532
ENTRYPOINT ["/usr/local/bin/dams-server"]
