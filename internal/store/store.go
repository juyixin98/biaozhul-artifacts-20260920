// Package store holds the three retention layers (raw seconds, minute
// rollups, hour rollups) and the incremental down-sampling logic.
//
// Ingestion is incremental: a new sample is merged straight into the minute
// and hour bucket with count/sum/min/max math (see package rollup), so late
// samples land correctly no matter when they arrive. A *correction*
// (overwriting a raw point) is propagated bottom-up by recomputing the
// affected minute bucket from retained raw seconds and the affected hour
// bucket from its minute buckets.
package store

import (
	"errors"
	"sort"
	"sync"

	"metricrollup/internal/model"
	"metricrollup/internal/rollup"
)

// Sentinel errors.
var (
	// ErrRawUnavailable means a correction targets a second for which no
	// raw samples are retained — either none were ever ingested there or
	// retention has already pruned them. The coarser layers cannot be
	// revised exactly: merged min/max cannot be un-merged, so the details
	// needed to recompute the bucket are unrecoverable.
	ErrRawUnavailable = errors.New("no raw samples retained at that timestamp (never ingested or pruned); correction cannot be propagated")

	// ErrNotFound means no series matches a lookup.
	ErrNotFound = errors.New("series not found")
)

// Layer selects a retention layer for queries.
type Layer string

// Supported retention layers.
const (
	LayerRaw    Layer = "raw"
	LayerMinute Layer = "minute"
	LayerHour   Layer = "hour"
)

// Width returns the bucket width in seconds for a layer.
func (l Layer) Width() (int64, error) {
	switch l {
	case LayerRaw:
		return rollup.Second, nil
	case LayerMinute:
		return rollup.Minute, nil
	case LayerHour:
		return rollup.Hour, nil
	default:
		return 0, errors.New("unknown layer: " + string(l))
	}
}

// seriesState is everything retained for one time series.
//
// Raw groups sample values by their (second-aligned) timestamp. Minute and
// Hour hold the rollup aggregates. All three maps use bucket-start Unix
// seconds as keys.
type seriesState struct {
	Series model.Series         `json:"series"`
	Raw    map[int64][]float64  `json:"raw"`
	Minute map[int64]rollup.Agg `json:"minute"`
	Hour   map[int64]rollup.Agg `json:"hour"`
}

func newSeriesState(s model.Series) *seriesState {
	return &seriesState{
		Series: s,
		Raw:    map[int64][]float64{},
		Minute: map[int64]rollup.Agg{},
		Hour:   map[int64]rollup.Agg{},
	}
}

// Store is the in-memory, mutex-protected retention store.
type Store struct {
	mu     sync.RWMutex
	series map[string]*seriesState
}

// New returns an empty store.
func New() *Store {
	return &Store{series: map[string]*seriesState{}}
}

// Ingest appends raw samples and incrementally merges each one into its
// minute and hour buckets. Samples may arrive out of order or arbitrarily
// late; the merge is order-independent.
func (s *Store) Ingest(samples []model.Sample) {
	s.mu.Lock()
	defer s.mu.Unlock()

	for _, smp := range samples {
		st := s.getOrCreate(smp)
		sec := rollup.AlignStart(smp.Ts, rollup.Second)
		minStart := rollup.AlignStart(smp.Ts, rollup.Minute)
		hourStart := rollup.AlignStart(smp.Ts, rollup.Hour)

		st.Raw[sec] = append(st.Raw[sec], smp.Value)
		one := rollup.FromSample(smp.Value)
		st.Minute[minStart] = rollup.Merge(st.Minute[minStart], one)
		st.Hour[hourStart] = rollup.Merge(st.Hour[hourStart], one)
	}
}

// Correct overwrites every raw value at the exact second of ts with value
// and propagates the revision upward: the containing minute bucket is
// recomputed from retained raw seconds, then the containing hour bucket is
// recomputed from its minute buckets.
//
// It fails with ErrRawPruned if raw retention has already dropped that
// second.
func (s *Store) Correct(metric string, labels map[string]string, ts int64, value float64) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	key := model.SeriesKey(metric, labels)
	st, ok := s.series[key]
	if !ok {
		return ErrNotFound
	}
	sec := rollup.AlignStart(ts, rollup.Second)
	if _, exists := st.Raw[sec]; !exists {
		return ErrRawUnavailable
	}

	st.Raw[sec] = []float64{value}
	s.rebuildMinuteLocked(st, rollup.AlignStart(ts, rollup.Minute))
	s.rebuildHourLocked(st, rollup.AlignStart(ts, rollup.Hour))
	return nil
}

// rebuildMinuteLocked recomputes one minute bucket from its raw seconds.
func (s *Store) rebuildMinuteLocked(st *seriesState, minStart int64) {
	acc := rollup.Agg{}
	for off := int64(0); off < rollup.Minute; off++ {
		for _, v := range st.Raw[minStart+off] {
			acc = rollup.Merge(acc, rollup.FromSample(v))
		}
	}
	if acc.Count == 0 {
		delete(st.Minute, minStart)
	} else {
		st.Minute[minStart] = acc
	}
}

// rebuildHourLocked recomputes one hour bucket from its 60 minute buckets.
// Note the inputs are minute aggregates (count/sum/min/max), never means.
func (s *Store) rebuildHourLocked(st *seriesState, hourStart int64) {
	acc := rollup.Agg{}
	for off := int64(0); off < 60; off++ {
		acc = rollup.Merge(acc, st.Minute[hourStart+off*rollup.Minute])
	}
	if acc.Count == 0 {
		delete(st.Hour, hourStart)
	} else {
		st.Hour[hourStart] = acc
	}
}

// PruneRaw drops raw seconds strictly older than cutoff (a Unix second).
// Minute and hour layers are untouched. After pruning, Correct on a dropped
// second returns ErrRawPruned.
func (s *Store) PruneRaw(cutoff int64) int {
	s.mu.Lock()
	defer s.mu.Unlock()

	removed := 0
	for _, st := range s.series {
		for sec := range st.Raw {
			if sec < cutoff {
				removed += len(st.Raw[sec])
				delete(st.Raw, sec)
			}
		}
	}
	return removed
}

// Bucket is one query result row. For an empty bucket Count is 0 and
// Sum/Min/Max/Mean are nil, which makes gaps ("空桶") explicit instead of
// silently skipping them.
type Bucket struct {
	Start int64    `json:"start"`
	End   int64    `json:"end"`
	Count int64    `json:"count"`
	Sum   *float64 `json:"sum,omitempty"`
	Min   *float64 `json:"min,omitempty"`
	Max   *float64 `json:"max,omitempty"`
	Mean  *float64 `json:"mean,omitempty"`
}

// Query returns aligned buckets in [start, end) for the given series and
// layer, including zero-count buckets for gaps. start/end are Unix seconds
// and need not be aligned; they are floored/ceiled to layer boundaries.
func (s *Store) Query(metric string, labels map[string]string, start, end int64, layer Layer) ([]Bucket, error) {
	width, err := layer.Width()
	if err != nil {
		return nil, err
	}
	if end <= start {
		return nil, errors.New("query requires end > start")
	}

	s.mu.RLock()
	defer s.mu.RUnlock()

	st, ok := s.series[model.SeriesKey(metric, labels)]
	if !ok {
		return nil, ErrNotFound
	}

	first := rollup.AlignStart(start, width)
	last := rollup.AlignStart(end-1, width)
	buckets := make([]Bucket, 0, (last-first)/width+1)
	for b := first; b <= last; b += width {
		buckets = append(buckets, toBucket(st, b, width, layer))
	}
	return buckets, nil
}

func toBucket(st *seriesState, start, width int64, layer Layer) Bucket {
	var a rollup.Agg
	switch layer {
	case LayerRaw:
		for _, v := range st.Raw[start] {
			a = rollup.Merge(a, rollup.FromSample(v))
		}
	case LayerMinute:
		a = st.Minute[start]
	case LayerHour:
		a = st.Hour[start]
	}
	b := Bucket{Start: start, End: start + width, Count: a.Count}
	if a.Count > 0 {
		sum, min, max, mean := a.Sum, a.Min, a.Max, a.Mean()
		b.Sum, b.Min, b.Max, b.Mean = &sum, &min, &max, &mean
	}
	return b
}

// SeriesInfo describes a known series.
type SeriesInfo struct {
	Key    string            `json:"key"`
	Metric string            `json:"metric"`
	Labels map[string]string `json:"labels"`
}

// ListSeries returns all ingested series, sorted by key for stable output.
func (s *Store) ListSeries() []SeriesInfo {
	s.mu.RLock()
	defer s.mu.RUnlock()

	out := make([]SeriesInfo, 0, len(s.series))
	for key, st := range s.series {
		out = append(out, SeriesInfo{Key: key, Metric: st.Series.Metric, Labels: st.Series.Labels})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Key < out[j].Key })
	return out
}

func (s *Store) getOrCreate(smp model.Sample) *seriesState {
	key := model.SeriesKey(smp.Metric, smp.Labels)
	st, ok := s.series[key]
	if !ok {
		labels := map[string]string{}
		for k, v := range smp.Labels {
			labels[k] = v
		}
		st = newSeriesState(model.Series{Metric: smp.Metric, Labels: labels})
		s.series[key] = st
	}
	return st
}
