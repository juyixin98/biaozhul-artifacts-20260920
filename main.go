// Command gpu-placement 启动“拓扑感知 GPU 任务放置服务”。
//
// 纯后端 HTTP 服务，仅依赖 Go 标准库。监听地址由环境变量
// GPU_PLACEMENT_ADDR 控制（默认 :8080）。
package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	addr := os.Getenv("GPU_PLACEMENT_ADDR")
	if addr == "" {
		addr = ":8080"
	}

	srv := newServer()

	httpSrv := &http.Server{
		Addr:              addr,
		Handler:           srv.routes(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		log.Printf("GPU 拓扑感知放置服务启动，监听 %s", addr)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("HTTP 服务异常: %v", err)
		}
	}()

	<-stop
	log.Printf("收到退出信号，开始优雅关闭…")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(ctx); err != nil {
		log.Printf("优雅关闭超时: %v", err)
	}
}
