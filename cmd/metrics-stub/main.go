// metrics-stub：受控指标桩服务进程。
package main

import (
	"fmt"
	"net/http"
	"os"
	"time"

	"rollout/internal/stub"
)

func main() {
	addr := ":18081"
	if v := os.Getenv("STUB_ADDR"); v != "" {
		addr = v
	}
	srv := &http.Server{
		Addr:              addr,
		Handler:           stub.New().Router(),
		ReadHeaderTimeout: 5 * time.Second,
	}
	fmt.Fprintf(os.Stderr, "%s metrics stub listening on %s\n", time.Now().Format(time.RFC3339), addr)
	if err := srv.ListenAndServe(); err != nil {
		fmt.Fprintf(os.Stderr, "fatal: %v\n", err)
		os.Exit(1)
	}
}
