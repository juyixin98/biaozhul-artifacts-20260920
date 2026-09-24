// Command server 在本地模拟两个组件：
// 锁服务（带围栏令牌的租约）与资源服务（受令牌保护的存储）。
// 两者是独立 HTTP 服务，监听不同端口，状态各自落盘。
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"fencingdemo/clock"
	"fencingdemo/locksvc"
	"fencingdemo/resourcesvc"
)

func main() {
	lockAddr := flag.String("lock-addr", "127.0.0.1:18080", "锁服务监听地址")
	resAddr := flag.String("res-addr", "127.0.0.1:18081", "资源服务监听地址")
	stateDir := flag.String("state-dir", "./data", "状态持久化目录（重启后令牌不回退）")
	flag.Parse()

	// 注入真实时钟；测试中组件可改用 clock.Fake。
	clk := clock.Real{}

	lockSvc, err := locksvc.NewService(clk, *stateDir)
	if err != nil {
		log.Fatalf("init lock service: %v", err)
	}
	resSvc, err := resourcesvc.NewService(*stateDir)
	if err != nil {
		log.Fatalf("init resource service: %v", err)
	}

	lockServer := &http.Server{Addr: *lockAddr, Handler: lockSvc.HTTPHandler()}
	resServer := &http.Server{Addr: *resAddr, Handler: resSvc.HTTPHandler()}

	go mustServe("lock", lockServer)
	go mustServe("resource", resServer)

	log.Printf("lock service:     http://%s (counter=%d)", *lockAddr, lockSvc.Counter())
	log.Printf("resource service: http://%s", *resAddr)
	log.Printf("state dir: %s (Ctrl+C 退出，状态保留)", *stateDir)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	<-ctx.Done()
	log.Println("shutting down ...")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = lockServer.Shutdown(shutdownCtx)
	_ = resServer.Shutdown(shutdownCtx)
}

func mustServe(name string, srv *http.Server) {
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("%s service: %v", name, err)
	}
}
