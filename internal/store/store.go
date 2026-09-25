// Package store is an in-memory time series store for cumulative histogram
// samples with an append-only JSONL write-ahead log for local persistence.
package store

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"histmerge/internal/histogram"
)

// Errors returned by the store.
var (
	ErrNotFound     = errors.New("series not found")
	ErrCounterReset = errors.New("counter reset: sample is not monotonic")
	ErrOutOfOrder   = errors.New("out-of-order timestamp")
	ErrBadSelector  = errors.New("invalid selector")
)

// Label is one metric label pair.
type Label struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

// Sample is one timestamped cumulative histogram observation of a series.
type Sample struct {
	Timestamp time.Time            `json:"timestamp"`
	Labels    []Label              `json:"labels"`
	Histogram *histogram.Histogram `json:"histogram"`
}

// SeriesKey builds the canonical identity of a label set.
func SeriesKey(ls []Label) string {
	cp := make([]Label, len(ls))
	copy(cp, ls)
	sort.Slice(cp, func(i, j int) bool { return cp[i].Name < cp[j].Name })
	var b strings.Builder
	for _, l := range cp {
		b.WriteString(l.Name)
		b.WriteByte('=')
		b.WriteString(l.Value)
		b.WriteByte(',')
	}
	return b.String()
}

// LabelsOfKey is not invertible; callers keep labels on the series instead.

type series struct {
	Labels  []Label
	Samples []Sample
}

// Store holds all series, guarded by mu.
type Store struct {
	mu      sync.Mutex
	series  map[string]*series
	walPath string
	walMu   sync.Mutex
	walFile *os.File
	walEnc  *json.Encoder
	walBW   *bufio.Writer
}

// New opens a store backed by walPath (created if missing) and replays it.
// An empty walPath gives a purely in-memory store.
func New(walPath string) (*Store, error) {
	s := &Store{series: map[string]*series{}, walPath: walPath}
	if walPath != "" {
		if err := s.replay(); err != nil {
			return nil, err
		}
		f, err := os.OpenFile(walPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
		if err != nil {
			return nil, fmt.Errorf("open wal: %w", err)
		}
		bw := bufio.NewWriter(f)
		s.walFile = f
		s.walBW = bw
		s.walEnc = json.NewEncoder(bw)
	}
	return s, nil
}

// Close flushes and closes the WAL.
func (s *Store) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.walFile == nil {
		return nil
	}
	if err := s.walBW.Flush(); err != nil {
		return err
	}
	return s.walFile.Close()
}

// replay loads valid records from the WAL and appends them directly, without
// monotonic checks (the WAL was accepted at write time). Corrupt lines are
// skipped but reported on replay error channel-less: we collect a summary.
func (s *Store) replay() error {
	f, err := os.Open(s.walPath)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 16<<20)
	var skipped int
	for sc.Scan() {
		line := sc.Bytes()
		if len(strings.TrimSpace(string(line))) == 0 {
			continue
		}
		var rec walRecord
		if err := json.Unmarshal(line, &rec); err != nil {
			skipped++
			continue
		}
		s.apply(rec.Sample)
	}
	if err := sc.Err(); err != nil {
		return fmt.Errorf("replay scan: %w", err)
	}
	if skipped > 0 {
		// Non-fatal; surfaced through the returned error message for honesty.
		return fmt.Errorf("wal replay skipped %d corrupt record(s)", skipped)
	}
	return nil
}

// walRecord is the on-disk envelope (JSONL, one record per line).
type walRecord struct {
	Kind   string `json:"kind"`
	Sample Sample `json:"sample"`
}

func (s *Store) appendWAL(smp Sample) error {
	if s.walEnc == nil {
		return nil
	}
	s.walMu.Lock()
	defer s.walMu.Unlock()
	if err := s.walEnc.Encode(walRecord{Kind: "sample", Sample: smp}); err != nil {
		return err
	}
	return s.walBW.Flush()
}

// apply inserts a sample into the map/slice (must be sorted after replay
// batch; replay appends in file order and ingest enforces ordering).
func (s *Store) apply(smp Sample) {
	key := SeriesKey(smp.Labels)
	sr, ok := s.series[key]
	if !ok {
		sr = &series{Labels: append([]Label(nil), smp.Labels...)}
		s.series[key] = sr
	}
	sr.Samples = append(sr.Samples, smp)
}

// Ingest validates one sample and, for an already-known series, enforces
// monotonicity of cumulative counts versus its latest sample:
//   - timestamps must advance (equal timestamp is rejected as out-of-order);
//   - total_count and every shared bucket count must not decrease;
//   - sum must not decrease beyond a small float tolerance.
func (s *Store) Ingest(smp Sample) error {
	if smp.Histogram == nil {
		return fmt.Errorf("sample has no histogram")
	}
	if smp.Timestamp.IsZero() {
		smp.Timestamp = time.Now().UTC()
	}
	if err := smp.Histogram.Validate(); err != nil {
		return err
	}
	key := SeriesKey(smp.Labels)

	s.mu.Lock()
	defer s.mu.Unlock()
	if sr, ok := s.series[key]; ok && len(sr.Samples) > 0 {
		last := sr.Samples[len(sr.Samples)-1]
		if !smp.Timestamp.After(last.Timestamp) {
			return fmt.Errorf("%w: %s not after last %s", ErrOutOfOrder,
				smp.Timestamp.Format(time.RFC3339Nano), last.Timestamp.Format(time.RFC3339Nano))
		}
		cur := smp.Histogram
		prev := last.Histogram
		if cur.TotalCount < prev.TotalCount {
			return fmt.Errorf("%w: total_count %d < previous %d",
				ErrCounterReset, cur.TotalCount, prev.TotalCount)
		}
		for _, pb := range prev.NormalizedCopy().Buckets {
			if c, ok := cur.CountAt(pb.Upper); ok && c < pb.CumulativeCount {
				return fmt.Errorf("%w: bucket %s count %d < previous %d",
					ErrCounterReset, pb.Upper, c, pb.CumulativeCount)
			}
		}
		const tol = 1e-9
		if math.Abs(prev.Sum) > 1 && cur.Sum+1e-9 < prev.Sum*(1-tol) {
			return fmt.Errorf("%w: sum %.6g < previous %.6g",
				ErrCounterReset, cur.Sum, prev.Sum)
		} else if math.Abs(prev.Sum) <= 1 && cur.Sum+1e-9 < prev.Sum-tol {
			return fmt.Errorf("%w: sum %.6g < previous %.6g",
				ErrCounterReset, cur.Sum, prev.Sum)
		}
	}
	if err := s.appendWAL(smp); err != nil {
		return fmt.Errorf("wal write: %w", err)
	}
	s.apply(smp)
	return nil
}

// Selector is an optional equality filter (empty means match everything).
type Selector []Label

func matches(labels []Label, sel Selector) bool {
	for _, want := range sel {
		ok := false
		for _, l := range labels {
			if l.Name == want.Name && l.Value == want.Value {
				ok = true
				break
			}
		}
		if !ok {
			return false
		}
	}
	return true
}

// CountSamples returns how many samples the series with the given labels has.
func (s *Store) CountSamples(labels []Label) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	if sr, ok := s.series[SeriesKey(labels)]; ok {
		return len(sr.Samples)
	}
	return 0
}

// Latest returns the most recent sample for every series matching sel,
// sorted by series key for deterministic output.
func (s *Store) Latest(sel Selector) []Sample {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []Sample
	keys := make([]string, 0, len(s.series))
	for k := range s.series {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		sr := s.series[k]
		if !matches(sr.Labels, sel) {
			continue
		}
		out = append(out, sr.Samples[len(sr.Samples)-1])
	}
	return out
}

// Range returns samples within [from,to] (inclusive) for every matching
// series.
func (s *Store) Range(sel Selector, from, to time.Time) map[string][]Sample {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := map[string][]Sample{}
	for k, sr := range s.series {
		if !matches(sr.Labels, sel) {
			continue
		}
		for _, smp := range sr.Samples {
			if (smp.Timestamp.Equal(from) || smp.Timestamp.After(from)) &&
				(smp.Timestamp.Equal(to) || smp.Timestamp.Before(to)) {
				out[k] = append(out[k], smp)
			}
		}
	}
	return out
}

// Increase returns the delta histogram (later minus earlier) for one series'
// window of samples. Cumulative bucket deltas are computed bucket-wise for
// bounds present at both ends; it always retains the trailing +Inf bucket
// whose delta is total-count delta. Returns nil for fewer than 2 samples.
func Increase(samples []Sample) *histogram.Histogram {
	if len(samples) < 2 {
		return nil
	}
	sort.Slice(samples, func(i, j int) bool { return samples[i].Timestamp.Before(samples[j].Timestamp) })
	first, last := samples[0].Histogram, samples[len(samples)-1].Histogram
	if last.TotalCount < first.TotalCount {
		// Counter reset inside the window: conservatively treat the increase
		// as unknown at the histogram level.
		return nil
	}
	out := &histogram.Histogram{Sum: last.Sum - first.Sum}
	var buckets []histogram.Bucket
	ln := last.NormalizedCopy()
	for _, lb := range ln.Buckets {
		pc, ok := first.CountAt(lb.Upper)
		if !ok {
			if lb.Upper.IsInf() {
				pc = first.TotalCount // +Inf cumulative == total
			} else {
				// Bound not known at window start; skip the fine bucket.
				continue
			}
		}
		if lb.CumulativeCount < pc {
			return nil
		}
		buckets = append(buckets, histogram.Bucket{Upper: lb.Upper, CumulativeCount: lb.CumulativeCount - pc})
	}
	out.TotalCount = last.TotalCount - first.TotalCount
	out.Buckets = buckets
	return out
}
