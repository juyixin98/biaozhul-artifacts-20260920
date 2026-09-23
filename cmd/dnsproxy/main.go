// Command dnsproxy runs the HTTP front-end of the DNS proxy.
//
// Endpoints:
//
//	GET    /resolve?name=example.com&type=A
//	GET    /cache
//	DELETE /cache
//	GET    /healthz
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

	"dnscomp-proxy/internal/cache"
	"dnscomp-proxy/internal/httpapi"
	"dnscomp-proxy/internal/proxy"
)

func main() {
	listen := flag.String("listen", "127.0.0.1:8080", "HTTP listen address")
	upstream := flag.String("upstream", "127.0.0.1:1053", "UDP DNS upstream address")
	timeout := flag.Duration("timeout", 2*time.Second, "per-query upstream timeout")
	sweep := flag.Duration("sweep", 10*time.Second, "background cache sweep interval")
	flag.Parse()

	logger := log.New(os.Stdout, "[dnsproxy] ", log.LstdFlags|log.Lmicroseconds)

	c := cache.New()
	p, err := proxy.New(proxy.Config{
		UpstreamAddr: *upstream,
		Timeout:      *timeout,
	}, c)
	if err != nil {
		logger.Fatalf("proxy init: %v", err)
	}

	stopSweep := make(chan struct{})
	go c.Sweep(*sweep, stopSweep)

	srv := &http.Server{
		Addr:              *listen,
		Handler:           httpapi.NewServer(p, logger),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		logger.Printf("HTTP listening on %s, upstream udp %s, timeout %s", *listen, *upstream, *timeout)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			logger.Fatalf("http: %v", err)
		}
	}()

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	<-sig
	logger.Printf("shutting down")
	close(stopSweep)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	_ = srv.Shutdown(ctx)
}
