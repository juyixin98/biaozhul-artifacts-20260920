// Package store implements an in-metric, cardinality-budgeted time-series
// store.
//
// Each metric has a fixed budget of label-set combinations (series). Once the
// budget is exhausted, samples from previously unseen combinations are folded
// into a single per-metric overflow bucket; combinations admitted earlier are
// never evicted. Global counters, label-count and label-length limits, and a
// metric-name budget keep the total memory footprint bounded.
package store

import (
	"fmt"
	"sort"
	"strings"
	"sync"
	"unicode/utf8"
)

// OverflowKey is the identity of the per-metric overflow series. It can never
// collide with a real series key: real keys start with a decimal length
// prefix (see seriesKey), this one does not.
const OverflowKey = "__overflow__"

// Config defines all cardinality bounds. Every bound is a hard limit.
type Config struct {
	// MaxSeriesPerMetric is the per-metric budget of distinct (non-overflow)
	// label combinations.
	MaxSeriesPerMetric int `json:"max_series_per_metric"`
	// MaxMetricNames bounds how many distinct metric names are admitted.
	MaxMetricNames int `json:"max_metric_names"`
	// MaxMetricNameLen bounds the byte length of a metric name.
	MaxMetricNameLen int `json:"max_metric_name_len"`
	// MaxLabelKeys bounds the number of labels carried by one sample.
	MaxLabelKeys int `json:"max_label_keys"`
	// MaxLabelKeyLen bounds label-key byte length; longer keys are rejected.
	MaxLabelKeyLen int `json:"max_label_key_len"`
	// MaxLabelValueLen bounds label-value byte length; longer values are
	// truncated (on a rune boundary) rather than rejected.
	MaxLabelValueLen int `json:"max_label_value_len"`
}

// DefaultConfig returns the production defaults used by the server.
func DefaultConfig() Config {
	return Config{
		MaxSeriesPerMetric: 1000,
		MaxMetricNames:     1000,
		MaxMetricNameLen:   256,
		MaxLabelKeys:       20,
		MaxLabelKeyLen:     128,
		MaxLabelValueLen:   512,
	}
}

// Validate rejects non-positive budgets.
func (c Config) Validate() error {
	if c.MaxSeriesPerMetric <= 0 {
		return fmt.Errorf("max_series_per_metric must be > 0")
	}
	if c.MaxMetricNames <= 0 {
		return fmt.Errorf("max_metric_names must be > 0")
	}
	if c.MaxMetricNameLen <= 0 {
		return fmt.Errorf("max_metric_name_len must be > 0")
	}
	if c.MaxLabelKeys <= 0 {
		return fmt.Errorf("max_label_keys must be > 0")
	}
	if c.MaxLabelKeyLen <= 0 {
		return fmt.Errorf("max_label_key_len must be > 0")
	}
	if c.MaxLabelValueLen <= 0 {
		return fmt.Errorf("max_label_value_len must be > 0")
	}
	return nil
}

// Sample is one ingested observation.
type Sample struct {
	Metric string            `json:"metric"`
	Labels map[string]string `json:"labels"`
	Value  float64           `json:"value"`
}

// Outcome reports what happened to one ingested sample.
type Outcome struct {
	Accepted  bool   `json:"accepted"`
	Reason    string `json:"reason,omitempty"`
	Metric    string `json:"metric"`
	SeriesKey string `json:"series_key,omitempty"`
	// NewMetric is true when the sample created its metric.
	NewMetric bool `json:"new_metric,omitempty"`
	// NewSeries is true when the sample created a normal (non-overflow) series.
	NewSeries bool `json:"new_series,omitempty"`
	// Overflow is true when the sample was folded into the overflow bucket.
	Overflow bool `json:"overflow,omitempty"`
	// Truncated is true when at least one label value was length-truncated.
	Truncated bool `json:"truncated,omitempty"`
}

// Stats are the global counters and budget occupancy figures.
type Stats struct {
	SamplesReceived int64 `json:"samples_received"`
	SamplesAccepted int64 `json:"samples_accepted"`
	SamplesRejected int64 `json:"samples_rejected"`
	// OverflowSamples counts accepted samples routed to an overflow bucket.
	OverflowSamples int64 `json:"overflow_samples"`
	// NormalSamples counts accepted samples recorded against normal series.
	NormalSamples int64 `json:"normal_samples"`
	// TruncatedLabelValues counts individual truncation events.
	TruncatedLabelValues int64 `json:"truncated_label_values"`

	MetricNames         int `json:"metric_names"`
	MetricBudget        int `json:"metric_budget"`
	MetricsAtBudget     int `json:"metrics_at_budget"`
	SeriesTotal         int `json:"series_total"`
	OverflowSeriesTotal int `json:"overflow_series_total"`
	SeriesCapacity      int `json:"series_capacity"`

	// LogicalBytes estimates retained label data (defensive upper estimate;
	// map/slice/struct overhead included). LogicalBytesBound is the hard
	// upper bound implied by Config regardless of input.
	LogicalBytes      int64 `json:"logical_bytes_estimate"`
	LogicalBytesBound int64 `json:"logical_bytes_bound"`
}

// Rejection reasons. These are part of the HTTP API contract.
const (
	ReasonEmptyMetric     = "empty_metric"
	ReasonMetricTooLong   = "metric_name_too_long"
	ReasonMetricBudget    = "metric_name_budget_exceeded"
	ReasonTooManyLabels   = "too_many_labels"
	ReasonEmptyLabelKey   = "empty_label_key"
	ReasonLabelKeyTooLong = "label_key_too_long"
)

type series struct {
	labels   map[string]string
	count    int64
	sum      float64
	overflow bool
}

type metric struct {
	name   string
	series map[string]*series
	// overflow is the overflow bucket; nil until first overflow sample.
	overflow *series
	// count is the total number of accepted samples for this metric,
	// including overflow samples.
	count int64
}

// Store is safe for concurrent use.
type Store struct {
	cfg Config

	mu      sync.RWMutex
	metrics map[string]*metric

	// Global counters, guarded by mu.
	received  int64
	accepted  int64
	rejected  int64
	overflowN int64
	truncated int64
}

// New creates an empty store with the given config.
func New(cfg Config) (*Store, error) {
	if err := cfg.Validate(); err != nil {
		return nil, err
	}
	return &Store{
		cfg:     cfg,
		metrics: make(map[string]*metric),
	}, nil
}

// Config returns the store's config.
func (s *Store) Config() Config { return s.cfg }

// IngestBatch records a batch of samples and returns one Outcome per sample,
// in order.
func (s *Store) IngestBatch(samples []Sample) []Outcome {
	outcomes := make([]Outcome, len(samples))

	s.mu.Lock()
	defer s.mu.Unlock()

	for i, sample := range samples {
		outcomes[i] = s.ingestLocked(sample)
	}
	return outcomes
}

// Ingest records one sample.
func (s *Store) Ingest(sample Sample) Outcome {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.ingestLocked(sample)
}

func (s *Store) ingestLocked(sample Sample) Outcome {
	s.received++

	name := sample.Metric
	o := Outcome{Metric: name}

	if name == "" {
		s.rejected++
		o.Reason = ReasonEmptyMetric
		return o
	}
	if len(name) > s.cfg.MaxMetricNameLen {
		s.rejected++
		o.Reason = ReasonMetricTooLong
		return o
	}
	if len(sample.Labels) > s.cfg.MaxLabelKeys {
		s.rejected++
		o.Reason = ReasonTooManyLabels
		return o
	}

	// Normalize labels into a fresh map: validate keys, truncate values.
	labels := make(map[string]string, len(sample.Labels))
	for k, v := range sample.Labels {
		if k == "" {
			s.rejected++
			o.Reason = ReasonEmptyLabelKey
			return o
		}
		if len(k) > s.cfg.MaxLabelKeyLen {
			s.rejected++
			o.Reason = ReasonLabelKeyTooLong
			return o
		}
		if len(v) > s.cfg.MaxLabelValueLen {
			v = truncateBytes(v, s.cfg.MaxLabelValueLen)
			o.Truncated = true
			s.truncated++
		}
		labels[k] = v
	}

	m, exists := s.metrics[name]
	if !exists {
		if len(s.metrics) >= s.cfg.MaxMetricNames {
			s.rejected++
			o.Reason = ReasonMetricBudget
			return o
		}
		m = &metric{name: name, series: make(map[string]*series)}
		s.metrics[name] = m
		o.NewMetric = true
	}

	key := seriesKey(labels)
	o.SeriesKey = key

	if existing := m.series[key]; existing != nil {
		// An admitted combination: never displaced by new label sets.
		existing.count++
		existing.sum += sample.Value
		m.count++
		s.accepted++
		o.Accepted = true
		return o
	}

	if m.overflow != nil || len(m.series) >= s.cfg.MaxSeriesPerMetric {
		// Budget exhausted (or already overflowing): fold into overflow.
		if m.overflow == nil {
			m.overflow = &series{overflow: true}
			m.series[OverflowKey] = m.overflow
		}
		m.overflow.count++
		m.overflow.sum += sample.Value
		m.count++
		s.accepted++
		s.overflowN++
		o.Accepted = true
		o.Overflow = true
		return o
	}

	// Admit a brand-new combination while budget remains.
	m.series[key] = &series{labels: labels, count: 1, sum: sample.Value}
	m.count++
	s.accepted++
	o.Accepted = true
	o.NewSeries = true
	return o
}

// seriesKey builds a collision-free identity for a label set. Each component
// is length-prefixed so ("ab","c")/("a","bc") style collisions are impossible.
func seriesKey(labels map[string]string) string {
	if len(labels) == 0 {
		return ""
	}
	keys := make([]string, 0, len(labels))
	for k := range labels {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	var b strings.Builder
	for _, k := range keys {
		v := labels[k]
		fmt.Fprintf(&b, "%d:%s=%d:%s;", len(k), k, len(v), v)
	}
	return b.String()
}

// truncateBytes shortens b to at most n bytes without splitting a UTF-8 rune.
func truncateBytes(b string, n int) string {
	if len(b) <= n {
		return b
	}
	cut := n
	for cut > 0 && !utf8.RuneStart(b[cut]) {
		cut--
	}
	return b[:cut]
}
