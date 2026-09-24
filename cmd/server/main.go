// Command drf-scheduler 启动双资源主导份额公平调度 HTTP 服务。
//
// 仅使用 Go 标准库（net/http），无第三方依赖。
package main

import (
	"flag"
	"log"
	"net/http"
	"time"

	"github.com/example/drf-scheduler/internal/scheduler"
	"github.com/example/drf-scheduler/internal/server"
)

func main() {
	addr := flag.String("addr", ":8080", "HTTP listen address")
	cpuCap := flag.Int64("capacity-cpu", 10000, "cluster CPU capacity in millicores")
	memCap := flag.Int64("capacity-mem", 10000, "cluster memory capacity in MiB")
	flag.Parse()

	sch, err := scheduler.New(scheduler.Resources{CPU: *cpuCap, Mem: *memCap})
	if err != nil {
		log.Fatalf("init scheduler: %v", err)
	}

	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           server.New(sch),
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	log.Printf("DRF scheduler listening on %s (capacity cpu=%dm mem=%dMiB)",
		*addr, *cpuCap, *memCap)
	if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("server error: %v", err)
	}
}
