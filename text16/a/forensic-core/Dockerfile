# 构建阶段
FROM golang:1.25-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -o /out/forensic-core ./cmd/server

# 运行阶段：非 root 运行，证据目录以只读方式挂载
FROM debian:bookworm-slim
RUN groupadd -r forensic && useradd -r -g forensic forensic
COPY --from=build /out/forensic-core /usr/local/bin/forensic-core
USER forensic
ENV FORENSIC_ADDR=:8080 \
    FORENSIC_EVIDENCE_ROOT=/evidence \
    FORENSIC_CHUNK_SIZE=4194304
EXPOSE 8080
ENTRYPOINT ["forensic-core"]
