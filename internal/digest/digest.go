// Package digest 提供基于内容的 SHA-256 摘要工具。
// 所有哈希只依赖文件内容与相对路径，不依赖时间戳。
package digest

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
)

const chunkSize = 1 << 20 // 1 MiB

// Bytes 返回数据的 SHA-256 十六进制摘要。
func Bytes(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// Canonical 返回值规范 JSON 编码后的摘要。
// Go 的 encoding/json 对 map 键按字典序输出，struct 按字段顺序输出，
// 因此相同逻辑值始终得到相同摘要。
func Canonical(v any) (string, error) {
	raw, err := json.Marshal(v)
	if err != nil {
		return "", fmt.Errorf("canonical marshal: %w", err)
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

// File 返回单个普通文件内容的摘要。
func File(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.CopyBuffer(h, f, make([]byte, chunkSize)); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// TreeEntry 描述目录树中的一个条目。
type TreeEntry struct {
	Path string `json:"path"` // 相对于根目录的 POSIX 风格路径
	Mode uint32 `json:"mode"`
	Hash string `json:"hash"`
}

// Tree 递归摘要一个目录：内容 = 排序后条目列表（相对路径 + 模式 + 文件内容哈希）。
// 只处理普通文件与目录；遇到符号链接返回错误，避免缓存工作目录之外的内容。
func Tree(root string) (string, []TreeEntry, error) {
	entries := []TreeEntry{}
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		if rel == "." {
			return nil
		}
		rel = filepath.ToSlash(rel)
		info, err := d.Info()
		if err != nil {
			return err
		}
		switch {
		case info.Mode().IsRegular():
			h, err := File(path)
			if err != nil {
				return err
			}
			entries = append(entries, TreeEntry{Path: rel, Mode: uint32(info.Mode().Perm()), Hash: h})
		case d.IsDir():
			// 目录本身仅记录路径与权限。
			entries = append(entries, TreeEntry{Path: rel + "/", Mode: uint32(info.Mode().Perm())})
		default:
			return fmt.Errorf("unsupported file type in input tree: %s (symlinks are not allowed)", rel)
		}
		return nil
	})
	if err != nil {
		return "", nil, err
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].Path < entries[j].Path })
	h, err := Canonical(entries)
	if err != nil {
		return "", nil, err
	}
	return h, entries, nil
}
