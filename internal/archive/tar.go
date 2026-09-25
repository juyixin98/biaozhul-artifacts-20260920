// Package archive 负责把源目录树确定性地打包为 tar 制品。
//
// 确定性策略：
//   - 条目按归一化的相对路径（UTF-8 字节序，以 / 分隔）严格排序，与
//     操作系统 readdir 的遍历顺序无关；
//   - 所有条目使用固定修改时间（默认 Unix epoch 1970-01-01T00:00:00Z）；
//   - 权限按策略归一化：普通文件 0644、可执行文件（可选保留）0755、目录 0755；
//   - uid/gid/uname/gname 全部清零，设备号与扩展属性不写入；
//   - 采用 PAX 格式，长文件名与 Unicode 路径由 archive/tar 确定性地生成 PAX 扩展头；
//   - 符号链接仅保留链接本身，链接目标必须词法上位于归档根内，且磁盘上解析后
//     也不得逃逸到归档根之外，否则构建被拒绝。
package archive

import (
	"archive/tar"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// 固定权限策略。
const (
	ModeFile     fs.FileMode = 0o644 // 普通文件
	ModeFileExec fs.FileMode = 0o755 // 可执行文件（仅在 PreserveExec 时）
	ModeDir      fs.FileMode = 0o755 // 目录
	ModeSymlink  fs.FileMode = 0o777 // 符号链接（tar 中仅作占位，Linux 上不使用）
)

// 默认固定修改时间：Unix epoch。
var DefaultModTime = time.Unix(0, 0).UTC()

// Options 控制一次确定性打包。
type Options struct {
	// ModTime 写入每个 tar 头的固定修改时间；零值使用 DefaultModTime。
	ModTime time.Time
	// PreserveExec 为 true 时，任何可执行位（u/g/x）被置位的普通文件
	// 归档为 0755；否则全部为 0644。
	PreserveExec bool
}

// Entry 是排序后的归档条目摘要（用于比较/清单，不包含文件内容）。
type Entry struct {
	Path     string `json:"path"`
	Type     string `json:"type"`               // "file" | "dir" | "symlink"
	Mode     uint32 `json:"mode"`               // 归一化后的权限位
	Size     int64  `json:"size,omitempty"`     // 普通文件字节数
	Linkname string `json:"linkname,omitempty"` // 符号链接目标（归一化为以 / 分隔）
	SHA256   string `json:"sha256,omitempty"`   // 普通文件内容的 SHA-256
}

// Summary 是一次打包的确定性摘要。
type Summary struct {
	Entries        []Entry `json:"entries"`
	ArtifactSHA256 string  `json:"artifact_sha256"` // 整个 tar 字节流的 SHA-256
	ArtifactSize   int64   `json:"artifact_size"`
	FileCount      int     `json:"file_count"`
	DirCount       int     `json:"dir_count"`
	SymlinkCount   int     `json:"symlink_count"`
}

// UnsafeSymlinkError 表示一个会逃逸出归档根的符号链接被拒绝。
type UnsafeSymlinkError struct {
	LinkPath string // 归档内相对路径
	Target   string // 原始链接目标
	Reason   string
}

func (e *UnsafeSymlinkError) Error() string {
	return fmt.Sprintf("拒绝逃逸符号链接: %s -> %s (%s)", e.LinkPath, e.Target, e.Reason)
}

// collected 是内部收集到的待打包条目。
type collected struct {
	relPath  string // 以 / 分隔的归档内路径，目录不以 / 结尾
	absPath  string // 磁盘绝对路径
	typ      byte   // tar.TypeReg / tar.TypeDir / tar.TypeSymlink
	mode     fs.FileMode
	size     int64
	linkname string // 以 / 分隔
	sha      string // 文件内容哈希
}

// normalizeRel 把磁盘上的相对路径（可能含分隔符差异）归一化为以 / 分隔。
func normalizeSlash(p string) string {
	return filepath.ToSlash(p)
}

// contained 判断清洗后的绝对路径是否等于 root 或位于 root 之下。
func contained(root, candidate string) bool {
	if candidate == root {
		return true
	}
	rel, err := filepath.Rel(root, candidate)
	if err != nil {
		return false
	}
	rel = filepath.ToSlash(rel)
	return rel != ".." && !strings.HasPrefix(rel, "../")
}

// checkSymlink 校验符号链接的安全性：
//  1. 绝对目标一律拒绝；
//  2. 相对目标相对“链接所在目录”做词法解析，结果必须位于 root 之内
//     （注意：子目录里的链接用 ../sibling 是合法的，不能只看目标字符串）；
//  3. 若目标当前在磁盘上存在（含多跳链接解析后的最终位置），解析后的真实
//     路径必须位于 root 之内——防止 “词法在内、经由目录符号链接跳出” 的链。
func checkSymlink(root, linkAbs, target string) error {
	relLink := normalizeSlash(mustRel(root, linkAbs))
	if filepath.IsAbs(target) {
		return &UnsafeSymlinkError{LinkPath: relLink, Target: target, Reason: "绝对链接目标"}
	}
	// 相对目标相对链接所在目录解析后做词法越界判断。
	lexical := filepath.Clean(filepath.Join(filepath.Dir(linkAbs), filepath.FromSlash(target)))
	if !contained(root, lexical) {
		return &UnsafeSymlinkError{LinkPath: relLink, Target: target, Reason: "词法解析逃逸出归档根"}
	}

	// 磁盘存在性检查：EvalSymlinks 解析整条已存在的前缀中的所有链接。
	resolved, err := filepath.EvalSymlinks(linkAbs)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return nil // 悬空链接：词法检查已通过，允许保留
		}
		return fmt.Errorf("解析符号链接 %s 失败: %w", relLink, err)
	}
	// EvalSymlinks 对 “已存在前缀 + 不存在尾部” 的情况，返回的 resolved 是
	// 已解析前缀与剩余尾部的拼接；不存在的尾部只能是普通名字，不可能再跳转。
	if !contained(root, resolved) {
		return &UnsafeSymlinkError{LinkPath: relLink, Target: target, Reason: "解析后的真实路径位于归档根之外"}
	}
	return nil
}

// mustRel 返回 path 相对 root 的路径；出错时退回原样（理论上 root 为绝对路径时不发生）。
func mustRel(root, path string) string {
	rel, err := filepath.Rel(root, path)
	if err != nil {
		return path
	}
	return rel
}

// hashFile 计算普通文件内容的 SHA-256。
func hashFile(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// readDirNames 是 collect 使用的目录列举函数，返回文件系统的原始顺序。
// 声明为包级变量仅为测试提供“以相反遍历顺序再跑一遍”的确定性接缝。
var readDirNames = func(dir string) ([]string, error) {
	d, err := os.Open(dir)
	if err != nil {
		return nil, err
	}
	defer d.Close()
	return d.Readdirnames(-1)
}

// collect 遍历源目录，收集全部待打包条目。
//
// 为了显式证明“与目录遍历顺序无关”，这里刻意不使用 filepath.WalkDir
// （其内部排序），而是通过 readDirNames 获取操作系统原始顺序，
// 排序统一发生在所有条目收集完成之后。
func collect(src string) ([]*collected, error) {
	root := filepath.Clean(src)
	var out []*collected

	var walk func(dir string) error
	walk = func(dir string) error {
		names, err := readDirNames(dir)
		if err != nil {
			return err
		}
		// 注意：此处刻意不排序 names，保留文件系统返回的原始遍历顺序。
		for _, name := range names {
			abs := filepath.Join(dir, name)
			rel := normalizeSlash(mustRel(root, abs))
			fi, err := os.Lstat(abs)
			if err != nil {
				return err
			}
			switch {
			case fi.IsDir():
				out = append(out, &collected{
					relPath: rel, absPath: abs, typ: tar.TypeDir, mode: fi.Mode(),
				})
				if err := walk(abs); err != nil {
					return err
				}
			case fi.Mode().IsRegular():
				sum, err := hashFile(abs)
				if err != nil {
					return err
				}
				out = append(out, &collected{
					relPath: rel, absPath: abs, typ: tar.TypeReg, mode: fi.Mode(),
					size: fi.Size(), sha: sum,
				})
			case fi.Mode()&fs.ModeSymlink != 0:
				target, err := os.Readlink(abs)
				if err != nil {
					return err
				}
				if err := checkSymlink(root, abs, target); err != nil {
					return err
				}
				out = append(out, &collected{
					relPath: rel, absPath: abs, typ: tar.TypeSymlink, mode: fi.Mode(),
					linkname: normalizeSlash(target),
				})
			default:
				return fmt.Errorf("不支持的文件类型（设备/套接字/FIFO 等）: %s", rel)
			}
		}
		return nil
	}

	if err := walk(root); err != nil {
		return nil, err
	}

	// 唯一的顺序决定点：按归档内相对路径的字节序排序。
	sort.Slice(out, func(i, j int) bool { return out[i].relPath < out[j].relPath })
	return out, nil
}

// normalizeMode 应用固定权限策略。
func normalizeMode(mode fs.FileMode, typ byte, preserveExec bool) fs.FileMode {
	switch typ {
	case tar.TypeDir:
		return ModeDir
	case tar.TypeSymlink:
		return ModeSymlink
	default:
		if preserveExec && mode&0o111 != 0 {
			return ModeFileExec
		}
		return ModeFile
	}
}

// WriteTar 把 src 目录确定性地打包为 tar 字节流写入 w，返回内容摘要。
// src 必须是一个绝对或相对的目录路径。
func WriteTar(src string, w io.Writer, opts Options) (*Summary, error) {
	if opts.ModTime.IsZero() {
		opts.ModTime = DefaultModTime
	}
	root := filepath.Clean(src)
	// 解析根路径自身可能包含的符号链接，使后续 EvalSymlinks 的越界判断
	// 基于同一个真实前缀，避免误判。
	if resolved, err := filepath.EvalSymlinks(root); err == nil {
		root = resolved
	}
	info, err := os.Stat(root)
	if err != nil {
		return nil, fmt.Errorf("源目录不可访问: %w", err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("源路径不是目录: %s", src)
	}

	items, err := collect(root)
	if err != nil {
		return nil, err
	}

	// 同一份 tar 字节流同时写入调用方输出与哈希/计数器，保证摘要与产物一致。
	hw := sha256.New()
	cw := &countingWriter{}
	tww := tar.NewWriter(io.MultiWriter(w, hw, cw))

	sum := &Summary{}
	for _, it := range items {
		fixedMode := normalizeMode(it.mode, it.typ, opts.PreserveExec)
		hdr := &tar.Header{
			Name:       it.relPath,
			Mode:       int64(fixedMode.Perm()),
			Uid:        0,
			Gid:        0,
			Uname:      "",
			Gname:      "",
			ModTime:    opts.ModTime,
			AccessTime: time.Time{},
			ChangeTime: time.Time{},
		}
		switch it.typ {
		case tar.TypeDir:
			hdr.Typeflag = tar.TypeDir
			hdr.Name = strings.TrimSuffix(hdr.Name, "/") + "/"
			sum.DirCount++
		case tar.TypeReg:
			hdr.Typeflag = tar.TypeReg
			hdr.Size = it.size
			sum.FileCount++
		case tar.TypeSymlink:
			hdr.Typeflag = tar.TypeSymlink
			hdr.Linkname = it.linkname
			sum.SymlinkCount++
		}
		if err := tww.WriteHeader(hdr); err != nil {
			return nil, fmt.Errorf("写入 tar 头失败 %s: %w", it.relPath, err)
		}

		entry := Entry{
			Path:     it.relPath,
			Mode:     uint32(fixedMode.Perm()),
			Linkname: it.linkname,
			Size:     it.size,
			SHA256:   it.sha,
		}
		switch it.typ {
		case tar.TypeDir:
			entry.Type = "dir"
		case tar.TypeReg:
			entry.Type = "file"
			if err := copyRegular(tww, it.absPath); err != nil {
				return nil, err
			}
		case tar.TypeSymlink:
			entry.Type = "symlink"
		}
		sum.Entries = append(sum.Entries, entry)
	}
	if err := tww.Close(); err != nil {
		return nil, fmt.Errorf("结束 tar 流失败: %w", err)
	}
	sum.ArtifactSHA256 = hex.EncodeToString(hw.Sum(nil))
	sum.ArtifactSize = cw.n
	return sum, nil
}

// countingWriter 统计流经的字节数。
type countingWriter struct{ n int64 }

func (c *countingWriter) Write(p []byte) (int, error) {
	c.n += int64(len(p))
	return len(p), nil
}

// copyRegular 把普通文件内容拷贝进 tar，并在失败时给出上下文。
func copyRegular(tw *tar.Writer, abs string) error {
	f, err := os.Open(abs)
	if err != nil {
		return fmt.Errorf("打开文件失败 %s: %w", abs, err)
	}
	defer f.Close()
	if _, err := io.Copy(tw, f); err != nil {
		return fmt.Errorf("写入文件内容失败 %s: %w", abs, err)
	}
	return nil
}
