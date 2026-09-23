# Real worker image: busybox tools (tar/gzip/sha256sum/base64) + curl + jq.
FROM alpine:3.20

RUN apk add --no-cache ca-certificates curl jq tar

COPY worker.sh /usr/local/bin/snapshot-worker
RUN chmod +x /usr/local/bin/snapshot-worker

USER 65532:65532
ENTRYPOINT ["/usr/local/bin/snapshot-worker"]
