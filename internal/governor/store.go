package governor

import (
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"sync"
	"unicode/utf8"
)

// Sample 是一条待摄入的观测样本。Labels 不能包含保留键 "__bucket__"。
type Sample struct {
	Metric    string            `json:"metric"`
	Labels    map[string]string `json:"labels"`
	Value     float64           `json:"value"`
	Timestamp int64             `json:"timestamp,omitempty"` // Unix 毫秒；0 表示由服务端填充
}

// Result 描述单条样本摄入结果。
type Result struct {
	Accepted   bool   `json:"accepted"`
	Overflowed bool   `json:"overflowed,omitempty"`
	Reason     string `json:"reason,omitempty"`
	// SeriesKey 是规范化后的组合键，便于调用方去重/排查。
	SeriesKey string `json:"series_key,omitempty"`
}

// Counters 是全局计数。任意时刻恒有 Received == Accepted+Overflowed+Rejected。
type Counters struct {
	Received         int64            `json:"received"`
	Accepted         int64            `json:"accepted"`
	Overflowed       int64            `json:"overflowed"`
	Rejected         int64            `json:"rejected"`
	SeriesCreated    int64            `json:"series_created"`
	SeriesEvicted    int64            `json:"series_evicted"` // 恒为 0：本实现不淘汰已有组合
	ValuesTruncated  int64            `json:"values_truncated"`
	RejectedByReason map[string]int64 `json:"rejected_by_reason"`
}

// Series 是一个具体标签组合的聚合值。
type Series struct {
	Labels      map[string]string `json:"labels"`
	ValueSum    float64           `json:"value_sum"`
	SampleCount int64             `json:"sample_count"`
	LastUpdated int64             `json:"last_updated"`
}

// MetricView 是单指标的查询视图。
type MetricView struct {
	Metric        string   `json:"metric"`
	SeriesBudget  int      `json:"series_budget"`
	TrackedSeries int      `json:"tracked_series"` // 不含 overflow 桶
	Series        []Series `json:"series"`
	Overflow      *Series  `json:"overflow,omitempty"`
}

// StatsView 是 /stats 的返回内容。
type StatsView struct {
	Counters       Counters `json:"counters"`
	MetricNames    int      `json:"metric_names"`
	TrackedSeries  int      `json:"tracked_series"`  // 所有指标 tracked 组合总和（不含 overflow）
	OverflowSeries int      `json:"overflow_series"` // 已建立的 overflow 桶数量
}

type metricState struct {
	series   map[string]*Series // key: seriesKey（overflow 桶的 key 为 overflowSeriesKey）
	overflow *Series
}

func overflowSeriesKey() string {
	return SeriesKey(map[string]string{OverflowLabel: OverflowValue})
}

// Store 是内存中的治理存储，所有方法均为并发安全的。
type Store struct {
	cfg Config

	mu      sync.RWMutex
	metrics map[string]*metricState
	ctr     Counters
}

// NewStore 创建存储并初始化配置默认值。
func NewStore(cfg Config) *Store {
	cfg.applyDefaults()
	s := &Store{
		cfg:     cfg,
		metrics: map[string]*metricState{},
	}
	s.ctr.RejectedByReason = map[string]int64{}
	return s
}

// Config 返回当前配置的副本。
func (s *Store) Config() Config {
	return s.cfg
}

// SeriesKey 返回标签组合的规范化、确定性键：按标签键排序后
// 以 US(0x1f) 分隔、键值均做 Go 引号转义。overflow 桶与普通组合共用同一编码。
func SeriesKey(labels map[string]string) string {
	keys := make([]string, 0, len(labels))
	for k := range labels {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	for i, k := range keys {
		if i > 0 {
			b.WriteByte(0x1f)
		}
		b.WriteString(strconv.Quote(k))
		b.WriteByte(0x1f)
		b.WriteString(strconv.Quote(labels[k]))
	}
	return b.String()
}

func isASCIIAlpha(b byte) bool {
	return b == '_' || (b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z')
}

func isASCIIDigit(b byte) bool { return b >= '0' && b <= '9' }

// 指标名/标签键遵循 Prometheus 风格：[a-zA-Z_:][a-zA-Z0-9_:]*
func validIdent(name string) bool {
	if name == "" {
		return false
	}
	if !isASCIIAlpha(name[0]) && name[0] != ':' {
		return false
	}
	for i := 1; i < len(name); i++ {
		b := name[i]
		if !isASCIIAlpha(b) && !isASCIIDigit(b) && b != ':' {
			return false
		}
	}
	return true
}

// truncateUTF8 在不超过 limit 字节的前提下按 rune 边界截断字符串。
func truncateUTF8(v string, limit int) string {
	if len(v) <= limit {
		return v
	}
	// 回退到最近的 rune 起点；再丢弃尾部可能不完整的最后一个 rune。
	cut := limit
	for cut > 0 && !utf8.RuneStart(v[cut]) {
		cut--
	}
	if r, _ := utf8.DecodeLastRuneInString(v[:cut]); r == utf8.RuneError {
		for cut > 0 {
			cut--
			if utf8.RuneStart(v[cut]) {
				if r2, _ := utf8.DecodeLastRuneInString(v[:cut]); r2 != utf8.RuneError {
					break
				}
			}
		}
	}
	return v[:cut]
}

func (s *Store) reject(reason string) Result {
	s.ctr.Rejected++
	s.ctr.RejectedByReason[reason]++
	return Result{Accepted: false, Reason: reason}
}

// Ingest 摄入一条样本。调用方传入的 labels map 会被复制，外部后续修改不影响存储。
// 时间戳为 0 时保持 0（HTTP 层负责填充），便于核心包在测试中保持确定行为。
func (s *Store) Ingest(sample Sample) Result {
	s.mu.Lock()
	defer s.mu.Unlock()

	s.ctr.Received++

	if len(sample.Metric) > s.cfg.MaxMetricNameBytes {
		return s.reject(ReasonMetricNameTooLong)
	}
	if !validIdent(sample.Metric) {
		return s.reject(ReasonInvalidMetricName)
	}

	// 全局指标名预算：已有指标恒可写，仅当“新名字”出现时才受限。
	st, known := s.metrics[sample.Metric]
	if !known {
		if len(s.metrics) >= s.cfg.MaxMetricNames {
			return s.reject(ReasonMetricBudgetExceeded)
		}
		st = &metricState{series: map[string]*Series{}}
		s.metrics[sample.Metric] = st
	}

	if len(sample.Labels) > s.cfg.MaxLabelsPerSeries {
		return s.reject(ReasonTooManyLabels)
	}

	normalized := make(map[string]string, len(sample.Labels))
	for k, v := range sample.Labels {
		if strings.HasPrefix(k, "__") {
			return s.reject(ReasonReservedLabelKey)
		}
		if len(k) > s.cfg.MaxLabelKeyBytes {
			return s.reject(ReasonLabelKeyTooLong)
		}
		if !validIdent(k) {
			return s.reject(ReasonInvalidLabelKey)
		}
		if len(v) > s.cfg.MaxLabelValueBytes {
			if !s.cfg.TruncateLabelValues {
				return s.reject(ReasonLabelValueTooLong)
			}
			v = truncateUTF8(v, s.cfg.MaxLabelValueBytes)
			s.ctr.ValuesTruncated++
		}
		normalized[k] = v
	}

	ts := sample.Timestamp
	key := SeriesKey(normalized)
	budget := s.cfg.SeriesBudget(sample.Metric)

	if ser, ok := st.series[key]; ok {
		// 已有组合：永远保留，只累加。
		ser.ValueSum += sample.Value
		ser.SampleCount++
		if ts > ser.LastUpdated {
			ser.LastUpdated = ts
		}
		s.ctr.Accepted++
		return Result{Accepted: true, SeriesKey: key}
	}

	if len(st.series) >= budget {
		// 预算用尽：新组合进入 overflow，不分配 map 项。
		if st.overflow == nil {
			st.overflow = &Series{
				Labels:      map[string]string{OverflowLabel: OverflowValue},
				ValueSum:    0,
				SampleCount: 0,
			}
		}
		o := st.overflow
		o.ValueSum += sample.Value
		o.SampleCount++
		if ts > o.LastUpdated {
			o.LastUpdated = ts
		}
		s.ctr.Overflowed++
		return Result{Accepted: true, Overflowed: true, SeriesKey: overflowSeriesKey()}
	}

	// 预算内的新组合：分配内存并保留，之后永不淘汰。
	st.series[key] = &Series{
		Labels:      normalized,
		ValueSum:    sample.Value,
		SampleCount: 1,
		LastUpdated: ts,
	}
	s.ctr.Accepted++
	s.ctr.SeriesCreated++
	return Result{Accepted: true, SeriesKey: key}
}

// Snapshot 返回计数器快照（值拷贝，读锁内完成）。
func (s *Store) Snapshot() Counters {
	s.mu.RLock()
	defer s.mu.RUnlock()
	c := s.ctr
	c.RejectedByReason = make(map[string]int64, len(s.ctr.RejectedByReason))
	for k, v := range s.ctr.RejectedByReason {
		c.RejectedByReason[k] = v
	}
	return c
}

// Stats 返回全局统计视图。
func (s *Store) Stats() StatsView {
	s.mu.RLock()
	defer s.mu.RUnlock()
	c := s.ctr
	c.RejectedByReason = make(map[string]int64, len(s.ctr.RejectedByReason))
	for k, v := range s.ctr.RejectedByReason {
		c.RejectedByReason[k] = v
	}
	v := StatsView{Counters: c, MetricNames: len(s.metrics)}
	for _, st := range s.metrics {
		v.TrackedSeries += len(st.series)
		if st.overflow != nil {
			v.OverflowSeries++
		}
	}
	return v
}

// QueryMetric 返回单指标视图；指标不存在时返回 ok=false。
func (s *Store) QueryMetric(metric string) (MetricView, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	st, ok := s.metrics[metric]
	if !ok {
		return MetricView{}, false
	}
	v := MetricView{
		Metric:        metric,
		SeriesBudget:  s.cfg.SeriesBudget(metric),
		TrackedSeries: len(st.series),
		Series:        make([]Series, 0, len(st.series)),
	}
	for _, ser := range st.series {
		v.Series = append(v.Series, copySeries(ser))
	}
	if st.overflow != nil {
		o := copySeries(st.overflow)
		v.Overflow = &o
	}
	sort.Slice(v.Series, func(i, j int) bool {
		return SeriesKey(v.Series[i].Labels) < SeriesKey(v.Series[j].Labels)
	})
	return v, true
}

func copySeries(ser *Series) Series {
	labels := make(map[string]string, len(ser.Labels))
	for k, v := range ser.Labels {
		labels[k] = v
	}
	return Series{
		Labels:      labels,
		ValueSum:    ser.ValueSum,
		SampleCount: ser.SampleCount,
		LastUpdated: ser.LastUpdated,
	}
}

// MetricNames 返回当前所有指标名（排序后）。
func (s *Store) MetricNames() []string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	names := make([]string, 0, len(s.metrics))
	for m := range s.metrics {
		names = append(names, m)
	}
	sort.Strings(names)
	return names
}

// CheckConservation 校验守恒律 Received == Accepted+Overflowed+Rejected。
func (c Counters) CheckConservation() error {
	if c.Received != c.Accepted+c.Overflowed+c.Rejected {
		return fmt.Errorf("count conservation violated: received=%d accepted=%d overflowed=%d rejected=%d",
			c.Received, c.Accepted, c.Overflowed, c.Rejected)
	}
	return nil
}

// String 便于在测试/日志中快速查看计数。
func (c Counters) String() string {
	b, _ := json.Marshal(c)
	return string(b)
}
