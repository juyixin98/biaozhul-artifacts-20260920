// Command server 启动契约兼容性检查的本地 HTTP 服务。
// 仅监听本地回环，不连接任何生产系统。
package main

import (
	"flag"
	"log"
	"net/http"
	"time"

	"contractcheck/internal/clock"
	"contractcheck/internal/httpapi"
	"contractcheck/internal/registry"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "监听地址（默认仅本地回环）")
	flag.Parse()

	store := registry.New()
	srv := httpapi.NewServer(store, clock.Real{})

	httpServer := &http.Server{
		Addr:              *addr,
		Handler:           srv.Mux,
		ReadHeaderTimeout: 5 * time.Second,
	}
	log.Printf("契约兼容性检查服务启动于 http://%s（仅限本地）", *addr)
	if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("服务退出: %v", err)
	}
}
