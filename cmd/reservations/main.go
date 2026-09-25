// Command reservations 启动资源预约冲突求解的本地 HTTP 服务。
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"resourcebooking/internal/clock"
	"resourcebooking/internal/executor"
	"resourcebooking/internal/scheduler"
	"resourcebooking/internal/server"
)

// seedFile 是 --seed 使用的 JSON 文件结构。
type seedFile struct {
	Resources []struct {
		ID       string  `json:"id"`
		Name     string  `json:"name"`
		Capacity []int64 `json:"capacity"`
	} `json:"resources"`
	Reservations []struct {
		ID       string            `json:"id"`
		Resource string            `json:"resource"`
		Start    time.Time         `json:"start"`
		End      time.Time         `json:"end"`
		Demand   scheduler.Demand  `json:"demand"`
		Meta     map[string]string `json:"meta"`
	} `json:"reservations"`
}

func main() {
	addr := flag.String("addr", "127.0.0.1:8080", "HTTP 监听地址")
	seedPath := flag.String("seed", "", "可选：启动时装载的种子 JSON 文件路径")
	fakeClock := flag.String("fake-clock", "", "可选：使用假时钟并设为给定 RFC3339 时刻（用于演示/测试）")
	flag.Parse()

	var clk clock.Clock = clock.Wall{}
	if *fakeClock != "" {
		t, err := time.Parse(time.RFC3339, *fakeClock)
		if err != nil {
			log.Fatalf("无法解析 fake-clock: %v", err)
		}
		clk = clock.NewFake(t)
		log.Printf("使用假时钟，当前时刻固定为 %s", t.UTC().Format(time.RFC3339))
	}

	// 事件日志使用同一个（可能是假的）时钟，保证事件时间戳
	// 与服务观察到的“当前时间”一致。
	eventLog := scheduler.NewEventLog(clk)
	sch := scheduler.New(eventLog)
	srv := server.New(sch, server.Config{
		Clock:         clk,
		Executor:      executor.Noop{},
		DispatchEvery: time.Minute,
	})

	if *seedPath != "" {
		if err := loadSeed(srv, *seedPath); err != nil {
			log.Fatalf("装载种子文件失败: %v", err)
		}
		log.Printf("已从 %s 装载种子数据", *seedPath)
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go srv.Dispatcher().Run(ctx)

	httpSrv := &http.Server{
		Addr:              *addr,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("资源预约服务监听 %s", *addr)
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("HTTP 服务失败: %v", err)
		}
	}()

	<-ctx.Done()
	log.Printf("收到退出信号，正在关闭…")
	shutdownCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := httpSrv.Shutdown(shutdownCtx); err != nil {
		log.Printf("关闭超时: %v", err)
	}
	srv.Dispatcher().Stop()
}

// loadSeed 读取种子文件并通过 HTTP 层同样的时间换算装载资源与预约。
func loadSeed(srv *server.Server, path string) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("读取文件: %w", err)
	}
	var seed seedFile
	if err := json.Unmarshal(data, &seed); err != nil {
		return fmt.Errorf("解析 JSON: %w", err)
	}
	for _, r := range seed.Resources {
		if _, err := srv.Scheduler().AddResource(scheduler.Resource{
			ID: r.ID, Name: r.Name, Capacity: r.Capacity,
		}); err != nil {
			return fmt.Errorf("资源 %s: %w", r.ID, err)
		}
	}
	for _, r := range seed.Reservations {
		iv, err := parseSeedInterval(srv, r.Start, r.End)
		if err != nil {
			return fmt.Errorf("预约 %s: %w", r.ID, err)
		}
		if _, err := srv.Scheduler().Reserve(scheduler.Request{
			ID: r.ID, Resource: r.Resource, Interval: iv, Demand: r.Demand, Meta: r.Meta,
		}); err != nil {
			return fmt.Errorf("预约 %s: %w", r.ID, err)
		}
	}
	return nil
}

// parseSeedInterval 通过 server 的时间换算装载半开区间。
func parseSeedInterval(srv *server.Server, start, end time.Time) (scheduler.Interval, error) {
	a, err := srv.ToTick(start)
	if err != nil {
		return scheduler.Interval{}, err
	}
	b, err := srv.ToTick(end)
	if err != nil {
		return scheduler.Interval{}, err
	}
	iv := scheduler.Interval{Start: a, End: b}
	if err := iv.Validate(); err != nil {
		return scheduler.Interval{}, err
	}
	return iv, nil
}
