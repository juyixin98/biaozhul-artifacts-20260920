// Command criticalpath 启动“追踪关键路径分析”纯后端服务。
//
// 用法:
//
//	go run ./cmd/server [-addr :8080] [-data ./data]
//
// 首次启动可设置 CP_SEED=1 自动写入 demo 合成 trace。
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"criticalpath/internal/analyzer"
	"criticalpath/internal/api"
	"criticalpath/internal/store"
)

func main() {
	addr := flag.String("addr", envOr("CP_ADDR", ":8080"), "HTTP 监听地址（也可用 CP_ADDR）")
	dataDir := flag.String("data", envOr("CP_DATA", "./data"), "JSONL 持久化目录（也可用 CP_DATA）")
	seed := flag.Bool("seed", os.Getenv("CP_SEED") == "1", "启动时写入 demo 合成 trace（CP_SEED=1）")
	flag.Parse()

	st, err := store.Open(*dataDir)
	if err != nil {
		log.Fatalf("打开存储失败: %v", err)
	}
	log.Printf("存储就绪: %s（重放恢复 %d 条记录）", *dataDir, st.ReplayedCount())

	if *seed {
		seedTrace(st, "demo-trace-1", analyzer.DemoSpans("demo-trace-1"))
	}

	srv := &http.Server{
		Addr:              *addr,
		Handler:           api.NewServer(st).Mux,
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		log.Printf("HTTP 服务监听 %s", *addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("HTTP 服务失败: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
	log.Printf("收到退出信号，开始优雅关闭")
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("关闭超时: %v", err)
	}
	if err := st.Close(); err != nil {
		log.Printf("存储关闭失败: %v", err)
	}
	fmt.Fprintln(os.Stderr, "已退出")
}

func seedTrace(st *store.Store, id string, spans []analyzer.Span) {
	if existing, ok := st.GetTrace(id); ok && len(existing) > 0 {
		log.Printf("seed 跳过: %s 已存在 %d 个 span", id, len(existing))
		return
	}
	if _, err := st.PutBatch(spans); err != nil {
		log.Printf("seed 写入失败: %v", err)
		return
	}
	log.Printf("seed 已写入 demo trace %s（%d 个 span）", id, len(spans))
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
