// Command server 启动可复现归档打包的本地 HTTP 服务。
//
// 用法示例：
//
//	go run ./cmd/server -addr :8080 -work ./.local/work -cache ./.local/cache
//
// 工作目录与缓存目录分离，均在本地文件系统，不连接任何云平台。
package main

import (
	"context"
	"errors"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"reproducible-archive/internal/httpapi"
	"reproducible-archive/internal/service"
)

func main() {
	var (
		addr     = flag.String("addr", "127.0.0.1:8080", "HTTP 监听地址")
		workDir  = flag.String("work", ".local/work", "工作目录（暂存与清单）")
		cacheDir = flag.String("cache", ".local/cache", "缓存目录（内容寻址制品）")
	)
	flag.Parse()

	// SOURCE_DATE_EPOCH 是可复现构建的事实标准环境变量；此处仅记录，
	// 固定时间以每次请求的 fixed_modtime_unix（默认 0=epoch）为准。
	if v := os.Getenv("SOURCE_DATE_EPOCH"); v != "" {
		log.Printf("提示: 检测到 SOURCE_DATE_EPOCH=%s（请求级 fixed_modtime_unix 优先生效）", v)
	}

	workAbs, err := filepath.Abs(*workDir)
	if err != nil {
		log.Fatalf("解析工作目录失败: %v", err)
	}
	cacheAbs, err := filepath.Abs(*cacheDir)
	if err != nil {
		log.Fatalf("解析缓存目录失败: %v", err)
	}

	mgr, err := service.NewManager(workAbs, cacheAbs)
	if err != nil {
		log.Fatalf("初始化构建服务失败: %v", err)
	}

	srv := &http.Server{
		Addr:              *addr,
		Handler:           httpapi.NewServer(mgr).Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("可复现归档服务监听 %s", *addr)
		log.Printf("工作目录: %s", mgr.WorkDir())
		log.Printf("缓存目录: %s", mgr.CacheDir())
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("HTTP 服务失败: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop
	log.Printf("收到停止信号，正在关闭…")

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Fatalf("优雅关闭失败: %v", err)
	}
	log.Printf("已关闭")
}
