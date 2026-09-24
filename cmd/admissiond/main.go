// Command admissiond 是镜像离线安全策略准入服务（纯后端 HTTP）。
//
// 启动时加载受信公钥与（冻结的）允许列表，编译 OPA 策略并计算策略哈希；
// 之后每次评估都在响应与持久化报告中回带同一版本号与哈希。
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
	"path/filepath"
	"syscall"
	"time"

	"mirrorsec/internal/api"
	"mirrorsec/internal/evaluate"
	"mirrorsec/internal/model"
	"mirrorsec/internal/policy"
	"mirrorsec/internal/store"
)

type trustFile struct {
	VerifierKey        string   `json:"verifierKey"`
	ExemptionAuthority string   `json:"exemptionAuthorityKey"`
	TrustedSignerKeys  []string `json:"trustedSignerKeys"`
}

func main() {
	var (
		addr      = flag.String("addr", "127.0.0.1:8080", "监听地址")
		trustPath = flag.String("trust", "examples/keys/trust.json", "受信公钥配置文件")
		reports   = flag.String("reports", "data/reports", "报告持久化目录（追加式）")
		allowPath = flag.String("allowlist", "", "允许列表 JSON 路径（默认使用随二进制冻结的嵌入副本）")
	)
	flag.Parse()

	trust, err := loadTrust(*trustPath)
	if err != nil {
		log.Fatalf("加载信任配置失败: %v", err)
	}
	allowlist, err := loadAllowlist(*allowPath)
	if err != nil {
		log.Fatalf("加载允许列表失败: %v", err)
	}

	ctx := context.Background()
	engine, err := evaluate.NewEngine(ctx, trust, allowlist, evaluate.RealClock{})
	if err != nil {
		log.Fatalf("初始化策略引擎失败: %v", err)
	}
	st, err := store.New(*reports)
	if err != nil {
		log.Fatalf("初始化报告存储失败: %v", err)
	}
	srv := &http.Server{
		Addr:              *addr,
		Handler:           api.NewServer(engine, st).Router(),
		ReadHeaderTimeout: 10 * time.Second,
	}

	go func() {
		log.Printf("镜像离线准入服务启动: %s", *addr)
		log.Printf("策略版本冻结: %s  哈希: %s", engine.Version(), engine.Hash())
		log.Printf("允许列表条目: %d  报告目录: %s", len(allowlist), *reports)
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			log.Fatalf("HTTP 服务失败: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	<-stop
	shutdownCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("关闭超时: %v", err)
	}
	log.Print("服务已停止（历史报告未被修改）")
}

func loadTrust(path string) (evaluate.Trust, error) {
	var t evaluate.Trust
	data, err := os.ReadFile(path)
	if err != nil {
		return t, fmt.Errorf("读取 %s: %w", path, err)
	}
	var f trustFile
	if err := json.Unmarshal(data, &f); err != nil {
		return t, fmt.Errorf("解析信任配置: %w", err)
	}
	// 相对路径相对于信任配置文件所在目录解析（与 CWD 无关）。
	resolve := func(ref string) ([]byte, error) { return loadKeyMaterial(ref, filepath.Dir(path)) }
	t.Verifier, err = resolve(f.VerifierKey)
	if err != nil {
		return t, fmt.Errorf("验签器公钥: %w", err)
	}
	t.ExemptionAuthority, err = resolve(f.ExemptionAuthority)
	if err != nil {
		return t, fmt.Errorf("豁免机构公钥: %w", err)
	}
	if len(f.TrustedSignerKeys) == 0 {
		return t, errors.New("受信镜像签名者列表为空（fail-closed）")
	}
	for i, ref := range f.TrustedSignerKeys {
		pem, err := resolve(ref)
		if err != nil {
			return t, fmt.Errorf("受信签名者[%d]: %w", i, err)
		}
		t.TrustedSignerPEMs = append(t.TrustedSignerPEMs, pem)
	}
	return t, nil
}

// loadKeyMaterial 支持直接内联 PEM，或 "@path" / "path" 文件引用。
// 相对路径优先按 baseDir（信任配置目录）解析，再回退到 CWD。
func loadKeyMaterial(ref string, baseDir string) ([]byte, error) {
	if ref == "" {
		return nil, errors.New("空的公钥引用")
	}
	if ref[0] == '-' {
		return []byte(ref), nil
	}
	path := ref
	if path[0] == '@' {
		path = path[1:]
	}
	if !filepath.IsAbs(path) {
		candidate := filepath.Join(baseDir, path)
		if data, err := os.ReadFile(candidate); err == nil {
			return data, nil
		}
	}
	if data, err := os.ReadFile(path); err == nil {
		return data, nil
	}
	return nil, fmt.Errorf("公钥文件不可读: %s", ref)
}

func loadAllowlist(path string) ([]model.AllowlistEntry, error) {
	if path == "" {
		return policy.DefaultAllowlist()
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var entries []model.AllowlistEntry
	if err := json.Unmarshal(data, &entries); err != nil {
		return nil, err
	}
	if len(entries) == 0 {
		return nil, errors.New("允许列表为空")
	}
	return entries, nil
}
