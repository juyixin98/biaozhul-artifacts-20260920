// Package cache 提供表达式 AST 的磁盘缓存。
//
// 缓存目录与服务的工作目录严格分离：缓存只存放可由原始表达式
// 确定性重建的解析结果，删除缓存目录不会丢失任何源数据。
// 写入采用"临时文件 + rename"的原子方式。
package cache

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"licensejudge/internal/expression"
)

// ErrNotFound 表示缓存未命中。
var ErrNotFound = errors.New("cache miss")

// astNode 是 AST 节点的序列化形式。
type astNode struct {
	Kind      string   `json:"kind"`                // license | with | and | or
	License   string   `json:"license,omitempty"`   // license/with
	Exception string   `json:"exception,omitempty"` // with
	Left      *astNode `json:"left,omitempty"`
	Right     *astNode `json:"right,omitempty"`
}

// entry 是缓存文件的载荷。
type entry struct {
	Version    int     `json:"version"`
	Expression string  `json:"expression"`
	AST        astNode `json:"ast"`
}

// cacheVersion 在序列化格式变化时递增，使旧缓存自动失效。
const cacheVersion = 1

// Cache 是基于文件系统的 AST 缓存。
type Cache struct {
	dir string
}

// New 创建/打开缓存目录。dir 不得位于工作目录之内（由调用方保证）。
func New(dir string) (*Cache, error) {
	if dir == "" {
		return nil, fmt.Errorf("缓存目录不能为空")
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("创建缓存目录失败: %w", err)
	}
	return &Cache{dir: dir}, nil
}

// Dir 返回缓存目录绝对路径。
func (c *Cache) Dir() string {
	abs, err := filepath.Abs(c.dir)
	if err != nil {
		return c.dir
	}
	return abs
}

// keyFor 返回某表达式的缓存文件路径。
func (c *Cache) keyFor(expr string) string {
	sum := sha256.Sum256([]byte(expr))
	name := hex.EncodeToString(sum[:]) + ".json"
	return filepath.Join(c.dir, name[:2], name)
}

// Get 返回缓存的 AST；未命中返回 ErrNotFound。
// 若缓存损坏或表达式不一致，同样视为未命中（由调用方重建）。
func (c *Cache) Get(expr string) (expression.Node, error) {
	data, err := os.ReadFile(c.keyFor(expr))
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, ErrNotFound
		}
		return nil, fmt.Errorf("读取缓存失败: %w", err)
	}
	var e entry
	if err := json.Unmarshal(data, &e); err != nil {
		return nil, ErrNotFound
	}
	if e.Version != cacheVersion || e.Expression != expr {
		return nil, ErrNotFound
	}
	return decodeNode(&e.AST)
}

// Put 将 AST 写入缓存。失败以错误返回，但缓存只影响性能，
// 调用方可以选择只记录日志而不中断请求。
func (c *Cache) Put(expr string, n expression.Node) error {
	e := entry{
		Version:    cacheVersion,
		Expression: expr,
		AST:        *encodeNode(n),
	}
	data, err := json.MarshalIndent(&e, "", "  ")
	if err != nil {
		return fmt.Errorf("序列化 AST 失败: %w", err)
	}
	target := c.keyFor(expr)
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return fmt.Errorf("创建缓存子目录失败: %w", err)
	}
	tmp, err := os.CreateTemp(filepath.Dir(target), ".tmp-*")
	if err != nil {
		return fmt.Errorf("创建缓存临时文件失败: %w", err)
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return fmt.Errorf("写入缓存失败: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("关闭缓存文件失败: %w", err)
	}
	if err := os.Rename(tmpName, target); err != nil {
		return fmt.Errorf("提交缓存文件失败: %w", err)
	}
	return nil
}

func encodeNode(n expression.Node) *astNode {
	switch v := n.(type) {
	case *expression.LicenseNode:
		return &astNode{Kind: "license", License: v.License}
	case *expression.WithNode:
		return &astNode{
			Kind:      "with",
			License:   v.License.License,
			Exception: v.Exception,
		}
	case *expression.AndNode:
		return &astNode{Kind: "and", Left: encodeNode(v.Left), Right: encodeNode(v.Right)}
	case *expression.OrNode:
		return &astNode{Kind: "or", Left: encodeNode(v.Left), Right: encodeNode(v.Right)}
	default:
		return &astNode{Kind: "unknown"}
	}
}

func decodeNode(a *astNode) (expression.Node, error) {
	switch a.Kind {
	case "license":
		return &expression.LicenseNode{License: a.License}, nil
	case "with":
		return &expression.WithNode{
			License:   &expression.LicenseNode{License: a.License},
			Exception: a.Exception,
		}, nil
	case "and", "or":
		if a.Left == nil || a.Right == nil {
			return nil, ErrNotFound
		}
		l, err := decodeNode(a.Left)
		if err != nil {
			return nil, err
		}
		r, err := decodeNode(a.Right)
		if err != nil {
			return nil, err
		}
		if a.Kind == "and" {
			return &expression.AndNode{Left: l, Right: r}, nil
		}
		return &expression.OrNode{Left: l, Right: r}, nil
	default:
		return nil, ErrNotFound
	}
}
