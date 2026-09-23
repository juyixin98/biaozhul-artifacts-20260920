// Package cache 实现内容寻址缓存。缓存目录与工作目录完全分离，
// 每个条目按缓存键 SHA-256 存放：输出文件树 + 元数据。
// 只有构建成功的节点才会发布缓存；恢复操作走临时目录后原子改名。
package cache

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"cdbg/internal/digest"
)

// EntryMeta 是缓存条目的元数据。
type EntryMeta struct {
	Key       string            `json:"key"`
	Node      string            `json:"node"`
	KeyFactor *KeyFactorSummary `json:"key_factor,omitempty"`
	Outputs   []string          `json:"outputs"`
	CreatedAt time.Time         `json:"created_at"`
}

// KeyFactorSummary 记录命中键的组成部分，供“为何命中/失效”解释使用。
type KeyFactorSummary struct {
	ToolName    string             `json:"tool_name"`
	ToolVersion string             `json:"tool_version"`
	Args        []string           `json:"args"`
	Params      map[string]string  `json:"params"`
	Env         map[string]string  `json:"env"`
	Inputs      []digest.TreeEntry `json:"inputs"`
	Deps        map[string]string  `json:"deps"` // 依赖节点名 -> 其缓存键
}

const (
	metaName = "meta.json"
	blobName = "blob"
	tmpName  = "tmp"
)

// Store 是基于文件系统的内容寻址缓存。
type Store struct {
	root string
}

// New 创建/打开 root 下的缓存仓库。
func New(root string) (*Store, error) {
	if err := os.MkdirAll(root, 0o755); err != nil {
		return nil, fmt.Errorf("create cache dir %q: %w", root, err)
	}
	return &Store{root: root}, nil
}

// Root 返回缓存根目录。
func (s *Store) Root() string { return s.root }

func (s *Store) entryDir(key string) string { return filepath.Join(s.root, key[:2], key) }

// isHexKey 判断名称是否为 64 位十六进制（SHA-256 键）。
func isHexKey(name string) bool {
	if len(name) != 64 {
		return false
	}
	for _, c := range name {
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) {
			return false
		}
	}
	return true
}

// Has 判断缓存条目是否存在且元数据完整。
func (s *Store) Has(key string) (bool, *EntryMeta, error) {
	if key == "" || len(key) < 2 {
		return false, nil, nil
	}
	dir := s.entryDir(key)
	metaBytes, err := os.ReadFile(filepath.Join(dir, metaName))
	if errors.Is(err, fs.ErrNotExist) {
		return false, nil, nil
	}
	if err != nil {
		return false, nil, err
	}
	var meta EntryMeta
	if err := json.Unmarshal(metaBytes, &meta); err != nil {
		return false, nil, fmt.Errorf("corrupt cache meta for %s: %w", key, err)
	}
	if _, err := os.Stat(filepath.Join(dir, blobName)); err != nil {
		return false, nil, nil // 元数据在但 blob 被外部删除，视为未命中
	}
	return true, &meta, nil
}

// Publish 把 workdir 下声明的 outputs 收集进缓存。
// 仅在节点命令成功后调用；任何输出缺失都会失败且不留半成品条目。
func (s *Store) Publish(key string, workdir string, outputs []string, meta *EntryMeta) error {
	dir := s.entryDir(key)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	// 先清理可能残留的临时发布目录，保证幂等。
	tmp := filepath.Join(dir, tmpName)
	if err := os.RemoveAll(tmp); err != nil {
		return err
	}
	if err := os.MkdirAll(tmp, 0o755); err != nil {
		return err
	}
	// 临时发布目录在函数返回时始终清理：成功时其中内容已原子转走，
	// 失败时清理半成品。两种情况下都不应残留。
	defer func() { _ = os.RemoveAll(tmp) }()

	blob := filepath.Join(tmp, blobName)
	for _, out := range outputs {
		src := filepath.Join(workdir, filepath.FromSlash(out))
		info, err := os.Stat(src)
		if err != nil {
			return fmt.Errorf("publish %s: output %q missing after successful command: %w", meta.Node, out, err)
		}
		dst := filepath.Join(blob, filepath.FromSlash(out))
		if info.IsDir() {
			if err := copyTree(src, dst); err != nil {
				return err
			}
		} else {
			if err := copyFile(src, dst, info.Mode()); err != nil {
				return err
			}
		}
	}
	if err := writeJSONAtomic(filepath.Join(tmp, metaName), meta); err != nil {
		return err
	}

	// 同键条目已存在时直接替换（内容寻址，键同即内容同）。
	if _, err := os.Stat(filepath.Join(dir, blobName)); err == nil {
		if err := os.RemoveAll(filepath.Join(dir, blobName)); err != nil {
			return err
		}
	}
	if err := os.Rename(filepath.Join(tmp, blobName), filepath.Join(dir, blobName)); err != nil {
		return err
	}
	if err := writeJSONAtomic(filepath.Join(dir, metaName), meta); err != nil {
		return err
	}
	return nil
}

// Restore 把缓存条目中的输出恢复到 workdir（写入前清理旧的同名输出）。
func (s *Store) Restore(key string, workdir string) (*EntryMeta, error) {
	ok, meta, err := s.Has(key)
	if err != nil {
		return nil, err
	}
	if !ok {
		return nil, fmt.Errorf("cache key %s not found", key)
	}
	blob := filepath.Join(s.entryDir(key), blobName)
	for _, out := range meta.Outputs {
		target := filepath.Join(workdir, filepath.FromSlash(out))
		if err := os.RemoveAll(target); err != nil {
			return nil, err
		}
		src := filepath.Join(blob, filepath.FromSlash(out))
		info, statErr := os.Stat(src)
		if statErr != nil {
			return nil, fmt.Errorf("restore %q: %w", out, statErr)
		}
		if info.IsDir() {
			if err := copyTree(src, target); err != nil {
				return nil, err
			}
		} else {
			if err := copyFile(src, target, info.Mode()); err != nil {
				return nil, err
			}
		}
	}
	return meta, nil
}

// Entry 暴露原始条目路径（供 explain 查询）。
func (s *Store) Entry(key string) (string, *EntryMeta, error) {
	ok, meta, err := s.Has(key)
	if err != nil || !ok {
		return "", meta, err
	}
	return s.entryDir(key), meta, nil
}

// List 列出缓存中全部条目（按创建时间排序）。
func (s *Store) List() ([]EntryMeta, error) {
	var out []EntryMeta
	err := filepath.WalkDir(s.root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() || d.Name() != metaName {
			return nil
		}
		// 合法元数据恒位于 <root>/<xx>/<64位十六进制键>/meta.json。
		// 临时发布目录中的残留（父目录名为 tmp）据此排除。
		if !isHexKey(filepath.Base(filepath.Dir(p))) {
			return nil
		}
		raw, err := os.ReadFile(p)
		if err != nil {
			return err
		}
		var m EntryMeta
		if err := json.Unmarshal(raw, &m); err == nil {
			out = append(out, m)
		}
		return nil
	})
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt.Before(out[j].CreatedAt) })
	return out, err
}

func copyTree(src, dst string) error {
	return filepath.Walk(src, func(p string, info fs.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(src, p)
		if err != nil {
			return err
		}
		target := filepath.Join(dst, rel)
		if info.IsDir() {
			return os.MkdirAll(target, info.Mode().Perm())
		}
		if info.Mode().IsRegular() {
			return copyFile(p, target, info.Mode())
		}
		return fmt.Errorf("unsupported file type in cache blob: %s", rel)
	})
}

func copyFile(src, dst string, mode fs.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	tmp := dst + ".tmp"
	out, err := os.OpenFile(tmp, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, mode.Perm())
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	if err := out.Close(); err != nil {
		return err
	}
	return os.Rename(tmp, dst)
}

func writeJSONAtomic(path string, v any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	raw, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	tmp := strings.TrimSuffix(path, filepath.Ext(path)) + ".json.tmp"
	if err := os.WriteFile(tmp, raw, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
