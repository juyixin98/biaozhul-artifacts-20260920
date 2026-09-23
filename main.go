// Command batchagg 启动批处理聚合调度的本地 HTTP 服务。
//
// 所有参数均可通过命令行 flag 或环境变量（BATCHAGG_ 前缀）覆盖，
// 例如 BATCHAGG_ADDR=:9090。
package main

import (
	"context"
	"encoding/json"
	"flag"
	"log"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"batchagg/batch"
	"batchagg/server"
)

func main() {
	cfg := parseFlags()

	logger := log.New(os.Stdout, "", log.LstdFlags|log.Lmicroseconds)
	logSink := batch.EventSinkFunc(func(e batch.Event) {
		data, err := json.Marshal(e)
		if err != nil {
			return
		}
		logger.Printf("event %s", data)
	})

	srv, err := server.New(server.Config{
		Addr:          cfg.addr,
		MaxCount:      cfg.maxCount,
		MaxBatchBytes: cfg.maxBatchBytes,
		MaxItemBytes:  cfg.maxItemBytes,
		MaxWait:       cfg.maxWait,
		ExecLatency:   cfg.execLatency,
		ExtraSink:     logSink,
	})
	if err != nil {
		log.Fatalf("failed to create server: %v", err)
	}

	srv.Start()

	// 快速感知端口占用等启动错误（正常监听时该通道保持阻塞）。
	select {
	case err := <-srv.ListenErr():
		if err != nil {
			log.Fatalf("http server failed: %v", err)
		}
	case <-time.After(150 * time.Millisecond):
	}

	logger.Printf("batch aggregation server listening on %s (max_count=%d max_batch_bytes=%d max_item_bytes=%d max_wait=%s exec_latency=%s)",
		cfg.addr, cfg.maxCount, cfg.maxBatchBytes, cfg.maxItemBytes, cfg.maxWait, cfg.execLatency)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	<-ctx.Done()
	logger.Printf("shutdown signal received, draining ...")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), cfg.shutdownTimeout)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Fatalf("graceful shutdown failed: %v", err)
	}
	logger.Printf("shutdown complete; final metrics: %s", mustJSON(srv.Batcher().Stats()))
}

type appConfig struct {
	addr            string
	maxCount        int
	maxBatchBytes   int
	maxItemBytes    int
	maxWait         time.Duration
	execLatency     time.Duration
	shutdownTimeout time.Duration
}

func parseFlags() appConfig {
	c := appConfig{
		addr:            ":8080",
		maxCount:        8,
		maxBatchBytes:   4096,
		maxItemBytes:    2048,
		maxWait:         50 * time.Millisecond,
		execLatency:     20 * time.Millisecond,
		shutdownTimeout: 10 * time.Second,
	}

	flag.StringVar(&c.addr, "addr", envStr("ADDR", c.addr), "HTTP listen address (env BATCHAGG_ADDR)")
	flag.IntVar(&c.maxCount, "max-count", envInt("MAX_COUNT", c.maxCount), "max items per batch (env BATCHAGG_MAX_COUNT)")
	flag.IntVar(&c.maxBatchBytes, "max-batch-bytes", envInt("MAX_BATCH_BYTES", c.maxBatchBytes), "max total bytes per batch (env BATCHAGG_MAX_BATCH_BYTES)")
	flag.IntVar(&c.maxItemBytes, "max-item-bytes", envInt("MAX_ITEM_BYTES", c.maxItemBytes), "reject single items larger than this (env BATCHAGG_MAX_ITEM_BYTES)")
	flag.DurationVar(&c.maxWait, "max-wait", envDur("MAX_WAIT", c.maxWait), "max wait from first item to flush (env BATCHAGG_MAX_WAIT)")
	flag.DurationVar(&c.execLatency, "exec-latency", envDur("EXEC_LATENCY", c.execLatency), "simulated executor latency per batch (env BATCHAGG_EXEC_LATENCY)")
	flag.DurationVar(&c.shutdownTimeout, "shutdown-timeout", envDur("SHUTDOWN_TIMEOUT", c.shutdownTimeout), "graceful shutdown timeout (env BATCHAGG_SHUTDOWN_TIMEOUT)")
	flag.Parse()
	return c
}

func envStr(name, def string) string {
	if v, ok := os.LookupEnv("BATCHAGG_" + name); ok && strings.TrimSpace(v) != "" {
		return strings.TrimSpace(v)
	}
	return def
}

func envInt(name string, def int) int {
	if v, ok := os.LookupEnv("BATCHAGG_" + name); ok {
		if n, err := strconv.Atoi(strings.TrimSpace(v)); err == nil {
			return n
		}
	}
	return def
}

func envDur(name string, def time.Duration) time.Duration {
	if v, ok := os.LookupEnv("BATCHAGG_" + name); ok {
		if d, err := time.ParseDuration(strings.TrimSpace(v)); err == nil {
			return d
		}
	}
	return def
}

func mustJSON(v any) string {
	b, err := json.Marshal(v)
	if err != nil {
		return "<unmarshalable>"
	}
	return string(b)
}
