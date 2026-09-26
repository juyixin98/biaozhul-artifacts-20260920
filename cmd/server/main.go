// Command idempotency-server 启动本地幂等存款 HTTP 服务。
//
// "外部依赖"（假审计系统）是一个独立进程（cmd/auditserver），通过真实 HTTP
// 访问；本服务不内嵌任何外部系统。默认真实时钟，加 -fake-clock 后可通过
// POST /admin/clock/advance 拨快时间以驱动占位 TTL。
//
// 纯本地、不接生产系统。
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

	"idempotentsave/internal/api"
	"idempotentsave/internal/auditclient"
	"idempotentsave/internal/clock"
	"idempotentsave/internal/service"
	"idempotentsave/internal/store"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:18080", "业务服务监听地址")
	auditBase := flag.String("audit-base-url", "http://127.0.0.1:18081", "外部假审计系统基地址")
	dataDir := flag.String("data-dir", "./data", "WAL 数据目录")
	fakeClock := flag.Bool("fake-clock", false, "使用可控假时钟（通过 /admin/clock/advance 拨快）")
	pendingTTL := flag.Duration("pending-ttl", 30*time.Second, "处理中占位的存活时间（TTL）")
	waitMax := flag.Duration("wait-max", 10*time.Second, "处理中重复请求最长等待时间")
	flag.Parse()

	var clk clock.Clock = clock.Real{}
	var now func() time.Time
	if *fakeClock {
		if err := os.MkdirAll(*dataDir, 0o755); err != nil {
			log.Fatalf("create data dir: %v", err)
		}
		fc, err := clock.LoadOrCreateFake(*dataDir)
		if err != nil {
			log.Fatalf("load fake clock: %v", err)
		}
		clk = fc
		now = fc.Now
		log.Printf("using a FAKE controllable clock starting at %s", fc.Now().Format(time.RFC3339Nano))
	} else {
		now = time.Now
	}

	st, err := store.Open(store.Options{Dir: *dataDir, PendingTTL: *pendingTTL, Now: now})
	if err != nil {
		log.Fatalf("open store: %v", err)
	}
	defer func() { _ = st.Close() }()

	svc := service.New(st, auditclient.New(*auditBase), clk)
	srv := api.New(api.Config{
		Store: st, Service: svc, AuditBaseURL: *auditBase,
		Clock: clk, WaitMax: *waitMax, RealClock: !*fakeClock,
	})

	bizHTTP := &http.Server{Addr: *addr, Handler: srv.Handler()}
	go func() {
		log.Printf("idempotency service listening on http://%s (data=%s, pendingTTL=%s, audit=%s)",
			*addr, *dataDir, *pendingTTL, *auditBase)
		if err := bizHTTP.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("business server: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	log.Printf("shutting down ...")

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	_ = bizHTTP.Shutdown(ctx)
}
