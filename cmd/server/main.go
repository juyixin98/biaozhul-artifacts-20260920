// Command robot-task-server 启动离线多机器人任务分配 HTTP 服务。
//
// 监听地址由环境变量 ADDR(默认 :8080)指定。
package main

import (
	"log"
	"net/http"
	"os"

	"github.com/example/robot-task/internal/api"
)

func main() {
	addr := os.Getenv("ADDR")
	if addr == "" {
		addr = ":8080"
	}
	srv := &http.Server{
		Addr:    addr,
		Handler: api.Handler(),
	}
	log.Printf("机器人任务分配服务监听于 %s", addr)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("服务退出: %v", err)
	}
}
