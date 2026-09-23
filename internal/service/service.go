// Package service orchestrates ingestion and window computation on top of the
// store and the pure twap computation package.
package service

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"

	"twap-service/internal/domain"
	"twap-service/internal/store"
	"twap-service/internal/twap"
)

// ConflictMode controls behaviour when two different sources report different
// prices for the same timestamp.
type ConflictMode string

const (
	// ConflictPriority resolves deterministically by priority then source
	// name; losers are recorded on the window's conflicts list.
	ConflictPriority ConflictMode = "priority"
	// ConflictReject refuses the new sample with HTTP 409.
	ConflictReject ConflictMode = "reject"
)

// IngestOutcome classifies a single sample write.
type IngestOutcome string

const (
	OutcomeInserted   IngestOutcome = "inserted"
	OutcomeUpdated    IngestOutcome = "updated"
	OutcomeIdempotent IngestOutcome = "idempotent" // same source, same price
)

// IngestResult is returned for each accepted sample.
type IngestResult struct {
	Symbol      string        `json:"symbol"`
	TSUsec      int64         `json:"ts_us"`
	Source      string        `json:"source"`
	Price       int64         `json:"price"`
	Outcome     IngestOutcome `json:"outcome"`
	Recomputed  []int64       `json:"recomputed_window_starts_us"`
	NewVersions []int64       `json:"new_version_window_starts_us"`
	Conflicts   []string      `json:"conflicts,omitempty"`
}

// ConflictError carries details of a rejected source conflict.
type ConflictError struct {
	Info store.ConflictInfo
}

func (e *ConflictError) Error() string {
	return fmt.Sprintf("source conflict at ts=%d: %s=%d vs %s=%d",
		e.Info.TSUsec, e.Info.SourceA, e.Info.PriceA, e.Info.SourceB, e.Info.PriceB)
}

// LateError means a sample arrived outside the late-data horizon.
type LateError struct {
	TSUsec     int64 `json:"ts_us"`
	NowUsec    int64 `json:"now_us"`
	MaxAgeUsec int64 `json:"max_age_usec"`
}

func (e *LateError) Error() string {
	return fmt.Sprintf("sample ts=%d is more than %ds older than now; late window closed",
		e.TSUsec, e.MaxAgeUsec/domain.MicroPerSec)
}

type Service struct {
	st           *store.Store
	cfg          domain.Config
	conflictMode ConflictMode
	now          func() time.Time
}

func New(st *store.Store, cfg domain.Config, mode ConflictMode, now func() time.Time) *Service {
	if now == nil {
		now = time.Now
	}
	return &Service{st: st, cfg: cfg, conflictMode: mode, now: now}
}

func (s *Service) Config() domain.Config      { return s.cfg }
func (s *Service) ConflictMode() ConflictMode { return s.conflictMode }

// RegisterSource creates or replaces a source credential.
func (s *Service) RegisterSource(ctx context.Context, name string, priority int, secretB64 string) error {
	return s.st.CreateSource(ctx, store.SourceRecord{Name: name, Priority: priority, SecretKey: secretB64})
}

// loadAndCompute runs the pure computation for one window inside tx and
// returns the result plus the effective events that produced it.
func (s *Service) loadAndCompute(ctx context.Context, tx pgx.Tx,
	symbol string, startUs, asOfUs int64) (domain.WindowResult, []domain.EffectiveSample, error) {
	endUs := startUs + s.cfg.WindowSec*domain.MicroPerSec
	rows, carry, priorities, err := s.st.LoadWindowInputs(ctx, tx, symbol, startUs, endUs, asOfUs)
	if err != nil {
		return domain.WindowResult{}, nil, err
	}
	raw := make([]domain.Sample, 0, len(rows)+1)
	if carry != nil {
		raw = append(raw, domain.Sample{
			Symbol: symbol, TS: carry.TSUsec, Price: carry.Price, Source: carry.Source,
		})
	}
	for _, r := range rows {
		raw = append(raw, domain.Sample{Symbol: symbol, TS: r.TSUsec, Price: r.Price, Source: r.Source})
	}
	eff := twap.ResolveEffective(raw, priorities)
	startSec := startUs / domain.MicroPerSec
	res := twap.ComputeWindow(symbol, startSec, startSec+s.cfg.WindowSec,
		eff, asOfUs/domain.MicroPerSec, s.cfg.StaleHorizonSec)
	return res, eff, nil
}

// recomputeWindow recomputes one window and, when its canonical content
// changed, appends a new version. Empty windows are never materialized (they
// have no content; they can be served on demand). Returns whether a new
// version was inserted.
func (s *Service) recomputeWindow(ctx context.Context, tx pgx.Tx,
	symbol string, startUs, asOfUs int64) (bool, string, error) {
	res, eff, err := s.loadAndCompute(ctx, tx, symbol, startUs, asOfUs)
	if err != nil {
		return false, "", err
	}
	if res.CoveredUsec == 0 {
		// Nothing observable; do not materialize empty versions.
		return false, "", nil
	}
	hash := twap.ResultHash(res, eff, s.cfg.StaleHorizonSec)
	latest, err := s.st.GetLatestVersion(ctx, tx, symbol, startUs)
	if err != nil {
		return false, "", err
	}
	if latest != nil && latest.ContentHash == hash {
		return false, hash, nil
	}
	ver, err := s.st.NextVersionNo(ctx, tx, symbol, startUs)
	if err != nil {
		return false, "", err
	}
	v := store.VersionRow{
		WindowStartUs: startUs,
		WindowSec:     s.cfg.WindowSec,
		Version:       ver,
		Integral:      res.Integral.String(),
		CoveredUsec:   res.CoveredUsec,
		WindowUsec:    res.WindowUsec,
		TWAPExact:     res.TWAPNum.String() + "/" + res.TWAPDen.String(),
		TWAP6:         res.TWAPString,
		Coverage6:     res.CoverageString,
		Stale:         res.Stale,
		LastSampleUs:  res.LastSampleUnix,
		Sources:       orEmpty(res.Sources),
		Conflicts:     orEmpty(res.Conflicts),
		ContentHash:   hash,
	}
	if err := s.st.InsertVersion(ctx, tx, symbol, v); err != nil {
		return false, "", err
	}
	return true, hash, nil
}

// IngestSample authenticates the caller (already done by middleware), applies
// late-data / conflict rules, writes the sample and incrementally recomputes
// affected windows in one transaction.
func (s *Service) IngestSample(ctx context.Context, smp domain.Sample) (*IngestResult, error) {
	now := s.now()
	nowUs := now.UnixMicro()
	oldest := nowUs - s.cfg.LateToleranceSec*domain.MicroPerSec
	if smp.TS < oldest {
		return nil, &LateError{TSUsec: smp.TS, NowUsec: nowUs,
			MaxAgeUsec: s.cfg.LateToleranceSec * domain.MicroPerSec}
	}
	futureLimit := nowUs + s.cfg.FutureGraceSec*domain.MicroPerSec
	if smp.TS > futureLimit {
		return nil, fmt.Errorf("sample ts=%d is more than %ds in the future",
			smp.TS, s.cfg.FutureGraceSec)
	}

	tx, err := s.st.Pool().Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)

	prevTs, nextTs, same, err := s.st.LoadContextRows(ctx, tx, smp.Symbol, smp.TS)
	if err != nil {
		return nil, err
	}

	var hadSelf bool
	for _, d := range same {
		if d.Source == smp.Source {
			hadSelf = true
			if d.Price == smp.Price {
				// Idempotent replay of identical information: no writes, no
				// recomputation.
				return &IngestResult{
					Symbol: smp.Symbol, TSUsec: smp.TS, Source: smp.Source,
					Price: smp.Price, Outcome: OutcomeIdempotent,
				}, nil
			}
		}
	}
	if ci := store.FindConflict(same, smp.Source, smp.Price); ci != nil {
		ci.TSUsec = smp.TS
		if s.conflictMode == ConflictReject {
			return nil, &ConflictError{Info: *ci}
		}
	}

	hadSelf, changed, err := s.st.UpsertSample(ctx, tx, smp)
	if err != nil {
		return nil, err
	}
	if !changed {
		// Should be unreachable due to the idempotent short-circuit above.
		return &IngestResult{
			Symbol: smp.Symbol, TSUsec: smp.TS, Source: smp.Source,
			Price: smp.Price, Outcome: OutcomeIdempotent,
		}, nil
	}
	outcome := OutcomeInserted
	if hadSelf {
		outcome = OutcomeUpdated
	}

	starts := twap.AffectedWindows(smp.TS, prevTs, nextTs, hadSelf,
		s.cfg.WindowSec, s.cfg.StaleHorizonSec, now.Unix())
	result := &IngestResult{
		Symbol: smp.Symbol, TSUsec: smp.TS, Source: smp.Source,
		Price: smp.Price, Outcome: outcome,
	}
	for _, stUs := range starts {
		newVer, _, rerr := s.recomputeWindow(ctx, tx, smp.Symbol, stUs, nowUs)
		if rerr != nil {
			return nil, rerr
		}
		result.Recomputed = append(result.Recomputed, stUs)
		if newVer {
			result.NewVersions = append(result.NewVersions, stUs)
		}
	}

	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return result, nil
}

// WindowView is the computed or stored presentation of one window.
type WindowView struct {
	Symbol         string     `json:"symbol"`
	WindowSec      int64      `json:"window_sec"`
	StartUnix      int64      `json:"start_unix"`
	EndUnix        int64      `json:"end_unix"`
	TWAP           string     `json:"twap"`       // 6-digit fixed point, "" if uncovered
	TWAPExact      string     `json:"twap_exact"` // "num/den", "0/0" if uncovered
	Integral       string     `json:"integral"`   // price*microseconds
	CoveredUsec    int64      `json:"covered_usec"`
	WindowUsec     int64      `json:"window_usec"`
	Coverage       string     `json:"coverage"` // 0..1, 6 digits
	Stale          bool       `json:"stale"`
	LastSampleUnix *int64     `json:"last_sample_unix_us,omitempty"`
	Sources        []string   `json:"sources"`
	Conflicts      []string   `json:"conflicts"`
	Version        int        `json:"version"`      // 0 when ephemeral/empty
	ContentHash    string     `json:"content_hash"` // "" for empty windows
	Live           bool       `json:"live"`         // computed on demand, not stored
	ComputedAt     *time.Time `json:"computed_at,omitempty"`
}

// GetWindow returns a window: the latest stored version when its canonical
// content still matches current data, otherwise it recomputes (and, for a
// covered window with changed content, appends a new version first).
//
// Empty (completely uncovered) windows are always served live without
// materializing versions.
func (s *Service) GetWindow(ctx context.Context, symbol string, startSec int64, persist bool) (*WindowView, error) {
	nowUs := s.now().UnixMicro()
	startUs := startSec * domain.MicroPerSec

	tx, err := s.st.Pool().Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)

	res, eff, err := s.loadAndCompute(ctx, tx, symbol, startUs, nowUs)
	if err != nil {
		return nil, err
	}
	stored, err := s.st.GetLatestVersion(ctx, tx, symbol, startUs)
	if err != nil {
		return nil, err
	}
	view := buildView(s.cfg, res)
	if res.CoveredUsec == 0 {
		view.Live = true
		return view, nil
	}
	hash := twap.ResultHash(res, eff, s.cfg.StaleHorizonSec)
	if stored != nil && stored.ContentHash == hash {
		view.Version = stored.Version
		view.ContentHash = stored.ContentHash
		view.ComputedAt = &stored.ComputedAt
		// A stored completed window is not live; a stored in-flight window
		// still is, but since the hash matched its content is settled enough.
		return view, nil
	}
	// Content drifted (late data or live evolution).
	if persist {
		ver, rerr := s.st.NextVersionNo(ctx, tx, symbol, startUs)
		if rerr != nil {
			return nil, rerr
		}
		v := store.VersionRow{
			WindowStartUs: startUs, WindowSec: s.cfg.WindowSec, Version: ver,
			Integral: res.Integral.String(), CoveredUsec: res.CoveredUsec,
			WindowUsec: res.WindowUsec,
			TWAPExact:  res.TWAPNum.String() + "/" + res.TWAPDen.String(),
			TWAP6:      res.TWAPString, Coverage6: res.CoverageString, Stale: res.Stale,
			LastSampleUs: res.LastSampleUnix,
			Sources:      orEmpty(res.Sources), Conflicts: orEmpty(res.Conflicts),
			ContentHash: hash,
		}
		if err := s.st.InsertVersion(ctx, tx, symbol, v); err != nil {
			return nil, err
		}
		if err := tx.Commit(ctx); err != nil {
			return nil, err
		}
		view.Version = ver
		view.ContentHash = hash
		view.Live = res.Stale
		return view, nil
	}
	view.ContentHash = hash
	view.Live = true
	return view, nil
}

func buildView(cfg domain.Config, res domain.WindowResult) *WindowView {
	exact := "0/0"
	if res.TWAPDen.Sign() != 0 {
		exact = res.TWAPNum.String() + "/" + res.TWAPDen.String()
	}
	return &WindowView{
		Symbol: res.Symbol, WindowSec: cfg.WindowSec,
		StartUnix: res.StartUnix, EndUnix: res.EndUnix,
		TWAP: res.TWAPString, TWAPExact: exact,
		Integral:    res.Integral.String(),
		CoveredUsec: res.CoveredUsec, WindowUsec: res.WindowUsec,
		Coverage: res.CoverageString, Stale: res.Stale,
		LastSampleUnix: res.LastSampleUnix,
		Sources:        orEmpty(res.Sources), Conflicts: orEmpty(res.Conflicts),
	}
}

func orEmpty(x []string) []string {
	if x == nil {
		return []string{}
	}
	return x
}

// ListWindows returns stored window versions over [fromSec, toSec), aligned to
// the grid. It does not trigger recomputation (use GetWindow for the current
// view of one window).
func (s *Service) ListWindows(ctx context.Context, symbol string, fromSec, toSec int64) ([]WindowView, error) {
	w := s.cfg.WindowSec
	fromSec = domain.AlignStart(fromSec, w)
	toSec = domain.AlignStart(toSec, w)
	if toSec <= fromSec {
		toSec = fromSec + w
	}
	rows, err := s.st.ListWindowLatest(ctx, symbol,
		fromSec*domain.MicroPerSec, toSec*domain.MicroPerSec)
	if err != nil {
		return nil, err
	}
	out := make([]WindowView, 0, len(rows))
	for _, v := range rows {
		t := v.ComputedAt
		out = append(out, WindowView{
			Symbol: symbol, WindowSec: v.WindowSec,
			StartUnix: v.WindowStartUs / domain.MicroPerSec,
			EndUnix:   v.WindowStartUs/domain.MicroPerSec + v.WindowSec,
			TWAP:      v.TWAP6, TWAPExact: v.TWAPExact, Integral: v.Integral,
			CoveredUsec: v.CoveredUsec, WindowUsec: v.WindowUsec,
			Coverage: v.Coverage6, Stale: v.Stale,
			LastSampleUnix: v.LastSampleUs,
			Sources:        v.Sources, Conflicts: v.Conflicts,
			Version: v.Version, ContentHash: v.ContentHash, ComputedAt: &t,
		})
	}
	return out, nil
}

// FullRebuild truncates window_versions and recomputes every window that
// intersects observed sample intervals, up to the current grid position, all
// in one transaction. It returns the number of windows materialized per
// symbol. This is the "full" computation used by the incremental/full
// equivalence test.
func (s *Service) FullRebuild(ctx context.Context) (map[string]int, error) {
	symbols, err := s.st.DistinctSymbols(ctx)
	if err != nil {
		return nil, err
	}
	nowSec := s.now().Unix()
	nowUs := nowSec * domain.MicroPerSec
	w := s.cfg.WindowSec

	tx, err := s.st.Pool().Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	if err := s.st.ClearWindowVersions(ctx, tx); err != nil {
		return nil, err
	}
	counts := map[string]int{}
	for _, sym := range symbols {
		minUs, maxUs, rerr := s.st.SampleTsRange(ctx, sym)
		if rerr != nil {
			return nil, rerr
		}
		if minUs == 0 && maxUs == 0 {
			continue
		}
		first := domain.AlignStart(minUs/domain.MicroPerSec, w) * domain.MicroPerSec
		last := domain.AlignStart(nowSec, w) * domain.MicroPerSec
		for st := first; st <= last; st += w * domain.MicroPerSec {
			newVer, _, rerr := s.recomputeWindow(ctx, tx, sym, st, nowUs)
			if rerr != nil {
				return nil, fmt.Errorf("rebuild %s window %d: %w", sym, st, rerr)
			}
			if newVer {
				counts[sym]++
			}
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, err
	}
	return counts, nil
}

// ErrUnsupported is returned for operations not enabled on this instance.
var ErrUnsupported = errors.New("operation not supported")
