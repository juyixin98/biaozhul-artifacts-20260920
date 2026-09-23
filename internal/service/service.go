// Package service wires storage, the TWAP kernel and signing into the
// ingestion / late-arrival / versioning protocol.
//
// Windows are fixed, epoch-aligned buckets of length WindowMicros. A
// bucket B = [bStart, bEnd) transitions through:
//
//	open     — now < bEnd; results are provisional (data is still coming);
//	closed   — bEnd <= now < bEnd + MaxLateMicros; materialized versions
//	           may still be superseded when a late sample arrives;
//	frozen   — now >= bEnd + MaxLateMicros; late samples are rejected and
//	           the last materialized version is authoritative forever.
package service

import (
	"context"
	"errors"
	"fmt"
	"math/big"
	"time"

	"github.com/jackc/pgx/v5"

	cryptopkg "twap/internal/crypto"
	"twap/internal/storage"
	"twap/internal/twap"
)

// Clock abstracts wall-clock time (microseconds).
type Clock func() int64

// SystemClock returns the real UTC clock.
func SystemClock() int64 { return time.Now().UnixMicro() }

// Config holds the tunables.
type Config struct {
	WindowMicros     int64
	MaxLateMicros    int64
	StaleAfterMicros int64
	SigningKey       []byte
}

// Service is the application layer.
type Service struct {
	db    *storage.DB
	cfg   Config
	clock Clock
}

// New constructs a service.
func New(db *storage.DB, cfg Config, clock Clock) *Service {
	if clock == nil {
		clock = SystemClock
	}
	return &Service{db: db, cfg: cfg, clock: clock}
}

// Validation errors.
var (
	ErrFutureSample = errors.New("sample timestamp is in the future")
	ErrTooLate      = errors.New("sample arrives after the maximum 5-minute late window")
)

// IngestResult reports what an ingestion did.
type IngestResult struct {
	Sample    twap.Sample        `json:"sample"`
	Changed   bool               `json:"changed"` // new row or price updated
	Accepted  bool               `json:"accepted"`
	Recompute []RecomputedWindow `json:"recomputed_windows"`
}

// RecomputedWindow describes one window touched by the ingestion.
type RecomputedWindow struct {
	WindowStart int64  `json:"window_start"`
	WindowEnd   int64  `json:"window_end"`
	Version     int    `json:"version"`
	Created     bool   `json:"created"` // new version row appended
	InputHash   string `json:"input_hash"`
}

// Ingest validates one sample, stores it, and materializes every closed
// bucket whose authoritative result it could change. Open buckets are
// never versioned on the ingest path (their data is still moving); reads
// of the open bucket are computed live.
func (s *Service) Ingest(ctx context.Context, sample twap.Sample) (*IngestResult, error) {
	now := s.clock()
	if sample.TS > now {
		// A price from the future can never fill history.
		return nil, fmt.Errorf("%w: ts=%d now=%d", ErrFutureSample, sample.TS, now)
	}
	bStart := s.alignDown(sample.TS)
	bEnd := bStart + s.cfg.WindowMicros
	if now >= bEnd+s.cfg.MaxLateMicros {
		return nil, fmt.Errorf("%w: sample ts=%d, bucket closed at %d, late cutoff %d",
			ErrTooLate, sample.TS, bEnd, bEnd+s.cfg.MaxLateMicros)
	}

	changed, err := s.db.UpsertSample(ctx, sample, now)
	if err != nil {
		return nil, fmt.Errorf("upsert sample: %w", err)
	}

	res := &IngestResult{
		Sample:   sample,
		Changed:  changed,
		Accepted: true,
	}

	// A sample can affect exactly two buckets: its own bucket (as an
	// in-window change point) and the immediately following bucket (as
	// that bucket's start anchor). Only closed, non-frozen buckets are
	// materialized; the rest are computed live on read or frozen.
	for _, ws := range []int64{bStart, bStart + s.cfg.WindowMicros} {
		we := ws + s.cfg.WindowMicros
		if now < we {
			continue // still open: computed live on read
		}
		if now >= we+s.cfg.MaxLateMicros {
			continue // frozen: nothing can change it
		}
		rc, err := s.materialize(ctx, ws)
		if err != nil {
			return nil, err
		}
		if rc != nil {
			res.Recompute = append(res.Recompute, *rc)
		}
	}
	return res, nil
}

// alignDown returns the start of the fixed bucket containing ts.
func (s *Service) alignDown(ts int64) int64 {
	w := s.cfg.WindowMicros
	if ts >= 0 {
		return ts - ts%w
	}
	return ts - ((ts - (w - 1)) % w)
}

// materialize recomputes one closed bucket from raw samples and appends a
// new version ONLY when the content hash differs from the latest stored
// version. Repeated ingests that do not change the result are no-ops.
// Returns nil when no new version was needed.
func (s *Service) materialize(ctx context.Context, wStart int64) (*RecomputedWindow, error) {
	wEnd := wStart + s.cfg.WindowMicros
	var rc *RecomputedWindow
	err := s.db.WithTx(ctx, func(tx pgx.Tx) error {
		// Serialize per window so concurrent ingests cannot both append
		// version N+1. Different windows remain fully concurrent.
		if err := s.db.LockWindowTx(ctx, tx, wStart); err != nil {
			return err
		}
		samples, err := s.db.LoadWindowSamplesTx(ctx, tx, wStart, wEnd, wEnd)
		if err != nil {
			return err
		}
		oldHash, oldVersion, err := s.db.LatestVersionHashTx(ctx, tx, wStart)
		if err != nil {
			return err
		}
		vr, err := s.computeVersion(samples, wStart, wEnd, wEnd, oldVersion+1)
		if err != nil {
			return err
		}
		if oldHash == vr.InputHash {
			return nil // identical inputs -> identical result, no new version
		}
		num, den := twapNumDen(vr)
		if err := s.db.InsertVersionTx(ctx, tx, vr, num, den); err != nil {
			return err
		}
		rc = &RecomputedWindow{
			WindowStart: wStart, WindowEnd: wEnd,
			Version: vr.Version, Created: true, InputHash: vr.InputHash,
		}
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("materialize window %d: %w", wStart, err)
	}
	return rc, nil
}

// computeVersion is the single authoritative computation used by both
// the incremental ingest path and the full admin rebuild: same samples,
// same kernel, same hash, same signature. This identity is what the
// incremental/full consistency test asserts.
func (s *Service) computeVersion(samples []twap.Sample, wStart, wEnd, horizon int64, version int) (*storage.VersionRow, error) {
	res, err := twap.Compute(samples, twap.Params{
		WindowStart:      wStart,
		WindowEnd:        wEnd,
		Now:              horizon,
		StaleAfterMicros: s.cfg.StaleAfterMicros,
	})
	if err != nil {
		return nil, err
	}
	hashSamples := make([]cryptopkg.InputSample, len(samples))
	for i, sm := range samples {
		hashSamples[i] = cryptopkg.InputSample{TS: sm.TS, Price: sm.Price, Source: sm.Source}
	}
	inputHash := cryptopkg.InputHash(wStart, wEnd, s.cfg.StaleAfterMicros, hashSamples)

	row := &storage.VersionRow{
		WindowStart:   wStart,
		WindowEnd:     wEnd,
		Version:       version,
		InputHash:     inputHash,
		CoveredMicros: res.CoveredMicros,
		WindowMicros:  wEnd - wStart,
		ConflictCount: res.ConflictCount,
		Stale:         res.Stale,
		LastSampleTS:  res.LastSampleTS,
		HasAnchor:     res.HasAnchor,
		SamplesUsed:   res.SamplesUsed,
		Segments:      res.Segments,
		ComputedAt:    s.clock(),
	}
	if res.CoveredMicros > 0 {
		row.TWAPNum = res.TWAPNum.String()
		row.TWAPDen = res.TWAPDen.String()
	}
	payload := cryptopkg.VersionPayload{
		WindowStart:   row.WindowStart,
		WindowEnd:     row.WindowEnd,
		Version:       row.Version,
		InputHash:     row.InputHash,
		TWAPNum:       row.TWAPNum,
		TWAPDen:       row.TWAPDen,
		CoveredMicros: row.CoveredMicros,
		WindowMicros:  row.WindowMicros,
		ConflictCount: row.ConflictCount,
		Stale:         row.Stale,
		LastSampleTS:  row.LastSampleTS,
	}
	row.Signature = cryptopkg.Sign(s.cfg.SigningKey, payload)
	return row, nil
}

func twapNumDen(v *storage.VersionRow) (*string, *string) {
	if v.TWAPNum == "" {
		return nil, nil
	}
	num, den := v.TWAPNum, v.TWAPDen
	return &num, &den
}

// WindowView is the read representation of one window.
type WindowView struct {
	WindowStart   int64          `json:"window_start"`
	WindowEnd     int64          `json:"window_end"`
	State         string         `json:"state"`   // open | closed | frozen
	Version       int            `json:"version"` // 0 for a live read
	InputHash     string         `json:"input_hash"`
	Signature     string         `json:"signature"`
	SignatureOK   *bool          `json:"signature_ok,omitempty"`
	TWAP          *float64       `json:"twap"`
	TWAPExact     *string        `json:"twap_exact"`
	Coverage      float64        `json:"coverage"`
	CoverageExact string         `json:"coverage_exact"`
	CoveredMicros int64          `json:"covered_micros"`
	WindowMicros  int64          `json:"window_micros"`
	Stale         bool           `json:"stale"`
	ConflictCount int            `json:"conflict_count"`
	SamplesUsed   int            `json:"samples_used"`
	HasAnchor     bool           `json:"has_anchor"`
	LastSampleTS  int64          `json:"last_sample_ts"`
	Segments      []twap.Segment `json:"segments"`
	Live          bool           `json:"live"`
}

// ReadWindow returns the TWAP of the fixed bucket containing atTime.
// Closed/frozen buckets serve the stored materialized version; the open
// bucket is computed live from current data (never from future prices).
func (s *Service) ReadWindow(ctx context.Context, atTime int64, verifySig bool) (*WindowView, error) {
	wStart := s.alignDown(atTime)
	wEnd := wStart + s.cfg.WindowMicros
	now := s.clock()
	state := bucketState(now, wEnd, s.cfg.MaxLateMicros)

	if state != "open" {
		v, err := s.db.LatestVersion(ctx, wStart)
		if errors.Is(err, storage.ErrNotFound) {
			// Closed or even frozen without a stored version (e.g. the
			// service was down across the whole late window):
			// materialize once on demand. A frozen bucket's first
			// materialization is still deterministic because no sample
			// past the cutoff is ever accepted.
			if _, err := s.materialize(ctx, wStart); err != nil {
				return nil, err
			}
			v, err = s.db.LatestVersion(ctx, wStart)
		}
		if err != nil {
			return nil, err
		}
		return s.rowToView(v, state, verifySig), nil
	}

	// Open bucket: live compute with horizon = now (samples at/after now
	// are invisible). Coverage denominator stays the full window length,
	// so an open bucket with sparse data reports partial coverage.
	samples, err := s.db.LoadWindowSamples(ctx, wStart, wEnd, now)
	if err != nil {
		return nil, err
	}
	row, err := s.computeVersion(samples, wStart, wEnd, now, 0)
	if err != nil {
		return nil, err
	}
	row.Version = 0
	view := s.rowToView(row, "open", false)
	view.Live = true
	return view, nil
}

func bucketState(now, wEnd, maxLate int64) string {
	switch {
	case now < wEnd:
		return "open"
	case now < wEnd+maxLate:
		return "closed"
	default:
		return "frozen"
	}
}

func (s *Service) rowToView(v *storage.VersionRow, state string, verifySig bool) *WindowView {
	view := &WindowView{
		WindowStart:   v.WindowStart,
		WindowEnd:     v.WindowEnd,
		State:         state,
		Version:       v.Version,
		InputHash:     v.InputHash,
		Signature:     v.Signature,
		CoveredMicros: v.CoveredMicros,
		WindowMicros:  v.WindowMicros,
		Stale:         v.Stale,
		ConflictCount: v.ConflictCount,
		SamplesUsed:   v.SamplesUsed,
		HasAnchor:     v.HasAnchor,
		LastSampleTS:  v.LastSampleTS,
		Segments:      v.Segments,
	}
	if v.TWAPNum != "" {
		num, _ := new(big.Int).SetString(v.TWAPNum, 10)
		den, _ := new(big.Int).SetString(v.TWAPDen, 10)
		f, _ := new(big.Float).Quo(
			new(big.Float).SetInt(num), new(big.Float).SetInt(den)).Float64()
		exact := v.TWAPNum + "/" + v.TWAPDen
		view.TWAP = &f
		view.TWAPExact = &exact
	}
	covNum := big.NewInt(v.CoveredMicros)
	covDen := big.NewInt(v.WindowMicros)
	cov, _ := new(big.Float).Quo(
		new(big.Float).SetInt(covNum), new(big.Float).SetInt(covDen)).Float64()
	view.Coverage = cov
	view.CoverageExact = fmt.Sprintf("%d/%d", v.CoveredMicros, v.WindowMicros)

	if verifySig {
		payload := cryptopkg.VersionPayload{
			WindowStart:   v.WindowStart,
			WindowEnd:     v.WindowEnd,
			Version:       v.Version,
			InputHash:     v.InputHash,
			TWAPNum:       v.TWAPNum,
			TWAPDen:       v.TWAPDen,
			CoveredMicros: v.CoveredMicros,
			WindowMicros:  v.WindowMicros,
			ConflictCount: v.ConflictCount,
			Stale:         v.Stale,
			LastSampleTS:  v.LastSampleTS,
		}
		ok := cryptopkg.Verify(s.cfg.SigningKey, payload, v.Signature)
		view.SignatureOK = &ok
	}
	return view
}

// ReadVersion serves a specific historical version of a window.
func (s *Service) ReadVersion(ctx context.Context, wStart int64, version int, verifySig bool) (*WindowView, error) {
	v, err := s.db.GetVersion(ctx, wStart, version)
	if err != nil {
		return nil, err
	}
	state := bucketState(s.clock(), v.WindowEnd, s.cfg.MaxLateMicros)
	return s.rowToView(v, state, verifySig), nil
}

// RangeResult is a live TWAP over an arbitrary [start,end) interval.
type RangeResult struct {
	Start int64 `json:"start"`
	End   int64 `json:"end"`
	*WindowView
}

// ReadRange computes TWAP over an arbitrary half-open interval. It is a
// full raw-scan computation (the authoritative reference the
// incremental bucket path is tested against).
func (s *Service) ReadRange(ctx context.Context, start, end int64) (*RangeResult, error) {
	if end <= start {
		return nil, fmt.Errorf("end must be greater than start")
	}
	now := s.clock()
	samples, err := s.db.LoadWindowSamples(ctx, start, end, now)
	if err != nil {
		return nil, err
	}
	row, err := s.computeVersion(samples, start, end, now, 0)
	if err != nil {
		return nil, err
	}
	row.Version = 0
	view := s.rowToView(row, "range", false)
	view.Live = true
	return &RangeResult{Start: start, End: end, WindowView: view}, nil
}

// ListWindows returns window starts that have materialized versions.
func (s *Service) ListWindows(ctx context.Context, limit int) ([]int64, error) {
	return s.db.ListWindowStarts(ctx, limit)
}

// FullRecompute re-runs the authoritative computation for every closed
// bucket from the earliest sample to the current bucket, and appends a
// new version only where the recomputed hash differs from what is
// stored. Because computeVersion is shared with the ingest path, a
// healthy system produces zero or content-equivalent diffs — this is
// the incremental/full consistency guarantee.
func (s *Service) FullRecompute(ctx context.Context) ([]RecomputedWindow, int, error) {
	minTS, err := s.db.MinSampleTS(ctx)
	if errors.Is(err, storage.ErrNotFound) {
		return nil, 0, nil
	}
	if err != nil {
		return nil, 0, err
	}
	now := s.clock()
	curStart := s.alignDown(now)
	var diffs []RecomputedWindow
	checked := 0
	for ws := s.alignDown(minTS); ws < curStart; ws += s.cfg.WindowMicros {
		we := ws + s.cfg.WindowMicros
		if now < we {
			continue
		}
		checked++
		rc, err := s.materialize(ctx, ws)
		if err != nil {
			return nil, checked, err
		}
		if rc != nil {
			diffs = append(diffs, *rc)
		}
	}
	return diffs, checked, nil
}
