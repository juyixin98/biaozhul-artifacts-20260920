// Package service 实现本地确定性构建服务。
//
// 服务本身不执行任何外部命令，只对调用方显式指定的本地目录做 tar 打包。
// 工作目录（构建暂存、清单）与缓存目录（内容寻址的制品）物理分离：
//
//	workDir/
//	  manifests/<build-id>.json   每次构建的清单
//	  tmp/<build-id>              构建期暂存文件
//	cacheDir/
//	  artifacts/<sha256>.tar      以制品字节哈希命名的不可变缓存对象
//
// 相同内容的源树产出相同的制品哈希，第二次构建直接命中缓存（硬链接/拷贝），
// 不重新写归档对象。
package service

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"reproducible-archive/internal/archive"
)

// BuildRequest 是发起一次构建的参数。
type BuildRequest struct {
	// SourceDir 待打包的本地源目录（必须为绝对路径）。
	SourceDir string `json:"source_dir"`
	// OutputPath 制品落盘位置（必须为绝对路径；文件会被原子替换）。
	OutputPath string `json:"output_path"`
	// PreserveExec 是否保留可执行位（归一化为 0755）。
	PreserveExec bool `json:"preserve_exec,omitempty"`
	// FixedModTimeUnix 固定修改时间（Unix 秒）；0 表示 Unix epoch。
	FixedModTimeUnix int64 `json:"fixed_modtime_unix,omitempty"`
}

// Manifest 是一次构建的持久化清单，也作为 JSON API 的响应主体。
type Manifest struct {
	BuildID          string          `json:"build_id"`
	Status           string          `json:"status"` // 仅 "succeeded"
	SourceDir        string          `json:"source_dir"`
	OutputPath       string          `json:"output_path"`
	PreserveExec     bool            `json:"preserve_exec"`
	FixedModTimeUnix int64           `json:"fixed_modtime_unix"`
	CacheHit         bool            `json:"cache_hit"`
	ArtifactSHA256   string          `json:"artifact_sha256"`
	ArtifactSize     int64           `json:"artifact_size"`
	FileCount        int             `json:"file_count"`
	DirCount         int             `json:"dir_count"`
	SymlinkCount     int             `json:"symlink_count"`
	Entries          []archive.Entry `json:"entries"`
	CreatedAt        time.Time       `json:"created_at"` // 记录时间，不属于确定性输入
}

var (
	// ErrNotFound 表示构建 ID 不存在。
	ErrNotFound = errors.New("构建不存在")
	// ErrInvalidRequest 表示请求参数不合法（HTTP 400）。
	ErrInvalidRequest = errors.New("非法请求")
)

// Manager 负责构建编排、缓存与清单管理。
type Manager struct {
	workDir  string
	cacheDir string

	mu     sync.Mutex
	builds map[string]*Manifest // 内存索引（启动时从 manifests/ 恢复）
}

// NewManager 准备目录结构并恢复历史清单。
func NewManager(workDir, cacheDir string) (*Manager, error) {
	workDir = filepath.Clean(workDir)
	cacheDir = filepath.Clean(cacheDir)
	for _, d := range []string{
		filepath.Join(workDir, "manifests"),
		filepath.Join(workDir, "tmp"),
		filepath.Join(cacheDir, "artifacts"),
	} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return nil, fmt.Errorf("创建目录 %s 失败: %w", d, err)
		}
	}
	m := &Manager{
		workDir:  workDir,
		cacheDir: cacheDir,
		builds:   make(map[string]*Manifest),
	}
	if err := m.restore(); err != nil {
		return nil, err
	}
	return m, nil
}

// WorkDir / CacheDir 供 HTTP 层或诊断使用。
func (m *Manager) WorkDir() string  { return m.workDir }
func (m *Manager) CacheDir() string { return m.cacheDir }

func (m *Manager) manifestsDir() string { return filepath.Join(m.workDir, "manifests") }
func (m *Manager) tmpDir() string       { return filepath.Join(m.workDir, "tmp") }
func (m *Manager) artifactPath(sha string) string {
	return filepath.Join(m.cacheDir, "artifacts", sha+".tar")
}

// restore 从工作目录恢复历史清单，保证服务重启后 GET 仍可查询。
func (m *Manager) restore() error {
	entries, err := os.ReadDir(m.manifestsDir())
	if err != nil {
		return err
	}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".json") {
			continue
		}
		b, err := os.ReadFile(filepath.Join(m.manifestsDir(), e.Name()))
		if err != nil {
			return err
		}
		var mf Manifest
		if err := json.Unmarshal(b, &mf); err != nil {
			return fmt.Errorf("清单 %s 损坏: %w", e.Name(), err)
		}
		m.builds[mf.BuildID] = &mf
	}
	return nil
}

// validate 校验请求并返回清洗后的字段。
func (req *BuildRequest) validate() error {
	if req.SourceDir == "" {
		return fmt.Errorf("%w: source_dir 必填", ErrInvalidRequest)
	}
	if req.OutputPath == "" {
		return fmt.Errorf("%w: output_path 必填", ErrInvalidRequest)
	}
	if !filepath.IsAbs(filepath.Clean(req.SourceDir)) {
		return fmt.Errorf("%w: source_dir 必须是绝对路径", ErrInvalidRequest)
	}
	if !filepath.IsAbs(filepath.Clean(req.OutputPath)) {
		return fmt.Errorf("%w: output_path 必须是绝对路径", ErrInvalidRequest)
	}
	return nil
}

// newBuildID 生成时间前缀 + 128 位随机后缀的构建 ID。
func newBuildID() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return time.Now().UTC().Format("20060102T150405Z") + "-" + hex.EncodeToString(b[:]), nil
}

// Build 执行一次确定性构建。
func (m *Manager) Build(req BuildRequest) (*Manifest, error) {
	if err := req.validate(); err != nil {
		return nil, err
	}
	src := filepath.Clean(req.SourceDir)
	out := filepath.Clean(req.OutputPath)

	// 预检查源目录，归类为客户端参数错误。
	if info, err := os.Stat(src); err != nil {
		return nil, fmt.Errorf("%w: 源目录不可访问: %v", ErrInvalidRequest, err)
	} else if !info.IsDir() {
		return nil, fmt.Errorf("%w: 源路径不是目录: %s", ErrInvalidRequest, src)
	}

	// 输出不允许落在缓存目录内部，避免外部写操作污染不可变缓存。
	if rel, err := filepath.Rel(m.cacheDir, out); err == nil &&
		(rel == "." || (!strings.HasPrefix(rel, "..") && !filepath.IsAbs(rel))) {
		return nil, fmt.Errorf("%w: output_path 不允许位于缓存目录内", ErrInvalidRequest)
	}

	id, err := newBuildID()
	if err != nil {
		return nil, err
	}

	// 暂存文件位于工作目录，构建成功后才原子移动到输出位置。
	staged := filepath.Join(m.tmpDir(), id+".tar")
	f, err := os.OpenFile(staged, os.O_CREATE|os.O_WRONLY|os.O_EXCL, 0o644)
	if err != nil {
		return nil, fmt.Errorf("创建暂存文件失败: %w", err)
	}

	opts := archive.Options{
		ModTime:      time.Unix(req.FixedModTimeUnix, 0).UTC(),
		PreserveExec: req.PreserveExec,
	}
	summary, werr := archive.WriteTar(src, f, opts)
	closeErr := f.Close()
	if werr != nil {
		_ = os.Remove(staged)
		return nil, werr
	}
	if closeErr != nil {
		_ = os.Remove(staged)
		return nil, fmt.Errorf("关闭暂存文件失败: %w", closeErr)
	}

	sha := summary.ArtifactSHA256
	cachePath := m.artifactPath(sha)
	cacheHit := false
	if _, err := os.Stat(cachePath); err == nil {
		// 内容寻址命中：缓存对象已存在，丢弃本次暂存字节，直接复用缓存。
		cacheHit = true
		if err := os.Remove(staged); err != nil {
			return nil, err
		}
	} else {
		// 发布进缓存（同文件系统用重命名，保证原子）。
		if err := os.MkdirAll(filepath.Dir(cachePath), 0o755); err != nil {
			return nil, err
		}
		if err := os.Rename(staged, cachePath); err != nil {
			return nil, fmt.Errorf("写入缓存失败: %w", err)
		}
	}

	// 从缓存发布到用户输出位置：先写同目录临时文件再原子替换。
	if err := publishFromCache(cachePath, out); err != nil {
		return nil, err
	}

	mf := &Manifest{
		BuildID:          id,
		Status:           "succeeded",
		SourceDir:        src,
		OutputPath:       out,
		PreserveExec:     req.PreserveExec,
		FixedModTimeUnix: req.FixedModTimeUnix,
		CacheHit:         cacheHit,
		ArtifactSHA256:   summary.ArtifactSHA256,
		ArtifactSize:     summary.ArtifactSize,
		FileCount:        summary.FileCount,
		DirCount:         summary.DirCount,
		SymlinkCount:     summary.SymlinkCount,
		Entries:          summary.Entries,
		CreatedAt:        time.Now().UTC(),
	}
	if err := m.persist(mf); err != nil {
		return nil, err
	}

	m.mu.Lock()
	m.builds[id] = mf
	m.mu.Unlock()
	return mf, nil
}

// publishFromCache 把缓存对象拷贝（硬链接优先）到输出路径并原子替换。
func publishFromCache(cachePath, out string) error {
	if err := os.MkdirAll(filepath.Dir(out), 0o755); err != nil {
		return fmt.Errorf("创建输出目录失败: %w", err)
	}
	tmpOut := out + ".tmp-publish"
	_ = os.Remove(tmpOut)
	if err := os.Link(cachePath, tmpOut); err != nil {
		// 跨设备等场景退化为字节拷贝。
		if err := copyFile(cachePath, tmpOut); err != nil {
			return err
		}
	}
	if err := os.Chmod(tmpOut, 0o644); err != nil {
		_ = os.Remove(tmpOut)
		return err
	}
	if err := os.Rename(tmpOut, out); err != nil {
		_ = os.Remove(tmpOut)
		return fmt.Errorf("发布输出文件失败: %w", err)
	}
	return nil
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_EXCL, 0o644)
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		_ = os.Remove(dst)
		return err
	}
	return out.Close()
}

// persist 把清单写入工作目录（临时文件 + 原子重命名）。
func (m *Manager) persist(mf *Manifest) error {
	b, err := json.MarshalIndent(mf, "", "  ")
	if err != nil {
		return err
	}
	final := filepath.Join(m.manifestsDir(), mf.BuildID+".json")
	tmp := final + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, final)
}

// Get 返回指定构建的清单。
func (m *Manager) Get(id string) (*Manifest, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	mf, ok := m.builds[id]
	if !ok {
		return nil, ErrNotFound
	}
	return mf, nil
}

// List 按创建时间倒序返回全部构建清单。
func (m *Manager) List() []*Manifest {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]*Manifest, 0, len(m.builds))
	for _, mf := range m.builds {
		out = append(out, mf)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt.After(out[j].CreatedAt) })
	return out
}
