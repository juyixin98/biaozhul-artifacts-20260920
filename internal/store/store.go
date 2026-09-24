// Package store 是准入报告的本地追加式存储。
//
// 关键不变量：评估只产生新报告（新 ID），永不更新/覆盖旧报告；
// 重评估得到独立报告，历史可审计。存储为纯文件 JSON，不依赖真实集群。
package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"

	"mirrorsec/internal/model"
)

var ErrNotFound = errors.New("报告不存在")

type Store struct {
	dir string
	mu  sync.RWMutex
}

// New 打开（必要时创建）报告目录。
func New(dir string) (*Store, error) {
	if strings.TrimSpace(dir) == "" {
		return nil, errors.New("报告目录为空")
	}
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, fmt.Errorf("创建报告目录: %w", err)
	}
	return &Store{dir: dir}, nil
}

// Save 以报告 ID 为文件名写入。若 ID 已存在则拒绝写入（防止任何覆盖）。
func (s *Store) Save(r *model.Report) error {
	if r == nil || r.ID == "" {
		return errors.New("报告或其 ID 为空")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	path := s.path(r.ID)
	if _, err := os.Stat(path); err == nil {
		return fmt.Errorf("报告 %s 已存在：存储不可变，拒绝覆盖", r.ID)
	}
	data, err := json.MarshalIndent(r, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return err
	}
	if err := os.Link(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return fmt.Errorf("提交报告（硬链接）: %w", err)
	}
	_ = os.Remove(tmp)
	return nil
}

func (s *Store) path(id string) string {
	return filepath.Join(s.dir, id+".json")
}

// Get 读取一份报告。
func (s *Store) Get(id string) (*model.Report, error) {
	if strings.ContainsAny(id, "/\\") || !strings.HasPrefix(id, "rep_") {
		return nil, ErrNotFound
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	data, err := os.ReadFile(s.path(id))
	if err != nil {
		if os.IsNotExist(err) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	var r model.Report
	if err := json.Unmarshal(data, &r); err != nil {
		return nil, fmt.Errorf("报告 %s 已损坏: %w", id, err)
	}
	return &r, nil
}

// Summary 是列表项。
type Summary struct {
	ID            string `json:"id"`
	CreatedAt     string `json:"createdAt"`
	PolicyVersion string `json:"policyVersion"`
	PolicyHash    string `json:"policyHash"`
	Repository    string `json:"repository"`
	Tag           string `json:"tag,omitempty"`
	ImageDigest   string `json:"imageDigest"`
	Decision      string `json:"decision"`
}

// List 按创建时间倒序返回报告摘要（不修改任何报告）。
func (s *Store) List(limit int) ([]Summary, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	entries, err := os.ReadDir(s.dir)
	if err != nil {
		return nil, err
	}
	out := make([]Summary, 0, len(entries))
	for _, en := range entries {
		name := en.Name()
		if en.IsDir() || !strings.HasSuffix(name, ".json") {
			continue
		}
		data, err := os.ReadFile(filepath.Join(s.dir, name))
		if err != nil {
			continue
		}
		var r model.Report
		if json.Unmarshal(data, &r) != nil {
			continue
		}
		out = append(out, Summary{
			ID: r.ID, CreatedAt: r.CreatedAt, PolicyVersion: r.PolicyVersion,
			PolicyHash: r.PolicyHash, Repository: r.Repository, Tag: r.Tag,
			ImageDigest: r.ImageDigest, Decision: r.Decision,
		})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CreatedAt > out[j].CreatedAt })
	if limit > 0 && len(out) > limit {
		out = out[:limit]
	}
	return out, nil
}
