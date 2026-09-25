package policy

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// LoadFile 从单个 JSON 文件加载策略并校验。
func LoadFile(path string) (*Policy, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("读取策略文件失败: %w", err)
	}
	var p Policy
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&p); err != nil {
		return nil, fmt.Errorf("解析策略 JSON 失败 (%s): %w", path, err)
	}
	if err := p.Validate(); err != nil {
		return nil, fmt.Errorf("策略校验失败 (%s): %w", path, err)
	}
	return &p, nil
}

// LoadPath 加载一个 JSON 文件；若 path 是目录，则要求目录中恰好有一个
// *.json 策略文件并加载它。
func LoadPath(path string) (*Policy, error) {
	info, err := os.Stat(path)
	if err != nil {
		return nil, fmt.Errorf("策略路径不可访问: %w", err)
	}
	if !info.IsDir() {
		return LoadFile(path)
	}
	entries, err := os.ReadDir(path)
	if err != nil {
		return nil, fmt.Errorf("读取策略目录失败: %w", err)
	}
	var jsonFiles []string
	for _, e := range entries {
		if !e.IsDir() && filepath.Ext(e.Name()) == ".json" {
			jsonFiles = append(jsonFiles, e.Name())
		}
	}
	switch len(jsonFiles) {
	case 0:
		return nil, fmt.Errorf("策略目录 %s 中没有 .json 文件", path)
	case 1:
		return LoadFile(filepath.Join(path, jsonFiles[0]))
	default:
		return nil, fmt.Errorf("策略目录 %s 中存在多个 .json 文件 (%v)，请直接指定文件", path, jsonFiles)
	}
}
