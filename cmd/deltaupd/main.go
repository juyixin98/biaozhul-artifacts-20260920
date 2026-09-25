// deltaupd 是制品差量更新服务：纯本地 HTTP JSON 接口，不访问任何云服务。
//
// 目录约定（缓存 / 状态 / 工作三者分离）：
//
//	-cache  内容寻址缓存（blobs、patches），可整体清空重建
//	-state  持久状态（项目配方、当前制品引用）
//	-work   临时工作目录（构建、补丁暂存），启动时清理残留
//
// 环境变量：
//
//	DELTA_CRASH_STAGE   调试/测试用：在补丁应用的指定阶段让进程崩溃退出
//	DELTA_CRASH_HELPER  测试辅助模式（见 crashHelper）
package main

import (
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strconv"

	"deltaupdate/internal/api"
	"deltaupdate/internal/service"
)

func main() {
	// 测试辅助模式：在独立目录中执行一次补丁应用并按配置阶段崩溃。
	if os.Getenv("DELTA_CRASH_HELPER") == "1" {
		crashHelper()
		return
	}

	var (
		addr     = flag.String("addr", envOr("DELTA_ADDR", "127.0.0.1:18080"), "HTTP 监听地址")
		cacheDir = flag.String("cache", envOr("DELTA_CACHE_DIR", "./data/cache"), "内容寻址缓存目录")
		stateDir = flag.String("state", envOr("DELTA_STATE_DIR", "./data/state"), "状态目录（项目配方、当前引用）")
		workDir  = flag.String("work", envOr("DELTA_WORK_DIR", "./data/work"), "工作目录（临时）")
		spaceCap = flag.Uint64("space-cap", envUint("DELTA_FREE_SPACE_CAP", 0), "模拟磁盘可用空间上限（字节），0 为不限制")
	)
	flag.Parse()

	cfg := service.Config{
		CacheDir:     *cacheDir,
		StateDir:     *stateDir,
		WorkDir:      *workDir,
		FreeSpaceCap: *spaceCap,
	}
	svc, err := service.New(cfg)
	if err != nil {
		log.Fatalf("服务初始化失败: %v", err)
	}
	svc.CrashStage = os.Getenv("DELTA_CRASH_STAGE")

	srv := api.New(svc)
	abs, _ := filepath.Abs(*cacheDir)
	log.Printf("deltaupd 监听 %s（缓存目录 %s）", *addr, abs)
	log.Fatal(http.ListenAndServe(*addr, srv.Handler()))
}

// crashHelper 在隔离目录中执行一次补丁应用，供崩溃恢复测试以子进程方式调用。
// 用法：DELTA_CRASH_HELPER=1 DELTA_CRASH_STAGE=<stage> deltaupd -root <dir> -name <n> -patch <digest>
// 约定：root 下已有 cache/state/work 结构，且补丁与基线制品已入库。
func crashHelper() {
	fs := flag.NewFlagSet("crash-helper", flag.ExitOnError)
	root := fs.String("root", "", "数据根目录")
	name := fs.String("name", "", "制品名")
	patchDigest := fs.String("patch", "", "补丁摘要")
	spaceCap := fs.Uint64("space-cap", 0, "空间上限")
	_ = fs.Parse(os.Args[1:])

	cfg := service.Config{
		CacheDir:     filepath.Join(*root, "cache"),
		StateDir:     filepath.Join(*root, "state"),
		WorkDir:      filepath.Join(*root, "work"),
		FreeSpaceCap: *spaceCap,
	}
	svc, err := service.New(cfg)
	if err != nil {
		fmt.Fprintln(os.Stderr, "init:", err)
		os.Exit(1)
	}
	svc.CrashStage = os.Getenv("DELTA_CRASH_STAGE")
	res, err := svc.ApplyPatch(*name, *patchDigest)
	if err != nil {
		fmt.Fprintln(os.Stderr, "apply:", err)
		os.Exit(1)
	}
	fmt.Printf("applied: %s -> %s (%d bytes)\n", res.OldDigest, res.NewDigest, res.Size)
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envUint(key string, def uint64) uint64 {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.ParseUint(v, 10, 64); err == nil {
			return n
		}
	}
	return def
}
