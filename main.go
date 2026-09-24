// tztrigger 是一个纯后端的“带时区定时触发器”服务：
// 分钟/小时/周几表达式 + IANA 时区，明确处理夏令时缺失/重复时刻，
// 按逻辑触发 ID 去重，支持停机后的补触发上限。
//
// 仅依赖 Go 标准库；时区数据库内嵌固定版本，见 internal/tzdb。
package main

import (
	"context"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"tztrigger/internal/engine"
	"tztrigger/internal/httpapi"
	"tztrigger/internal/tzdb"
)

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}

	eng := engine.New(engine.RealClock{}, time.Second)
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go eng.Run(ctx)

	srv := &http.Server{
		Addr:              ":" + port,
		Handler:           httpapi.New(eng),
		ReadHeaderTimeout: 10 * time.Second,
	}
	go func() {
		<-ctx.Done()
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = srv.Shutdown(shutdownCtx)
	}()

	log.Printf("tztrigger 启动：监听 :%s，内嵌 IANA tz 数据库 %s（zoneinfo.zip sha256 %s）",
		port, tzdb.Version, tzdb.ZipSHA256[:16]+"…")
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatal(err)
	}
	log.Print("tztrigger 已关闭")
}
