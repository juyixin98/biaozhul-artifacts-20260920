FROM golang:1.22-alpine AS build
WORKDIR /src
RUN apk add --no-cache git
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /out/server ./cmd/server \
 && CGO_ENABLED=0 go build -o /out/reconcile ./cmd/reconcile

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=build /out/server /out/reconcile /usr/local/bin/
EXPOSE 8080
ENTRYPOINT ["server"]
