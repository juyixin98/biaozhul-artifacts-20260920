package service

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// PlanWriter 把构建规划落盘：锁文件进工作目录，缓存记录进缓存目录。
// 两者刻意分离，且整个过程不联网、不下载任何包。
type PlanWriter interface {
	Write(req PlanRequest, lf Lockfile) (*WrittenFiles, error)
}

// LocalPlanWriter 是基于本地文件系统的实现。
type LocalPlanWriter struct{}

// cacheRecord 是写入缓存目录的元数据：仅记录“已解析到的版本钉选”，
// 供重复构建复用；不包含包内容（本项目永不下载包内容）。
type cacheRecord struct {
	Schema   string          `json:"schema"`
	Packages []LockedPackage `json:"packages"`
}

// Write 执行落盘。目录必须是已存在的本地路径，禁止路径穿越。
func (LocalPlanWriter) Write(req PlanRequest, lf Lockfile) (*WrittenFiles, error) {
	workspace, err := cleanExistingDir(req.WorkspaceDir, "workspaceDir")
	if err != nil {
		return nil, err
	}
	cache, err := cleanExistingDir(req.CacheDir, "cacheDir")
	if err != nil {
		return nil, err
	}
	lockName := req.LockfileName
	if lockName == "" {
		lockName = "depsolver.lock.json"
	}
	if strings.ContainsAny(lockName, `/\`) || lockName == "." || lockName == ".." {
		return nil, fmt.Errorf("invalid lockfileName %q", lockName)
	}
	lockPath := filepath.Join(workspace, lockName)
	if err := writeIndentedJSON(lockPath, lf); err != nil {
		return nil, fmt.Errorf("write lockfile: %w", err)
	}

	rec := cacheRecord{Schema: "depsolver.cache/v1", Packages: lf.Packages}
	// 缓存文件按根依赖名确定文件名，相同输入得到相同缓存路径。
	key := strings.Join(lf.Roots, "_")
	if key == "" {
		key = "plan"
	}
	cachePath := filepath.Join(cache, "resolve-"+sanitize(key)+".json")
	if err := writeIndentedJSON(cachePath, rec); err != nil {
		return nil, fmt.Errorf("write cache record: %w", err)
	}
	return &WrittenFiles{LockfilePath: lockPath, CacheRecord: cachePath}, nil
}

// cleanExistingDir 校验目录参数：必须是已存在的绝对路径。
func cleanExistingDir(path, field string) (string, error) {
	if path == "" {
		return "", fmt.Errorf("%s is required", field)
	}
	if !filepath.IsAbs(path) {
		return "", fmt.Errorf("%s must be an absolute path: %q", field, path)
	}
	info, err := os.Stat(path)
	if err != nil {
		return "", fmt.Errorf("%s: %w", field, err)
	}
	if !info.IsDir() {
		return "", fmt.Errorf("%s is not a directory: %q", field, path)
	}
	return filepath.Clean(path), nil
}

func sanitize(name string) string {
	var b strings.Builder
	for _, r := range name {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '_':
			b.WriteRune(r)
		default:
			b.WriteByte('_')
		}
	}
	if b.Len() == 0 {
		return "plan"
	}
	return b.String()
}

func writeIndentedJSON(path string, v any) error {
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	return os.WriteFile(path, data, 0o644)
}
