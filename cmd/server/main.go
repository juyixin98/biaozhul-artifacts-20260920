// Command quorum-server 启动 N 副本读写仲裁模拟的 HTTP 服务。
//
// 仅使用 Go 标准库（net/http），无第三方依赖。
package main

import (
	"flag"
	"log"
	"net/http"
	"time"

	"quorum-demo/api"
	"quorum-demo/quorum"
)

func main() {
	var (
		addr     = flag.String("addr", ":8080", "HTTP 监听地址")
		n        = flag.Int("n", 3, "副本数 N")
		w        = flag.Int("w", 2, "写法定人数 W")
		r        = flag.Int("r", 2, "读法定人数 R")
		rpcDelay = flag.Duration("rpc-delay", 10*time.Millisecond, "模拟单次副本 RPC 的往返延迟")
		deadline = flag.Duration("deadline", 60*time.Millisecond, "协调者等待 W/R 个响应的截止时间")
	)
	flag.Parse()

	cfg := quorum.Config{N: *n, W: *w, R: *r}
	cluster, err := quorum.New(cfg, quorum.WithTiming(*rpcDelay, *deadline))
	if err != nil {
		log.Fatalf("非法配置: %v", err)
	}

	srv := api.NewServer(cluster)
	log.Printf("quorum 模拟器启动: %s (N=%d W=%d R=%d, rpc-delay=%s deadline=%s)",
		*addr, cfg.N, cfg.W, cfg.R, *rpcDelay, *deadline)
	if cfg.W+cfg.R > cfg.N {
		log.Printf("W+R=%d > N=%d：读集合必含最近一次已完成写的参与者；并发写冲突仍需解决，且不自动保证线性一致。", cfg.W+cfg.R, cfg.N)
	} else {
		log.Printf("W+R=%d <= N=%d：读写法定人数可能不相交，允许读到旧值。", cfg.W+cfg.R, cfg.N)
	}

	httpServer := &http.Server{
		Addr:              *addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	if err := httpServer.ListenAndServe(); err != nil {
		log.Fatal(err)
	}
}
