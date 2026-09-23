#!/usr/bin/env bash
# Regenerate Go gRPC stubs from proto definitions.
#
# The generated files are committed, so normal builds/tests do NOT need this.
# It is only required after editing proto/nodesync/sync.proto. On first use it
# bootstraps a pinned, repo-local toolchain under tools/ (gitignored), which
# needs network access once.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS="$ROOT/tools"
BIN="$TOOLS/bin"
PROTOC_VER=25.1
PROTOC_GEN_GO=v1.34.2
PROTOC_GEN_GO_GRPC=v1.4.0

if [ ! -x "$BIN/protoc" ]; then
  echo "bootstrapping protoc ${PROTOC_VER} and Go plugins (one-time)..."
  mkdir -p "$BIN"
  GOBIN="$BIN" go install google.golang.org/protobuf/cmd/protoc-gen-go@${PROTOC_GEN_GO}
  GOBIN="$BIN" go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@${PROTOC_GEN_GO_GRPC}
  tmp="$(mktemp -d)"
  url="https://github.com/protocolbuffers/protobuf/releases/download/v${PROTOC_VER}/protoc-${PROTOC_VER}-linux-x86_64.zip"
  curl -fsSL -o "$tmp/protoc.zip" "$url"
  unzip -q -o "$tmp/protoc.zip" -d "$TOOLS/protoc"
  rm -rf "$tmp"
  ln -sf "$TOOLS/protoc/bin/protoc" "$BIN/protoc"
fi

export PATH="$BIN:$PATH"
"$BIN/protoc" \
  --proto_path="$ROOT/proto" \
  --go_out="$ROOT" --go_opt=module=nodesync \
  --go-grpc_out="$ROOT" --go-grpc_opt=module=nodesync \
  "$ROOT/proto/nodesync/sync.proto"
echo "generated internal/pb/sync.pb.go internal/pb/sync_grpc.pb.go"
