// Command trace-server 启动分布式追踪拼装的 HTTP 服务。
//
// 用法：
//
//	go run ./cmd/trace-server -addr :8080 -data-dir ./data -timeout-ns 100
//
// 时间均为调用方提供的合成逻辑时间（receive_ns / watermark_ns），
// 服务自身绝不读取壁钟参与拼装判定。
package main

import (
	"flag"
	"log"
	"net/http"

	"traceassembly/internal/httpapi"
	"traceassembly/store"
	"traceassembly/trace"
)

func main() {
	addr := flag.String("addr", ":8080", "HTTP 监听地址")
	dataDir := flag.String("data-dir", "./data", "持久化目录（WAL 与快照）；空串表示纯内存")
	timeoutNS := flag.Int64("timeout-ns", 100, "trace 空闲超时（逻辑时间单位）")
	skewTol := flag.Int64("skew-tolerance-ns", 1000, "跨服务时钟偏差容忍带（本地时间戳纳秒）")
	flag.Parse()

	cfg := trace.Config{TimeoutNS: *timeoutNS, SkewToleranceNS: *skewTol}

	var asm *trace.Assembler
	var st *store.FileStore
	var err error
	if *dataDir != "" {
		asm, st, err = store.Open(*dataDir, cfg)
		if err != nil {
			log.Fatalf("open store %q: %v", *dataDir, err)
		}
		log.Printf("persistence enabled at %s", *dataDir)
	} else {
		asm = trace.New(cfg, nil)
		log.Printf("running in-memory (no persistence)")
	}

	srv := httpapi.NewServer(asm)
	log.Printf("trace assembly server listening on %s (timeout=%d watermark=%d)",
		*addr, *timeoutNS, asm.Watermark())
	if st != nil {
		defer func() { _ = st.Close() }()
	}
	if err := http.ListenAndServe(*addr, srv.Mux); err != nil {
		log.Fatal(err)
	}
}
