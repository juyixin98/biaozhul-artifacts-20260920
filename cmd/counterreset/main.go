// Command counterreset 启动“时序计数器重置”样例后端：
// 加载合成种子数据（可选）、重放 WAL、提供 HTTP 摄入与查询接口。
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"counterreset/internal/httpapi"
	"counterreset/internal/series"
	"counterreset/internal/store"
	"counterreset/internal/wal"
)

// seedRecord 与 examples/seed.jsonl 的每行结构一致。
type seedRecord struct {
	Labels  map[string]string `json:"labels"`
	Samples []series.Sample   `json:"samples"`
}

func main() {
	addr := flag.String("addr", ":8080", "HTTP 监听地址")
	dataDir := flag.String("data-dir", "./data", "WAL 持久化目录")
	seedPath := flag.String("seed", "examples/seed.jsonl", "合成种子数据文件（NDJSON）；置空则不加载")
	flag.Parse()

	st := store.New()

	// 1) 打开 WAL 并重放历史数据。
	walPath := *dataDir + "/counter.wal.jsonl"
	w, err := wal.Open(walPath)
	if err != nil {
		log.Fatalf("打开 WAL 失败: %v", err)
	}
	ok, bad, err := wal.Replay(walPath, func(rec wal.Record) error {
		_, rejected, err := st.Ingest(rec.Labels, rec.Samples)
		if err != nil {
			return err
		}
		if len(rejected) > 0 {
			// 历史日志由本服务写入，理论上不会出现；出现则跳过并告警。
			log.Printf("警告：重放时跳过含非法样本的记录 labels=%v rejected=%d", rec.Labels, len(rejected))
		}
		return nil
	})
	if err != nil {
		log.Fatalf("WAL 重放失败: %v", err)
	}
	if ok > 0 || bad > 0 {
		log.Printf("WAL 重放完成：成功 %d 条，损坏跳过 %d 条", ok, bad)
	}

	// 2) 加载合成种子（仅当没有任何历史数据时；种子仅作演示，
	//    重启后通过 WAL 重放恢复，避免重复播种）。
	if *seedPath != "" && ok == 0 && bad == 0 && len(st.List()) == 0 {
		if err := loadSeed(st, w, *seedPath); err != nil {
			log.Fatalf("加载种子数据失败: %v", err)
		}
	}

	srv := http.Server{
		Addr:              *addr,
		Handler:           httpapi.NewServer(st, w).Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("计数器重置服务启动，监听 %s（WAL: %s）", *addr, walPath)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("HTTP 服务失败: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	log.Printf("收到退出信号，开始优雅关闭…")

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("HTTP 关闭超时: %v", err)
	}
	if err := w.Close(); err != nil {
		log.Printf("WAL 关闭失败: %v", err)
	}
	fmt.Println("已退出")
}

func loadSeed(st *store.Store, w *wal.WAL, path string) error {
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			log.Printf("种子文件不存在，跳过: %s", path)
			return nil
		}
		return err
	}
	defer f.Close()

	dec := json.NewDecoder(f)
	n := 0
	for {
		var rec seedRecord
		if err := dec.Decode(&rec); err != nil {
			if errors.Is(err, io.EOF) {
				break // NDJSON 正常结束
			}
			return fmt.Errorf("解析种子文件失败: %w", err)
		}
		// 与 HTTP 摄入相同路径：先持久化后更新内存。
		if err := w.Append(wal.Record{Labels: rec.Labels, Samples: rec.Samples}); err != nil {
			return err
		}
		if _, rejected, err := st.Ingest(rec.Labels, rec.Samples); err != nil {
			return err
		} else if len(rejected) > 0 {
			return fmt.Errorf("种子数据含非法样本: labels=%v rejected=%d", rec.Labels, len(rejected))
		}
		n++
	}
	log.Printf("合成种子加载完成：%d 条记录（来自 %s）", n, path)
	return nil
}
