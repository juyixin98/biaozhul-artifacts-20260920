// Package store 提供基于 JSON 文件的小型持久化工具。
// 两个组件（锁服务、资源服务）各自持有一个状态文件，
// 这样进程重启后围栏令牌计数与已见令牌都不会回退。
package store

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// Load 读取 path 指向的 JSON 文件并解码到 v；
// 文件不存在时 v 保持零值并返回 nil（全新启动）。
func Load(path string, v any) error {
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("read state file %s: %w", path, err)
	}
	if err := json.Unmarshal(data, v); err != nil {
		return fmt.Errorf("parse state file %s: %w", path, err)
	}
	return nil
}

// Save 将 v 原子写入 path：先写临时文件再 rename，避免崩溃产生半截文件。
func Save(path string, v any) error {
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal state: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("create state dir: %w", err)
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), ".state-*")
	if err != nil {
		return fmt.Errorf("create temp state file: %w", err)
	}
	tmpName := tmp.Name()
	defer os.Remove(tmpName)
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return fmt.Errorf("write temp state file: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temp state file: %w", err)
	}
	if err := os.Rename(tmpName, path); err != nil {
		return fmt.Errorf("replace state file: %w", err)
	}
	return nil
}
