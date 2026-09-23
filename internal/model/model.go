// Package model 定义贯穿各层的数据结构与时间对齐工具。
package model

import (
	"fmt"
	"sort"
	"strconv"
	"strings"
	"time"
)

// 各层桶宽（秒）。
const (
	RawWindow    = 0
	MinuteWindow = int64(60)
	HourWindow   = int64(3600)
)

// Sample 是一个原始观测样本。
type Sample struct {
	// ID 由调用方提供（幂等键）。留空时存储层自动分配。
	ID     string            `json:"id,omitempty"`
	Metric string            `json:"metric"`
	Labels map[string]string `json:"labels,omitempty"`
	// Ts 为 Unix 秒（UTC）。
	Ts    int64   `json:"ts"`
	Value float64 `json:"value"`
}

// Validate 校验样本字段。
func (s Sample) Validate() error {
	if s.Metric == "" {
		return fmt.Errorf("metric 不能为空")
	}
	if s.Ts < 0 {
		return fmt.Errorf("ts 不能为负")
	}
	return nil
}

// SeriesKey 是时间序列的规范化标识：metric + 排序后的标签集。
type SeriesKey string

// Key 计算序列键。标签按 key 排序，保证等价标签集映射到同一序列。
func (s Sample) Key() SeriesKey {
	return SeriesKey(SeriesID(s.Metric, s.Labels))
}

// SeriesID 是 Key 的纯函数版本，供查询侧使用。
func SeriesID(metric string, labels map[string]string) string {
	keys := make([]string, 0, len(labels))
	for k := range labels {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	b.WriteString(metric)
	for _, k := range keys {
		b.WriteByte('|')
		b.WriteString(k)
		b.WriteByte('=')
		b.WriteString(labels[k])
	}
	return b.String()
}

// Point 是查询返回的一个时间点。avg 在 count==0（补零桶）时为 null。
type Point struct {
	Ts    int64    `json:"ts"`
	Count int64    `json:"count"`
	Sum   float64  `json:"sum"`
	Min   float64  `json:"min"`
	Max   float64  `json:"max"`
	Avg   *float64 `json:"avg"`
}

// Query 描述一次查询。
type Query struct {
	Metric string
	// Selectors 为标签等值条件，序列标签必须包含全部条件。
	Selectors map[string]string
	Start     int64 // Unix 秒，含
	End       int64 // Unix 秒，含
	// Window 为目标桶宽：0 表示原始样本，60/3600 表示分钟/小时层。
	Window int64
	// EmptyFill 为 true 时，无数据桶以 count=0 补齐（空桶可观测）。
	EmptyFill bool
}

// FloorToWindow 把时间戳向下取整到所属桶起点。
func FloorToWindow(ts, window int64) int64 {
	if window <= 0 {
		return ts
	}
	return ts - mod(ts, window)
}

func mod(a, n int64) int64 {
	r := a % n
	if r < 0 {
		r += n
	}
	return r
}

// ParseTs 接受 Unix 秒数字或 RFC3339 字符串。
func ParseTs(s string) (int64, error) {
	if s == "" {
		return 0, fmt.Errorf("时间参数为空")
	}
	// 必须整串都是数字才算 Unix 秒；否则 "2025-01-01T01:00:00Z"
	// 会被前缀扫描误解析成 2025。
	if n, err := strconv.ParseInt(s, 10, 64); err == nil {
		return n, nil
	}
	t, err := time.Parse(time.RFC3339, s)
	if err != nil {
		return 0, fmt.Errorf("无法解析时间 %q（支持 Unix 秒或 RFC3339）", s)
	}
	return t.Unix(), nil
}

// FormatTs 把 Unix 秒格式化为 UTC RFC3339，便于人读。
func FormatTs(ts int64) string {
	return time.Unix(ts, 0).UTC().Format(time.RFC3339)
}
