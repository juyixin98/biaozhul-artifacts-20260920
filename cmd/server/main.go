// Command metricsink 启动指标保留层压缩后端：
// HTTP 摄入/查询 + 内存三层存储 + 本地 WAL/快照持久化。
package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"metricsink/internal/api"
	"metricsink/internal/store"
)

func main() {
	addr := flag.String("addr", ":8080", "HTTP 监听地址")
	dataDir := flag.String("data", "./data", "数据目录（快照与操作日志）")
	snapshotEvery := flag.Duration("snapshot-interval", 60*time.Second,
		"自动快照间隔；0 表示关闭（关闭进程时仍会落一次快照）")
	flag.Parse()

	st, err := store.Open(*dataDir)
	if err != nil {
		log.Fatalf("打开存储失败: %v", err)
	}

	srv := &http.Server{
		Addr:              *addr,
		Handler:           api.NewServer(st).Mux,
		ReadHeaderTimeout: 5 * time.Second,
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)

	go func() {
		log.Printf("metricsink 监听 %s，数据目录 %s", *addr, *dataDir)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("HTTP 服务失败: %v", err)
		}
	}()

	var ticker *time.Ticker
	if *snapshotEvery > 0 {
		ticker = time.NewTicker(*snapshotEvery)
	}
	for {
		select {
		case <-stop:
			log.Printf("收到退出信号，关闭中…")
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			if err := srv.Shutdown(ctx); err != nil {
				log.Printf("HTTP 关闭超时: %v", err)
			}
			cancel()
			if err := st.Close(); err != nil {
				log.Printf("落快照失败: %v", err)
				os.Exit(1)
			}
			log.Printf("已安全退出")
			return
		case <-tickerCh(ticker):
			if err := st.SaveSnapshot(); err != nil {
				log.Printf("自动快照失败: %v", err)
			} else {
				log.Printf("自动快照完成")
			}
		}
	}
}

func tickerCh(t *time.Ticker) <-chan time.Time {
	if t == nil {
		return nil
	}
	return t.C
}
