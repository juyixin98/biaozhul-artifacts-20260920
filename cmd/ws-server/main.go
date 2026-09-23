// ws-server 是工作窃取执行器的本地 HTTP 服务入口。
//
// 默认创建一个名为 "default" 的执行器（worker 数可用 -workers 配置），
// 通过 JSON/SSE 接口提交任务、查询状态、取消任务、订阅事件、管理执行器。
package main

import (
	"context"
	"errors"
	"flag"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"worksteal/internal/server"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "HTTP listen address")
	workers := flag.Int("workers", 0, "worker count for the default executor (0 = NumCPU)")
	deque := flag.String("deque", "chaselev", "deque implementation: chaselev|mutex")
	level := flag.String("log", "info", "log level: debug|info|warn|error")
	flag.Parse()

	var lvl slog.Level
	switch *level {
	case "debug":
		lvl = slog.LevelDebug
	case "warn":
		lvl = slog.LevelWarn
	case "error":
		lvl = slog.LevelError
	default:
		lvl = slog.LevelInfo
	}
	log := slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: lvl}))

	srv := server.New(log)
	if _, err := srv.CreateExecutor("default", *workers, *deque); err != nil {
		log.Error("create default executor", "err", err)
		os.Exit(1)
	}

	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		log.Info("http server listening", "addr", *addr, "workers", *workers, "deque", *deque)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Error("http server", "err", err)
			os.Exit(1)
		}
	}()

	<-ctx.Done()
	log.Info("shutdown signal received")
	shCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(shCtx); err != nil {
		log.Error("http shutdown", "err", err)
	}
	srv.Close()
	log.Info("server stopped")
}
