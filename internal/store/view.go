package store

// SeriesView is the query representation of one series (normal or overflow).
type SeriesView struct {
	Key      string            `json:"key"`
	Overflow bool              `json:"overflow"`
	Labels   map[string]string `json:"labels,omitempty"`
	Count    int64             `json:"count"`
	Sum      float64           `json:"sum"`
}

// MetricView describes one metric.
type MetricView struct {
	Name          string       `json:"name"`
	Count         int64        `json:"count"`
	NormalSeries  int          `json:"normal_series"`
	Budget        int          `json:"budget"`
	AtBudget      bool         `json:"at_budget"`
	Overflow      *SeriesView  `json:"overflow,omitempty"`
	Series        []SeriesView `json:"series,omitempty"`
	OverflowCount int64        `json:"overflow_count"`
}

// SnapshotSeries is the persisted form of one series.
type SnapshotSeries struct {
	Labels   map[string]string `json:"labels,omitempty"`
	Count    int64             `json:"count"`
	Sum      float64           `json:"sum"`
	Overflow bool              `json:"overflow,omitempty"`
}

// SnapshotMetric is the persisted form of one metric.
type SnapshotMetric struct {
	Name   string           `json:"name"`
	Count  int64            `json:"count"`
	Series []SnapshotSeries `json:"series"`
}

// Snapshot is the full persistent state. Versioned for forward compatibility.
type Snapshot struct {
	Version int              `json:"version"`
	Config  Config           `json:"config"`
	Globals SnapshotGlobals  `json:"globals"`
	Metrics []SnapshotMetric `json:"metrics"`
}

// SnapshotVersion is the on-disk format version produced by this build.
const SnapshotVersion = 1

// SnapshotGlobals persists the global counters.
type SnapshotGlobals struct {
	SamplesReceived      int64 `json:"samples_received"`
	SamplesAccepted      int64 `json:"samples_accepted"`
	SamplesRejected      int64 `json:"samples_rejected"`
	OverflowSamples      int64 `json:"overflow_samples"`
	TruncatedLabelValues int64 `json:"truncated_label_values"`
}

// Stats returns a point-in-time copy of the global counters and budget
// occupancy.
func (s *Store) Stats() Stats {
	s.mu.RLock()
	defer s.mu.RUnlock()

	st := Stats{
		SamplesReceived:      s.received,
		SamplesAccepted:      s.accepted,
		SamplesRejected:      s.rejected,
		OverflowSamples:      s.overflowN,
		NormalSamples:        s.accepted - s.overflowN,
		TruncatedLabelValues: s.truncated,
		MetricNames:          len(s.metrics),
		MetricBudget:         s.cfg.MaxMetricNames,
		SeriesCapacity:       s.cfg.MaxMetricNames * s.cfg.MaxSeriesPerMetric,
		LogicalBytesBound:    s.logicalBytesBound(),
	}
	for _, m := range s.metrics {
		if m.overflow != nil {
			st.OverflowSeriesTotal++
		}
		st.SeriesTotal += len(m.series)
		if m.normalSeriesLocked() >= s.cfg.MaxSeriesPerMetric {
			st.MetricsAtBudget++
		}
		st.LogicalBytes += m.logicalBytesLocked(s.cfg.MaxMetricNameLen)
	}
	return st
}

// Metric returns the view of one metric. When includeSeries is false the
// per-series list (including the overflow bucket detail) is omitted.
// found is false for an unknown metric.
func (s *Store) Metric(name string, includeSeries bool) (MetricView, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()

	m, ok := s.metrics[name]
	if !ok {
		return MetricView{}, false
	}
	return m.viewLocked(s.cfg.MaxSeriesPerMetric, includeSeries), true
}

// Metrics lists all metric views without series detail.
func (s *Store) Metrics() []MetricView {
	s.mu.RLock()
	defer s.mu.RUnlock()

	out := make([]MetricView, 0, len(s.metrics))
	for _, m := range s.metrics {
		out = append(out, m.viewLocked(s.cfg.MaxSeriesPerMetric, false))
	}
	return out
}

func (m *metric) viewLocked(seriesBudget int, includeSeries bool) MetricView {
	v := MetricView{
		Name:         m.name,
		Count:        m.count,
		NormalSeries: m.normalSeriesLocked(),
		Budget:       seriesBudget,
		// AtBudget is true once the full normal-series budget is occupied;
		// an overflow bucket can only exist in that state.
		AtBudget: m.normalSeriesLocked() >= seriesBudget,
		OverflowCount: func() int64 {
			if m.overflow != nil {
				return m.overflow.count
			}
			return 0
		}(),
	}
	if includeSeries {
		v.Series = make([]SeriesView, 0, len(m.series))
		for key, sr := range m.series {
			var labelsCopy map[string]string
			if !sr.overflow {
				labelsCopy = make(map[string]string, len(sr.labels))
				for k, val := range sr.labels {
					labelsCopy[k] = val
				}
			}
			v.Series = append(v.Series, SeriesView{
				Key:      key,
				Overflow: sr.overflow,
				Labels:   labelsCopy,
				Count:    sr.count,
				Sum:      sr.sum,
			})
		}
		if m.overflow != nil {
			ov := SeriesView{
				Key:      OverflowKey,
				Overflow: true,
				Count:    m.overflow.count,
				Sum:      m.overflow.sum,
			}
			v.Overflow = &ov
		}
	}
	return v
}

func (m *metric) normalSeriesLocked() int {
	if m.overflow == nil {
		return len(m.series)
	}
	return len(m.series) - 1
}

// logicalBytes estimates retained string payload plus conservative per-entry
// overhead. It never decreases as data is admitted, which is what the
// cardinality bounds guarantee.
func (m *metric) logicalBytesLocked(maxNameLen int) int64 {
	// Per-series fixed overhead: series struct + map bucket + *series +
	// labels-map header, rounded up deliberately.
	const perSeriesOverhead = 256
	// Per-label entry: two map buckets + two string headers.
	const perLabelOverhead = 128

	var n int64 = int64(len(m.name))
	for _, sr := range m.series {
		n += perSeriesOverhead
		for k, v := range sr.labels {
			n += int64(len(k) + len(v) + perLabelOverhead)
		}
	}
	return n
}

// logicalBytesBound is the hard upper bound on logical retained bytes implied
// by the config alone: metric names + every admitted series + its labels +
// one overflow bucket per metric.
func (s *Store) logicalBytesBound() int64 {
	const (
		perSeriesOverhead = 256
		perLabelOverhead  = 128
		perMetricOverhead = 128
	)
	names := int64(s.cfg.MaxMetricNames)
	budget := int64(s.cfg.MaxSeriesPerMetric)
	nameLen := int64(s.cfg.MaxMetricNameLen)
	keyLen := int64(s.cfg.MaxLabelKeyLen)
	valLen := int64(s.cfg.MaxLabelValueLen)
	labelsPerSeries := int64(s.cfg.MaxLabelKeys)

	perSeries := perSeriesOverhead + labelsPerSeries*(keyLen+valLen+perLabelOverhead)
	perMetric := perMetricOverhead + nameLen + budget*perSeries + perSeriesOverhead
	return names * perMetric
}
