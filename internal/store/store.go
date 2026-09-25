// Package store 提供按标签键控的计数器序列内存存储。
// 摄入时先校验（非法样本整批拒绝），再合并、归一化（排序 + 同时间戳 max-wins 去重）。
package store

import (
	"errors"
	"sort"
	"strings"
	"sync"

	"counterreset/internal/series"
)

var (
	errEmptyLabels  = errors.New("labels 不能为空")
	errEmptySamples = errors.New("samples 不能为空")
)

// Metric 是一条计数器序列：标签集合 + 归一化后的样本 + 去重统计。
type Metric struct {
	Labels            map[string]string
	Samples           []series.Sample
	DuplicateSame     int
	DuplicateConflict int
}

// IngestResult 是一次合并的结果统计。
type IngestResult struct {
	// 合并后该序列的唯一（时间戳）样本总数。
	UniqueSamples int `json:"unique_samples"`
	// 本批次请求中携带的样本数。
	Requested int `json:"requested_samples"`
	// 本批次内部的同时间戳重复（与历史数据无关）。
	BatchDuplicateSame     int `json:"batch_duplicate_same"`
	BatchDuplicateConflict int `json:"batch_duplicate_conflicts"`
	// 本批次样本与已存储样本落在同一时间戳的次数。
	OverlapSame     int `json:"overlap_same"`
	OverlapConflict int `json:"overlap_conflicts"`
	// 该序列累计统计：同值重复次数 / max-wins 覆盖次数。
	TotalDuplicateSame     int `json:"total_duplicate_same"`
	TotalDuplicateConflict int `json:"total_duplicate_conflicts"`
}

// Store 是并发安全的内存存储。
type Store struct {
	mu      sync.RWMutex
	metrics map[string]*Metric
}

// New 创建空存储。
func New() *Store {
	return &Store{metrics: map[string]*Metric{}}
}

// Key 把标签序列化为稳定的字符串键（按 key 排序）。
func Key(labels map[string]string) string {
	keys := make([]string, 0, len(labels))
	for k := range labels {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	for _, k := range keys {
		b.WriteString(k)
		b.WriteByte('=')
		b.WriteString(labels[k])
		b.WriteByte(',')
	}
	return b.String()
}

// Ingest 校验并合并一批样本到指定序列。
// 非法样本（负值/非有限值/坏时间戳）导致整批拒绝，返回拒绝明细且不改动状态。
func (s *Store) Ingest(labels map[string]string, samples []series.Sample) (IngestResult, []series.Rejected, error) {
	if len(labels) == 0 {
		return IngestResult{}, nil, errEmptyLabels
	}
	if len(samples) == 0 {
		return IngestResult{}, nil, errEmptySamples
	}
	if rejected := series.ValidateBatch(samples); len(rejected) > 0 {
		return IngestResult{}, rejected, nil
	}

	key := Key(labels)
	s.mu.Lock()
	defer s.mu.Unlock()

	m := s.metrics[key]
	if m == nil {
		cp := make(map[string]string, len(labels))
		for k, v := range labels {
			cp[k] = v
		}
		m = &Metric{Labels: cp}
		s.metrics[key] = m
	}

	// 先归一化本批次（统计批次内部重复），再与已存储样本合并统计重叠。
	batch := series.Normalize(samples)
	res := IngestResult{
		Requested:              len(samples),
		BatchDuplicateSame:     batch.DuplicateSame,
		BatchDuplicateConflict: batch.DuplicateConflict,
	}

	existingByTs := make(map[int64]float64, len(m.Samples))
	for _, sm := range m.Samples {
		existingByTs[sm.TimestampMs] = sm.Value
	}
	for _, sm := range batch.Samples {
		if old, ok := existingByTs[sm.TimestampMs]; ok {
			if old == sm.Value {
				res.OverlapSame++
			} else {
				res.OverlapConflict++
			}
		}
	}

	merged := make([]series.Sample, 0, len(m.Samples)+len(batch.Samples))
	merged = append(merged, m.Samples...)
	merged = append(merged, batch.Samples...)
	nr := series.Normalize(merged)
	m.Samples = nr.Samples
	m.DuplicateSame += batch.DuplicateSame + res.OverlapSame
	m.DuplicateConflict += batch.DuplicateConflict + res.OverlapConflict

	res.UniqueSamples = len(nr.Samples)
	res.TotalDuplicateSame = m.DuplicateSame
	res.TotalDuplicateConflict = m.DuplicateConflict
	return res, nil, nil
}

// Get 返回指定标签的序列快照（拷贝），不存在返回 nil。
func (s *Store) Get(labels map[string]string) *Metric {
	s.mu.RLock()
	defer s.mu.RUnlock()
	m := s.metrics[Key(labels)]
	if m == nil {
		return nil
	}
	return m.clone()
}

// List 返回全部序列快照。
func (s *Store) List() []*Metric {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]*Metric, 0, len(s.metrics))
	for _, m := range s.metrics {
		out = append(out, m.clone())
	}
	sort.Slice(out, func(i, j int) bool { return Key(out[i].Labels) < Key(out[j].Labels) })
	return out
}

func (m *Metric) clone() *Metric {
	cp := &Metric{
		DuplicateSame:     m.DuplicateSame,
		DuplicateConflict: m.DuplicateConflict,
		Labels:            make(map[string]string, len(m.Labels)),
		Samples:           make([]series.Sample, len(m.Samples)),
	}
	for k, v := range m.Labels {
		cp.Labels[k] = v
	}
	copy(cp.Samples, m.Samples)
	return cp
}
