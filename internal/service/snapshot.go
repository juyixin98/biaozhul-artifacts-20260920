// Package service 实现增量依赖扫描的业务编排与 JSON HTTP 接口。
//
// 快照（基线）以 JSON 文件形式保存在调用方指定的缓存目录中，
// 该目录必须位于项目根目录之外（缓存与工作目录严格分离）。
package service

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"

	"depscanner/internal/builder"
)

// Config 为一次扫描的可比较配置。配置变化视为“全量受影响”。
type Config struct {
	Targets    []string `json:"targets"`
	QuoteDirs  []string `json:"quote_include_dirs"`
	SystemDirs []string `json:"system_include_dirs"`
}

// Snapshot 是持久化在缓存目录中的基线。
type Snapshot struct {
	Version int    `json:"version"`
	Root    string `json:"root"`
	Config  Config `json:"config"`
	// Graph 为基线图的节点与边（用于展示删除发生前的结构）。
	Graph *builder.Result `json:"graph,omitempty"`
	// Files 为全部已解析文件的缓存记录，键为显示路径。
	Files map[string]builder.StoredFile `json:"files"`
}

const snapshotVersion = 1

// memStore 是 builder.Store 的进程内实现，可由磁盘快照预热。
type memStore struct {
	files map[string]builder.StoredFile
}

func newMemStore() *memStore {
	return &memStore{files: map[string]builder.StoredFile{}}
}

func (m *memStore) Get(p string) (builder.StoredFile, bool) {
	f, ok := m.files[p]
	return f, ok
}

func (m *memStore) Put(f builder.StoredFile) {
	m.files[f.Path] = f
}

// snapshotPath 计算基线文件位置：<cacheDir>/depscanner-<root短哈希>.json。
func snapshotPath(cacheDir, rootAbs string) string {
	sum := hashHex(rootAbs)
	return filepath.Join(cacheDir, "depscanner-root-"+sum[:16]+".json")
}

// saveSnapshot 原子写入快照（同目录临时文件 + rename）。
func saveSnapshot(path string, snap *Snapshot) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	data, err := json.MarshalIndent(snap, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// loadSnapshot 读取基线；不存在返回 (nil, nil)。
func loadSnapshot(path string) (*Snapshot, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var snap Snapshot
	if err := json.Unmarshal(data, &snap); err != nil {
		return nil, fmt.Errorf("parse snapshot %s: %w", path, err)
	}
	if snap.Version != snapshotVersion {
		return nil, fmt.Errorf("unsupported snapshot version %d in %s", snap.Version, path)
	}
	return &snap, nil
}

// ensureCacheSeparation 校验缓存目录与项目根互不嵌套。
func ensureCacheSeparation(rootAbs, cacheAbs string) error {
	if cacheAbs == rootAbs {
		return fmt.Errorf("cache dir must differ from project root: %s", cacheAbs)
	}
	if pathContains(cacheAbs, rootAbs) {
		return fmt.Errorf("cache dir %s must not be inside project root %s", cacheAbs, rootAbs)
	}
	if pathContains(rootAbs, cacheAbs) {
		return fmt.Errorf("project root %s must not be inside cache dir %s", rootAbs, cacheAbs)
	}
	return nil
}

func pathContains(dir, sub string) bool {
	rel, err := filepath.Rel(dir, sub)
	if err != nil {
		return false
	}
	return rel != ".." && rel != "." &&
		!filepath.IsAbs(rel) && !startsWithParent(rel)
}

func startsWithParent(p string) bool {
	return len(p) >= 2 && p[0] == '.' && p[1] == '.' &&
		(len(p) == 2 || os.IsPathSeparator(p[2]))
}

// reverseClosure 在图上计算“谁依赖了 changed”的反向可达集合。
// 结果包含 changed 自身，按显示路径排序返回。
func reverseClosure(edges []builder.Edge, changed map[string]struct{}) map[string]struct{} {
	rev := map[string][]string{}
	for _, e := range edges {
		rev[e.To] = append(rev[e.To], e.From)
	}
	reach := map[string]struct{}{}
	var stack []string
	for p := range changed {
		reach[p] = struct{}{}
		stack = append(stack, p)
	}
	for len(stack) > 0 {
		cur := stack[len(stack)-1]
		stack = stack[:len(stack)-1]
		for _, pred := range rev[cur] {
			if _, seen := reach[pred]; !seen {
				reach[pred] = struct{}{}
				stack = append(stack, pred)
			}
		}
	}
	return reach
}

func sortedKeys(m map[string]struct{}) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
