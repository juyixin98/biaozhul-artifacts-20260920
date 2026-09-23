// agingqueue-server 是带老化优先级队列的本地 HTTP 服务。
//
// 纯内存状态、无持久化；时钟与执行器可替换的核心库在仓库根包 agingqueue。
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"agingqueue"
)

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func envDur(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}

func main() {
	addr := flag.String("addr", ":8080", "HTTP 监听地址")
	agingStep := flag.Duration("aging-step", envDur("AGING_STEP", time.Second), "等待老化步长（每等待这么久有效优先级 +1）")
	maxConc := flag.Int("max-concurrency", envInt("MAX_CONCURRENCY", 4), "最大并发执行数")
	maxAttempts := flag.Int("max-attempts", envInt("MAX_ATTEMPTS", 3), "默认最大尝试次数")
	backoff := flag.Duration("backoff", envDur("BACKOFF", 200*time.Millisecond), "初始重试退避")
	maxBackoff := flag.Duration("max-backoff", envDur("MAX_BACKOFF", 5*time.Second), "退避上限")
	flag.Parse()

	clock := agingqueue.NewRealClock()
	sched := agingqueue.NewScheduler(agingqueue.Config{
		Clock:              clock,
		AgingStep:          *agingStep,
		MaxConcurrency:     *maxConc,
		DefaultMaxAttempts: *maxAttempts,
		DefaultBackoff:     *backoff,
		MaxBackoff:         *maxBackoff,
	})
	agingqueue.RegisterBuiltinExecutors(sched, clock)
	sched.Start()

	srv := agingqueue.NewServer(sched)
	httpSrv := &http.Server{Addr: *addr, Handler: srv.Handler(), ReadHeaderTimeout: 5 * time.Second}

	go func() {
		log.Printf("agingqueue listening on %s (aging-step=%s concurrency=%d)", *addr, *agingStep, *maxConc)
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("server error: %v", err)
		}
	}()

	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	<-sigCh
	log.Printf("shutting down...")

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(ctx); err != nil {
		log.Printf("http shutdown: %v", err)
	}
	if err := sched.Close(); err != nil {
		log.Printf("scheduler close: %v", err)
	}
	log.Printf("stopped")
}
