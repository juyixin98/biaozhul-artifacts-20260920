// Command auditserver 单独运行本进程"假外部审计系统"。
//
// 与业务服务分开部署（独立端口、独立进程），这样业务进程崩溃/被强杀时，
// "外部系统"依然存活并保留已收到的事件——验收时可以清楚看到：
// 业务侧重试可能让外部系统收到重复事件，而本地账本只有一笔。
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

	"idempotentsave/internal/fakeaudit"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:18081", "假审计系统监听地址")
	flag.Parse()

	srv := &http.Server{Addr: *addr, Handler: fakeaudit.New().Handler()}
	go func() {
		log.Printf("fake external audit system listening on http://%s", *addr)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("audit server: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
}
