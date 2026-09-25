package service

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func mkTree(t *testing.T, root string, mtime time.Time) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(root, "空目录"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "a.txt"), []byte("alpha\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "unicode-文件-λ.txt"), []byte("u\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(filepath.Join(root, "a.txt"), mtime, mtime); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("a.txt", filepath.Join(root, "link")); err != nil {
		t.Fatal(err)
	}
}

func newTestManager(t *testing.T) *Manager {
	t.Helper()
	m, err := NewManager(filepath.Join(t.TempDir(), "work"), filepath.Join(t.TempDir(), "cache"))
	if err != nil {
		t.Fatal(err)
	}
	return m
}

// TestBuildSuccessAndOutput 验证首次构建成功、输出落盘且与缓存对象字节相同。
func TestBuildSuccessAndOutput(t *testing.T) {
	m := newTestManager(t)
	src := filepath.Join(t.TempDir(), "src")
	mkTree(t, src, time.Unix(1000000000, 0))
	out := filepath.Join(t.TempDir(), "out", "artifact.tar")

	mf, err := m.Build(BuildRequest{SourceDir: src, OutputPath: out})
	if err != nil {
		t.Fatalf("首次构建失败: %v", err)
	}
	if mf.CacheHit {
		t.Fatal("首次构建不应命中缓存")
	}
	if mf.ArtifactSHA256 == "" || mf.ArtifactSize == 0 {
		t.Fatal("摘要不完整")
	}

	outBytes, err := os.ReadFile(out)
	if err != nil {
		t.Fatalf("输出文件不存在: %v", err)
	}
	h := sha256.Sum256(outBytes)
	if hex.EncodeToString(h[:]) != mf.ArtifactSHA256 {
		t.Fatal("输出文件哈希与清单不符")
	}

	cacheBytes, err := os.ReadFile(m.artifactPath(mf.ArtifactSHA256))
	if err != nil {
		t.Fatalf("缓存对象不存在: %v", err)
	}
	if hex.EncodeToString(hashed(cacheBytes)) != mf.ArtifactSHA256 {
		t.Fatal("缓存对象内容损坏")
	}

	// 清单已持久化到工作目录。
	if _, err := os.Stat(filepath.Join(m.manifestsDir(), mf.BuildID+".json")); err != nil {
		t.Fatalf("清单未持久化: %v", err)
	}
}

func hashed(b []byte) []byte { h := sha256.Sum256(b); return h[:] }

// TestCacheHit 相同内容（含不同 mtime）第二次构建命中缓存、哈希一致。
func TestCacheHit(t *testing.T) {
	m := newTestManager(t)
	base := t.TempDir()
	src1 := filepath.Join(base, "src1")
	src2 := filepath.Join(base, "src2")
	mkTree(t, src1, time.Unix(1000000000, 0))
	mkTree(t, src2, time.Unix(1700000000, 0))

	mf1, err := m.Build(BuildRequest{SourceDir: src1, OutputPath: filepath.Join(base, "1.tar")})
	if err != nil {
		t.Fatal(err)
	}
	mf2, err := m.Build(BuildRequest{SourceDir: src2, OutputPath: filepath.Join(base, "2.tar")})
	if err != nil {
		t.Fatal(err)
	}
	if mf1.ArtifactSHA256 != mf2.ArtifactSHA256 {
		t.Fatalf("相同内容哈希不一致: %s vs %s", mf1.ArtifactSHA256, mf2.ArtifactSHA256)
	}
	if !mf2.CacheHit {
		t.Fatal("第二次构建应命中缓存")
	}
	if mf1.BuildID == mf2.BuildID {
		t.Fatal("构建 ID 必须唯一")
	}
}

// TestWorkCacheSeparation 缓存与工作目录物理分离：输出位于第三处，
// 缓存目录内只有对象，工作目录内只有清单/暂存。
func TestWorkCacheSeparation(t *testing.T) {
	tmp := t.TempDir()
	work := filepath.Join(tmp, "work")
	cache := filepath.Join(tmp, "cache")
	m, err := NewManager(work, cache)
	if err != nil {
		t.Fatal(err)
	}
	src := filepath.Join(tmp, "src")
	mkTree(t, src, time.Unix(0, 0))
	out := filepath.Join(tmp, "deliver", "a.tar")
	if _, err := m.Build(BuildRequest{SourceDir: src, OutputPath: out}); err != nil {
		t.Fatal(err)
	}

	// 工作目录不得包含任何 .tar。
	if found := findSuffix(t, work, ".tar"); len(found) != 0 {
		t.Fatalf("工作目录中出现 tar 文件: %v", found)
	}
	// 缓存目录不得包含清单 JSON。
	if found := findSuffix(t, cache, ".json"); len(found) != 0 {
		t.Fatalf("缓存目录中出现清单文件: %v", found)
	}
	// 暂存目录必须已清空。
	if entries, err := os.ReadDir(m.tmpDir()); err != nil || len(entries) != 0 {
		t.Fatalf("暂存目录未清空: %v", entries)
	}
}

func findSuffix(t *testing.T, root, suffix string) []string {
	t.Helper()
	var out []string
	err := filepath.Walk(root, func(p string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if !info.IsDir() && strings.HasSuffix(p, suffix) {
			out = append(out, p)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// TestValidationErrors 覆盖参数与输出位置校验。
func TestValidationErrors(t *testing.T) {
	m := newTestManager(t)
	tmp := t.TempDir()
	src := filepath.Join(tmp, "src")
	mkTree(t, src, time.Unix(0, 0))

	_, err := m.Build(BuildRequest{SourceDir: "relative/path", OutputPath: filepath.Join(tmp, "x.tar")})
	if !errors.Is(err, ErrInvalidRequest) {
		t.Fatalf("相对 source_dir 应报 ErrInvalidRequest, 实际: %v", err)
	}
	_, err = m.Build(BuildRequest{SourceDir: src, OutputPath: "relative.tar"})
	if !errors.Is(err, ErrInvalidRequest) {
		t.Fatalf("相对 output_path 应报 ErrInvalidRequest, 实际: %v", err)
	}
	_, err = m.Build(BuildRequest{
		SourceDir:  src,
		OutputPath: filepath.Join(m.CacheDir(), "artifacts", "evil.tar"),
	})
	if !errors.Is(err, ErrInvalidRequest) {
		t.Fatalf("输出位于缓存目录应报错, 实际: %v", err)
	}
	_, err = m.Build(BuildRequest{
		SourceDir:  filepath.Join(tmp, "missing"),
		OutputPath: filepath.Join(tmp, "x.tar"),
	})
	if !errors.Is(err, ErrInvalidRequest) {
		t.Fatalf("源目录不存在应报 ErrInvalidRequest, 实际: %v", err)
	}
}

// TestGetListAndRestore 验证查询、列表与重启后清单恢复。
func TestGetListAndRestore(t *testing.T) {
	tmp := t.TempDir()
	work := filepath.Join(tmp, "work")
	cache := filepath.Join(tmp, "cache")
	m1, err := NewManager(work, cache)
	if err != nil {
		t.Fatal(err)
	}
	src := filepath.Join(tmp, "src")
	mkTree(t, src, time.Unix(0, 0))
	mf, err := m1.Build(BuildRequest{SourceDir: src, OutputPath: filepath.Join(tmp, "a.tar")})
	if err != nil {
		t.Fatal(err)
	}

	if got, err := m1.Get(mf.BuildID); err != nil || got.ArtifactSHA256 != mf.ArtifactSHA256 {
		t.Fatalf("Get 失败: %v", err)
	}
	if _, err := m1.Get("does-not-exist"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("期望 ErrNotFound, 实际: %v", err)
	}
	if len(m1.List()) != 1 {
		t.Fatal("List 长度异常")
	}

	// 新 Manager 模拟服务重启，从工作目录恢复清单。
	m2, err := NewManager(work, cache)
	if err != nil {
		t.Fatal(err)
	}
	got, err := m2.Get(mf.BuildID)
	if err != nil {
		t.Fatalf("重启后无法恢复构建: %v", err)
	}
	if got.ArtifactSHA256 != mf.ArtifactSHA256 {
		t.Fatal("恢复的清单内容不一致")
	}
}

// TestUnsafeSymlinkPropagates 归档层的逃逸错误应原样上抛（HTTP 层据此分类）。
func TestUnsafeSymlinkPropagates(t *testing.T) {
	m := newTestManager(t)
	src := filepath.Join(t.TempDir(), "src")
	if err := os.MkdirAll(src, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("/etc/passwd", filepath.Join(src, "evil")); err != nil {
		t.Fatal(err)
	}
	_, err := m.Build(BuildRequest{
		SourceDir:  src,
		OutputPath: filepath.Join(t.TempDir(), "x.tar"),
	})
	if err == nil || !strings.Contains(err.Error(), "拒绝逃逸符号链接") {
		t.Fatalf("期望逃逸符号链接错误, 实际: %v", err)
	}
}
