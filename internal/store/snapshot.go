package store

import "fmt"

// Export builds a full Snapshot of the store under the read lock.
func (s *Store) Export() Snapshot {
	s.mu.RLock()
	defer s.mu.RUnlock()

	snap := Snapshot{
		Version: SnapshotVersion,
		Config:  s.cfg,
		Globals: SnapshotGlobals{
			SamplesReceived:      s.received,
			SamplesAccepted:      s.accepted,
			SamplesRejected:      s.rejected,
			OverflowSamples:      s.overflowN,
			TruncatedLabelValues: s.truncated,
		},
		Metrics: make([]SnapshotMetric, 0, len(s.metrics)),
	}
	for _, m := range s.metrics {
		sm := SnapshotMetric{
			Name:   m.name,
			Count:  m.count,
			Series: make([]SnapshotSeries, 0, len(m.series)),
		}
		for _, sr := range m.series {
			var labelsCopy map[string]string
			if !sr.overflow {
				labelsCopy = make(map[string]string, len(sr.labels))
				for k, v := range sr.labels {
					labelsCopy[k] = v
				}
			}
			sm.Series = append(sm.Series, SnapshotSeries{
				Labels:   labelsCopy,
				Count:    sr.count,
				Sum:      sr.sum,
				Overflow: sr.overflow,
			})
		}
		snap.Metrics = append(snap.Metrics, sm)
	}
	return snap
}

// Restore builds a Store from a snapshot. The config stored in the snapshot
// is authoritative (the wantConfig parameter only lets the caller verify
// the on-disk config matches the flags it started with; pass nil to accept
// whatever the snapshot contains).
func Restore(snap Snapshot, wantConfig *Config) (*Store, error) {
	cfg := snap.Config
	if err := cfg.Validate(); err != nil {
		return nil, fmt.Errorf("snapshot config invalid: %w", err)
	}
	if wantConfig != nil && *wantConfig != cfg {
		return nil, fmt.Errorf("snapshot config %+v does not match running config %+v", snap.Config, *wantConfig)
	}

	st, err := New(cfg)
	if err != nil {
		return nil, err
	}

	for _, sm := range snap.Metrics {
		if sm.Name == "" {
			return nil, fmt.Errorf("snapshot contains empty metric name")
		}
		m := &metric{name: sm.Name, series: make(map[string]*series, len(sm.Series))}
		m.count = sm.Count
		var seriesCount, overflowCount int64
		for _, ss := range sm.Series {
			sr := &series{labels: ss.Labels, count: ss.Count, sum: ss.Sum, overflow: ss.Overflow}
			if ss.Overflow {
				m.overflow = sr
				m.series[OverflowKey] = sr
				overflowCount += ss.Count
			} else {
				if len(ss.Labels) > cfg.MaxLabelKeys {
					return nil, fmt.Errorf("snapshot series for %q has %d labels, limit %d", sm.Name, len(ss.Labels), cfg.MaxLabelKeys)
				}
				key := seriesKey(ss.Labels)
				if _, dup := m.series[key]; dup {
					return nil, fmt.Errorf("snapshot for %q contains duplicate series", sm.Name)
				}
				m.series[key] = sr
				seriesCount += ss.Count
			}
		}
		if len(m.series) > cfg.MaxSeriesPerMetric+1 {
			return nil, fmt.Errorf("snapshot for %q exceeds series budget", sm.Name)
		}
		if got := seriesCount + overflowCount; got != m.count {
			return nil, fmt.Errorf("snapshot for %q not count-conserving: series total %d, metric count %d", sm.Name, got, m.count)
		}
		st.metrics[sm.Name] = m
	}

	st.received = snap.Globals.SamplesReceived
	st.accepted = snap.Globals.SamplesAccepted
	st.rejected = snap.Globals.SamplesRejected
	st.overflowN = snap.Globals.OverflowSamples
	st.truncated = snap.Globals.TruncatedLabelValues

	// Sanity-check global conservation.
	if st.accepted+st.rejected != st.received {
		return nil, fmt.Errorf("snapshot globals not conserving: accepted %d + rejected %d != received %d", st.accepted, st.rejected, st.received)
	}
	var accepted int64
	for _, m := range st.metrics {
		accepted += m.count
	}
	if accepted != st.accepted {
		return nil, fmt.Errorf("snapshot globals inconsistent: per-metric total %d, accepted %d", accepted, st.accepted)
	}
	return st, nil
}
