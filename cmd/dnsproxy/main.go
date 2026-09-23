// dnsproxy 是本地 DNS 查询代理 HTTP 服务。
// 仅通过 UDP 向可控上游发起单问题 A/AAAA 查询，带 TTL 缓存。
package main

import (
	"flag"
	"log"
	"net/http"
	"time"

	"dnsproxy/internal/httpapi"
	"dnsproxy/internal/proxy"
)

func main() {
	listen := flag.String("listen", "127.0.0.1:8080", "HTTP 监听地址")
	upstream := flag.String("upstream", "127.0.0.1:5354", "UDP DNS 上游地址（本地假服务器）")
	timeout := flag.Duration("timeout", 2*time.Second, "等待上游 UDP 响应的超时")
	flag.Parse()

	p, err := proxy.New(proxy.Config{
		UpstreamAddr: *upstream,
		Timeout:      *timeout,
	})
	if err != nil {
		log.Fatalf("create proxy: %v", err)
	}

	srv := &http.Server{
		Addr:              *listen,
		Handler:           httpapi.Handler(p),
		ReadHeaderTimeout: 5 * time.Second,
	}
	log.Printf("DNS proxy HTTP listening on http://%s (upstream udp %s, timeout %s)",
		*listen, *upstream, *timeout)
	if err := srv.ListenAndServe(); err != nil {
		log.Fatal(err)
	}
}
