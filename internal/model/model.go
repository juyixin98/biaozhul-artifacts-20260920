// Package model defines the core data types exchanged by the ingest and
// query layers.
package model

import (
	"sort"
	"strings"
)

// Sample is one raw observability data point. Ts is a Unix timestamp in
// seconds (UTC); Value is the gauge/counter-style measurement.
type Sample struct {
	Metric string            `json:"metric"`
	Labels map[string]string `json:"labels"`
	Ts     int64             `json:"ts"`
	Value  float64           `json:"value"`
}

// Series describes one uniquely identified time series.
type Series struct {
	Metric string            `json:"metric"`
	Labels map[string]string `json:"labels"`
}

// SeriesKey is the canonical identity of a time series: the metric name plus
// its label set rendered in deterministic order. Samples with equal keys
// aggregate into the same buckets.
func SeriesKey(metric string, labels map[string]string) string {
	if len(labels) == 0 {
		return metric + "|"
	}
	keys := make([]string, 0, len(labels))
	for k := range labels {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	b.WriteString(metric)
	b.WriteByte('|')
	for i, k := range keys {
		if i > 0 {
			b.WriteByte(',')
		}
		b.WriteString(k)
		b.WriteByte('=')
		b.WriteString(labels[k])
	}
	return b.String()
}
