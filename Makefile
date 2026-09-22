.PHONY: all build test test-race fixture demo1 demo2 demo3 clean vet proto

all: build

build:
	go build ./...

test:
	go test ./... -count=1

test-race:
	go test -race ./... -count=1

vet:
	go vet ./...

fixture:
	go run ./cmd/genfixture -out examples/fixture.json -tip 63 -checkpoints 0,16,32,48,63

demo1:
	./script/1-full-sync.sh

demo2:
	./script/2-cancel-resume.sh

demo3:
	./script/3-unfillable-gap.sh

# 重新生成 gRPC 代码（需要 protoc 与 protoc-gen-go / protoc-gen-go-grpc）
proto:
	protoc --go_out=. --go_opt=paths=source_relative \
	       --go-grpc_out=. --go-grpc_opt=paths=source_relative \
	       proto/syncpb/sync.proto

clean:
	rm -rf bin run
