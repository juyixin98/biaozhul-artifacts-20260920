// Package storage 负责打样文件的本地落盘：原子写入、校验与路径安全。
//
// 设计要点：
//   - 所有文件先写入 root 下的 tmp 目录（同文件系统），校验通过后 rename 到目标路径，
//     避免半成品文件出现在正式目录；
//   - 存储路径完全由服务生成；任何相对路径解析都限定在 root 之内，拒绝越界；
//   - 大小、文件头(Magic Number)、SHA-256 全部在落盘阶段校验；
//   - 调用方负责数据库事务；DB 失败时可凭 RelPath 调 Delete 清理已提交文件。
package storage

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

var (
	// ErrTooLarge 文件超过大小上限。
	ErrTooLarge = errors.New("file too large")
	// ErrEmptyFile 空文件。
	ErrEmptyFile = errors.New("empty file")
	// ErrUnsupportedKind 不是允许的 PDF/PNG 类型（按文件头判定，而非扩展名）。
	ErrUnsupportedKind = errors.New("unsupported file type: only local PDF and PNG are accepted")
	// ErrBadHeader 文件扩展名/声明与文件头不一致。
	ErrBadHeader = errors.New("file content does not match declared type")
	// ErrHashMismatch 客户端声明的 SHA-256 与实际内容不一致。
	ErrHashMismatch = errors.New("sha-256 mismatch")
	// ErrPathEscape 目标路径逃逸出存储根目录。
	ErrPathEscape = errors.New("storage path escapes root")
)

// Kind 为允许的文件种类。
type Kind string

const (
	KindPDF Kind = "pdf"
	KindPNG Kind = "png"
)

// ContentType 返回对应的 MIME 类型。
func (k Kind) ContentType() string {
	switch k {
	case KindPDF:
		return "application/pdf"
	case KindPNG:
		return "image/png"
	}
	return "application/octet-stream"
}

// Ext 返回规范扩展名。
func (k Kind) Ext() string { return string(k) }

var (
	pngSignature = []byte{0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A}
	pdfSignature = []byte("%PDF-")
)

// LocalStore 是基于本地文件系统的存储器。
type LocalStore struct {
	root string
	tmp  string
}

// NewLocalStore 创建存储目录（含临时目录）。
func NewLocalStore(root string) (*LocalStore, error) {
	root = filepath.Clean(root)
	if !filepath.IsAbs(root) {
		abs, err := filepath.Abs(root)
		if err != nil {
			return nil, err
		}
		root = abs
	}
	tmp := filepath.Join(root, "tmp")
	if err := os.MkdirAll(tmp, 0o750); err != nil {
		return nil, fmt.Errorf("create storage dirs: %w", err)
	}
	return &LocalStore{root: root, tmp: tmp}, nil
}

// Root 返回存储根目录绝对路径。
func (s *LocalStore) Root() string { return s.root }

// saveResult 是一次临时写入的产物。
type saveResult struct {
	tmpPath string
	kind    Kind
	size    int64
	sha256  string
}

// writeTemp 把 src 流式写入临时文件，同时校验大小、文件头并计算 SHA-256。
// expectSHA 非空时还会与客户端声明值比对。临时文件在返回错误时由本方法负责清理。
func (s *LocalStore) writeTemp(src io.Reader, maxSize int64, expectSHA string) (*saveResult, error) {
	f, err := os.CreateTemp(s.tmp, "upload-*")
	if err != nil {
		return nil, fmt.Errorf("create temp file: %w", err)
	}
	tmpName := f.Name()
	abort := func(cause error) (*saveResult, error) {
		_ = f.Close()
		_ = os.Remove(tmpName)
		return nil, cause
	}

	h := sha256.New()
	// LimitReader 在超限时多读 1 字节即可发现，避免请求体无限拷贝。
	written, err := io.Copy(io.MultiWriter(f, h), io.LimitReader(src, maxSize+1))
	if err != nil {
		return abort(fmt.Errorf("write temp file: %w", err))
	}
	if written > maxSize {
		return abort(ErrTooLarge)
	}
	if written == 0 {
		return abort(ErrEmptyFile)
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return abort(err)
	}
	head := make([]byte, 8)
	n, err := io.ReadFull(f, head)
	if err != nil && !errors.Is(err, io.ErrUnexpectedEOF) && !errors.Is(err, io.EOF) {
		return abort(err)
	}
	head = head[:n]
	if err := f.Sync(); err != nil {
		return abort(err)
	}
	if err := f.Close(); err != nil {
		_ = os.Remove(tmpName)
		return nil, err
	}

	kind, err := detectKind(head)
	if err != nil {
		_ = os.Remove(tmpName)
		return nil, err
	}
	sum := hex.EncodeToString(h.Sum(nil))
	if expectSHA != "" && !strings.EqualFold(normalizeHex(expectSHA), sum) {
		_ = os.Remove(tmpName)
		return nil, ErrHashMismatch
	}
	return &saveResult{tmpPath: tmpName, kind: kind, size: written, sha256: sum}, nil
}

// StagedFile 是已写入临时目录并通过全部内容校验、尚未提交到正式目录的上传。
// 用于“先写临时文件 → 提交数据库事务 → commit 落盘”的可恢复上传流程。
type StagedFile struct {
	tmpPath string
	kind    Kind
	size    int64
	sha256  string
	store   *LocalStore
}

// Stage 校验并暂存上传内容。
func (s *LocalStore) Stage(src io.Reader, maxSize int64, expectSHA string) (*StagedFile, error) {
	res, err := s.writeTemp(src, maxSize, expectSHA)
	if err != nil {
		return nil, err
	}
	return &StagedFile{tmpPath: res.tmpPath, kind: res.kind, size: res.size, sha256: res.sha256, store: s}, nil
}

// Kind 检测到的文件种类。
func (f *StagedFile) Kind() Kind { return f.kind }

// Size 文件大小。
func (f *StagedFile) Size() int64 { return f.size }

// SHA256 实际内容摘要。
func (f *StagedFile) SHA256() string { return f.sha256 }

// Commit 将暂存文件移动到 root 下的 relPath（同文件系统 rename，原子）。
func (f *StagedFile) Commit(relPath string) error {
	if err := f.store.commit(f.tmpPath, relPath); err != nil {
		return err
	}
	f.tmpPath = "" // 已移动，Release 不再清理
	return nil
}

// Release 放弃暂存文件（事务失败时调用）。已 Commit 后调用为空操作。
func (f *StagedFile) Release() {
	if f.tmpPath != "" {
		_ = os.Remove(f.tmpPath)
		f.tmpPath = ""
	}
}

// commit 校验 relPath 不越界后执行原子 rename。
func (s *LocalStore) commit(tmpAbs, relPath string) error {
	target, err := s.resolve(relPath)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(target), 0o750); err != nil {
		return fmt.Errorf("create target dir: %w", err)
	}
	if err := os.Rename(tmpAbs, target); err != nil {
		return fmt.Errorf("commit file: %w", err)
	}
	return nil
}

// resolve 把服务生成的相对路径解析为 root 内的绝对路径，拒绝任何越界。
func (s *LocalStore) resolve(relPath string) (string, error) {
	if relPath == "" || filepath.IsAbs(relPath) {
		return "", ErrPathEscape
	}
	clean := filepath.Clean(filepath.FromSlash(relPath))
	if clean == "." || clean == ".." || strings.HasPrefix(clean, ".."+string(os.PathSeparator)) {
		return "", ErrPathEscape
	}
	abs := filepath.Join(s.root, clean)
	// 二次确认：解析结果相对 root 仍不能跳出。
	rel, err := filepath.Rel(s.root, abs)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
		return "", ErrPathEscape
	}
	return abs, nil
}

// Open 打开 relPath 指向的文件供下载；越界或不存在返回错误。
func (s *LocalStore) Open(relPath string) (*os.File, error) {
	abs, err := s.resolve(relPath)
	if err != nil {
		return nil, err
	}
	return os.Open(abs)
}

// Delete 删除 relPath（新修订提交失败时的补偿动作）。不存在不算错误。
func (s *LocalStore) Delete(relPath string) error {
	abs, err := s.resolve(relPath)
	if err != nil {
		return err
	}
	if err := os.Remove(abs); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	return nil
}

// SweepTemp 清理临时目录中的残留文件（上次进程上传中断/崩溃的恢复点）。
func (s *LocalStore) SweepTemp() error {
	entries, err := os.ReadDir(s.tmp)
	if err != nil {
		return err
	}
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		_ = os.Remove(filepath.Join(s.tmp, e.Name()))
	}
	return nil
}

// detectKind 按文件头识别 PDF / PNG，不依赖扩展名。
func detectKind(head []byte) (Kind, error) {
	if len(head) >= len(pngSignature) && bytesEqual(head[:len(pngSignature)], pngSignature) {
		return KindPNG, nil
	}
	if len(head) >= len(pdfSignature) && bytesEqual(head[:len(pdfSignature)], pdfSignature) {
		return KindPDF, nil
	}
	return "", ErrUnsupportedKind
}

func bytesEqual(a, b []byte) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// normalizeHex 去掉可能的 0x 前缀并转小写。
func normalizeHex(v string) string {
	v = strings.TrimSpace(v)
	v = strings.TrimPrefix(v, "0x")
	v = strings.TrimPrefix(v, "0X")
	return strings.ToLower(v)
}
