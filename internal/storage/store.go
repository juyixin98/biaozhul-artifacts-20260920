// Package storage 管理三类彼此分离的目录：
//
//   - CacheDir：内容寻址存储（blobs/ 制品、patches/ 补丁）。对象按 SHA-256
//     命名，写入后 chmod 0444 且绝不原地修改；可整体清空重建。
//   - StateDir：需要持久化的状态——项目配方与 “当前制品” 引用。
//   - WorkDir：构建与补丁应用的临时暂存区；服务启动时清理残留。
//
// 关键不变量：
//   - blob/patch 经“临时文件 + fsync + rename”原子落位，读到的永远是完整对象；
//   - “当前制品”引用经临时文件 + rename 原子切换，任何时刻只指向完整制品；
//   - 所有读取以只读方式打开（os.O_RDONLY，文件权限 0444），不修改正在读的文件。
package storage

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// ErrNotFound 表示对象或引用不存在。
var ErrNotFound = errors.New("对象不存在")

// Store 聚合三个目录。
type Store struct {
	CacheDir string
	StateDir string
	WorkDir  string
}

// New 校验/创建目录结构。
func New(cacheDir, stateDir, workDir string) (*Store, error) {
	s := &Store{CacheDir: cacheDir, StateDir: stateDir, WorkDir: workDir}
	for _, d := range s.dirs() {
		if err := os.MkdirAll(d, 0o755); err != nil {
			return nil, fmt.Errorf("创建目录 %s: %w", d, err)
		}
	}
	return s, nil
}

func (s *Store) dirs() []string {
	return []string{
		s.CacheDir,
		filepath.Join(s.CacheDir, "blobs"),
		filepath.Join(s.CacheDir, "patches"),
		filepath.Join(s.CacheDir, "tmp"),
		s.StateDir,
		filepath.Join(s.StateDir, "projects"),
		filepath.Join(s.StateDir, "refs"),
		s.WorkDir,
	}
}

// ---- 内容寻址对象 ----

func casPath(root string, digest string) (string, error) {
	if len(digest) != sha256.Size*2 {
		return "", fmt.Errorf("摘要长度非法: %q", digest)
	}
	if _, err := hex.DecodeString(digest); err != nil {
		return "", fmt.Errorf("摘要不是十六进制: %q", digest)
	}
	return filepath.Join(root, digest[:2], digest), nil
}

func putCAS(root string, r io.Reader) (string, int64, error) {
	// 先写入同级 tmp 目录的临时文件，算完摘要后 rename 到最终位置。
	tmp, err := os.CreateTemp(filepath.Join(filepath.Dir(root), "tmp"), "cas-*")
	if err != nil {
		return "", 0, err
	}
	tmpName := tmp.Name()
	// 出错时尽力清理临时文件。
	committed := false
	defer func() {
		if !committed {
			tmp.Close()
			os.Remove(tmpName)
		}
	}()

	h := sha256.New()
	n, err := io.Copy(io.MultiWriter(tmp, h), r)
	if err != nil {
		return "", 0, err
	}
	if err := tmp.Sync(); err != nil {
		return "", 0, err
	}
	if err := tmp.Close(); err != nil {
		return "", 0, err
	}
	digest := hex.EncodeToString(h.Sum(nil))
	dst, err := casPath(root, digest)
	if err != nil {
		return "", 0, err
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return "", 0, err
	}
	if err := os.Chmod(tmpName, 0o444); err != nil {
		return "", 0, err
	}
	if err := atomicRename(tmpName, dst); err != nil {
		return "", 0, err
	}
	committed = true
	return digest, n, nil
}

// openCAS 以只读方式打开对象；不存在返回 ErrNotFound。
func openCAS(root, digest string) (*os.File, os.FileInfo, error) {
	p, err := casPath(root, digest)
	if err != nil {
		return nil, nil, err
	}
	f, err := os.Open(p) // O_RDONLY
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil, fmt.Errorf("%w: %s", ErrNotFound, digest)
	}
	if err != nil {
		return nil, nil, err
	}
	fi, err := f.Stat()
	if err != nil {
		f.Close()
		return nil, nil, err
	}
	return f, fi, nil
}

func hasCAS(root, digest string) bool {
	p, err := casPath(root, digest)
	if err != nil {
		return false
	}
	_, err = os.Stat(p)
	return err == nil
}

// PutBlob 从 r 流式写入一个制品，返回其摘要与字节数。
func (s *Store) PutBlob(r io.Reader) (string, int64, error) {
	return putCAS(filepath.Join(s.CacheDir, "blobs"), r)
}

// OpenBlob 只读打开制品。
func (s *Store) OpenBlob(digest string) (*os.File, os.FileInfo, error) {
	return openCAS(filepath.Join(s.CacheDir, "blobs"), digest)
}

// HasBlob 判断制品是否在缓存中。
func (s *Store) HasBlob(digest string) bool {
	return hasCAS(filepath.Join(s.CacheDir, "blobs"), digest)
}

// BlobPath 返回制品在缓存中的绝对路径（只读用途）。
func (s *Store) BlobPath(digest string) (string, error) {
	return casPath(filepath.Join(s.CacheDir, "blobs"), digest)
}

// PutPatch 从 r 流式写入一个补丁，返回其摘要与字节数。
func (s *Store) PutPatch(r io.Reader) (string, int64, error) {
	return putCAS(filepath.Join(s.CacheDir, "patches"), r)
}

// OpenPatch 只读打开补丁。
func (s *Store) OpenPatch(digest string) (*os.File, os.FileInfo, error) {
	return openCAS(filepath.Join(s.CacheDir, "patches"), digest)
}

// HasPatch 判断补丁是否在缓存中。
func (s *Store) HasPatch(digest string) bool {
	return hasCAS(filepath.Join(s.CacheDir, "patches"), digest)
}

// ---- “当前制品”引用 ----

// SetRef 原子地把 name 的当前制品指向 digest。
// 切换前不要求 digest 已存在（调用方先 PutBlob），切换瞬间旧制品仍完整可读。
func (s *Store) SetRef(name, digest string) error {
	if err := validateName(name); err != nil {
		return err
	}
	dir := filepath.Join(s.StateDir, "refs", name)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, "current-*")
	if err != nil {
		return err
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if _, err := tmp.WriteString(digest + "\n"); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	dst := filepath.Join(dir, "current")
	return atomicRename(tmpName, dst)
}

// GetRef 返回 name 的当前制品摘要；未设置返回 ErrNotFound。
func (s *Store) GetRef(name string) (string, error) {
	if err := validateName(name); err != nil {
		return "", err
	}
	b, err := os.ReadFile(filepath.Join(s.StateDir, "refs", name, "current"))
	if errors.Is(err, os.ErrNotExist) {
		return "", fmt.Errorf("%w: %s 无当前制品", ErrNotFound, name)
	}
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(b)), nil
}

// ListRefs 返回 名称 -> 当前摘要 的映射。
func (s *Store) ListRefs() (map[string]string, error) {
	refs := map[string]string{}
	root := filepath.Join(s.StateDir, "refs")
	names, err := os.ReadDir(root)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return refs, nil
		}
		return nil, err
	}
	for _, n := range names {
		if !n.IsDir() {
			continue
		}
		d, err := s.GetRef(n.Name())
		if err == nil {
			refs[n.Name()] = d
		}
	}
	return refs, nil
}

// ---- 工作目录 ----

// WorkPath 返回工作目录下的一个临时路径（已创建目录），供构建/暂存使用。
func (s *Store) WorkPath(prefix string) (string, error) {
	return os.MkdirTemp(s.WorkDir, prefix+"-*")
}

// RemoveWork 删除工作目录下的某次暂存（失败不致命，返回错误供记录）。
func (s *Store) RemoveWork(dir string) error {
	if !within(s.WorkDir, dir) {
		return fmt.Errorf("拒绝清理工作目录之外的路径: %s", dir)
	}
	return os.RemoveAll(dir)
}

// CleanWork 删除工作目录中的所有残留暂存（服务启动恢复时调用）。
func (s *Store) CleanWork() error {
	entries, err := os.ReadDir(s.WorkDir)
	if err != nil {
		return err
	}
	for _, e := range entries {
		_ = os.RemoveAll(filepath.Join(s.WorkDir, e.Name()))
	}
	return nil
}

// ---- 辅助 ----

// atomicRename 在同一目录内以 rename(2) 原子替换，并对目录 fsync 保证落盘。
func atomicRename(src, dst string) error {
	if err := os.Rename(src, dst); err != nil {
		return err
	}
	if d, err := os.Open(filepath.Dir(dst)); err == nil {
		_ = d.Sync()
		_ = d.Close()
	}
	return nil
}

func validateName(name string) error {
	if name == "" || name == "." || name == ".." ||
		strings.ContainsAny(name, `/\`) || name != filepath.Clean(name) {
		return fmt.Errorf("非法名称: %q", name)
	}
	if strings.HasPrefix(name, ".") {
		return fmt.Errorf("非法名称: %q", name)
	}
	return nil
}

func within(base, target string) bool {
	rel, err := filepath.Rel(base, target)
	if err != nil {
		return false
	}
	return rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator))
}

// SortedNames 返回 map 键的排序结果，便于稳定输出。
func SortedNames(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
