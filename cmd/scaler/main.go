// scaler 服务：以 HTTP 方式提供离线扩缩容回放接口。
package main

import (
	"flag"
	"log"
	"net/http"

	"offline-scaler/internal/api"
)

func main() {
	addr := flag.String("addr", ":8080", "监听地址")
	flag.Parse()

	srv := &http.Server{
		Addr:    *addr,
		Handler: api.Handler(),
	}
	log.Printf("离线扩缩容控制器监听 %s（POST /api/v1/replay, GET /healthz）", *addr)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
}
