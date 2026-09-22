// Package detection implements the windowed anomaly detection engine.
//
// Processing model:
//   - Newly ingested events land as unprocessed rows. A worker claims them in
//     batches, derives the detection windows each event belongs to, and
//     evaluates those windows. All state changes are idempotent (unique keys,
//     upserts), so processing may be retried and runs may overlap across
//     restarts/replicas without producing duplicate alerts.
//   - A periodic sweep re-evaluates any window in the allowed backfill horizon
//     that has never been evaluated under the *current* rule version. This is
//     the backstop that makes the scheduler restart-safe: it does not matter
//     whether an event was processed inline, by a previous process, or by
//     another replica.
package detection

import (
	"fmt"
	"math"
	"sort"
	"time"

	"anomalywatch/internal/config"
	"anomalywatch/internal/models"
	"anomalywatch/internal/timeutil"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

// Clock allows tests to freeze time.
type Clock interface{ Now() time.Time }

type realClock struct{}

func (realClock) Now() time.Time { return time.Now().UTC() }

// WindowRef identifies one detection window for one employee.
type WindowRef struct {
	Scope       string
	LabelDate   time.Time // UTC midnight; night/stat = owning local date, burst = UTC bucket date
	WindowStart time.Time // UTC (zero time for firstusb)
	WindowEnd   time.Time // UTC (zero time for firstusb)
}

// Engine runs detection over stored events.
type Engine struct {
	db  *gorm.DB
	cfg config.Config
	clk Clock
}

func New(db *gorm.DB, cfg config.Config) *Engine {
	return &Engine{db: db, cfg: cfg, clk: realClock{}}
}

// WithClock returns a copy of the engine using clk (tests).
func (e *Engine) WithClock(clk Clock) *Engine {
	cp := *e
	cp.clk = clk
	return &cp
}

func (e *Engine) now() time.Time { return e.clk.Now().UTC() }

// RefsForEvent derives every window an event contributes to. Rule parameters
// (window width, night hours) come from the currently active rule versions.
func (e *Engine) RefsForEvent(ev models.Event, loc *time.Location, rules map[string]RuleParams) []WindowRef {
	t := ev.OccurredAt.UTC()
	var refs []WindowRef

	// download_burst: fixed tumbling windows aligned to the rule width (UTC).
	if ev.EventType == models.EventTypeFileDownload {
		if p, ok := rules[models.RuleDownloadBurst]; ok && p.Enabled {
			start := timeutil.BurstWindowStart(t, p.Window)
			refs = append(refs, WindowRef{
				Scope:       models.ScopeBurst,
				LabelDate:   timeutil.MidnightUTC(start),
				WindowStart: start,
				WindowEnd:   start.Add(p.Window),
			})
		}
	}

	// night_activity: the night window keyed by its owning local date.
	if p, ok := rules[models.RuleNightActivity]; ok && p.Enabled {
		if night, day := timeutil.IsNight(t, loc, p.StartHour, p.EndHour); night {
			s, end := timeutil.NightBounds(day, p.StartHour, p.EndHour)
			refs = append(refs, WindowRef{
				Scope:       models.ScopeNight,
				LabelDate:   timeutil.DateLabelOfDate(day),
				WindowStart: s,
				WindowEnd:   end,
			})
		}
	}

	// first_usb is employee-scoped, not window-scoped; the event still marks
	// the employee for (re)evaluation.
	if ev.EventType == models.EventTypeUSB {
		if p, ok := rules[models.RuleFirstUSB]; ok && p.Enabled {
			refs = append(refs, WindowRef{
				Scope:       models.ScopeFirstUSB,
				LabelDate:   sentinelDate,
				WindowStart: sentinelDate,
				WindowEnd:   sentinelDate,
			})
		}
	}

	// statistical: the event's own local day. If the day is still open the
	// evaluation is a no-op until the sweep revisits it after local midnight.
	if p, ok := rules[models.RuleStatistical]; ok && p.Enabled {
		day := timeutil.LocalDate(t, loc)
		refs = append(refs, WindowRef{
			Scope:       models.ScopeStat,
			LabelDate:   timeutil.DateLabelOfDate(day),
			WindowStart: day.UTC(),
			WindowEnd:   day.AddDate(0, 0, 1).UTC(),
		})
	}
	return refs
}

// sentinelDate stands in for absent window bounds (first_usb). It must be a
// valid DATETIME value; Go's time.Time zero value (year 1) is outside MySQL's
// DATETIME range.
var sentinelDate = time.Date(1970, 1, 1, 0, 0, 0, 0, time.UTC)

// ProcessPending claims unprocessed events and evaluates their windows.
// Returns the number of events processed. It is safe to run concurrently:
// events are claimed with a conditional UPDATE inside a transaction, and
// window evaluation is idempotent.
func (e *Engine) ProcessPending(limit int) (int, error) {
	if limit <= 0 {
		limit = e.cfg.ProcessBatch
	}
	claimed, err := e.claimPending(limit)
	if err != nil {
		return 0, err
	}
	if len(claimed) == 0 {
		return 0, nil
	}
	if err := e.processEvents(claimed); err != nil {
		return 0, err
	}
	return len(claimed), nil
}

// claimPending atomically marks up to limit unprocessed events as processed and
// returns the claimed rows. A row is claimed only if processed is still 0, so
// duplicate schedulers/replicas never claim the same event twice.
func (e *Engine) claimPending(limit int) ([]models.Event, error) {
	var ids []uint64
	err := e.db.Transaction(func(tx *gorm.DB) error {
		if err := tx.Raw(`
			SELECT id FROM events WHERE processed = 0
			ORDER BY received_at, id LIMIT ?`, limit).Scan(&ids).Error; err != nil {
			return err
		}
		if len(ids) == 0 {
			return nil
		}
		return tx.Exec(`UPDATE events SET processed = 1 WHERE id IN ? AND processed = 0`, ids).Error
	})
	if err != nil || len(ids) == 0 {
		return nil, err
	}
	var out []models.Event
	if err := e.db.Where("id IN ?", ids).Find(&out).Error; err != nil {
		return nil, err
	}
	return out, nil
}

// processEvents groups claimed events by employee and evaluates the union of
// their windows. A failure does not un-claim events; because evaluation is
// idempotent, the sweep backstop still covers their windows.
func (e *Engine) processEvents(events []models.Event) error {
	rules, err := LoadRules(e.cfg, e.db)
	if err != nil {
		return err
	}
	byEmp := map[uint64][]models.Event{}
	for _, ev := range events {
		byEmp[ev.EmployeeID] = append(byEmp[ev.EmployeeID], ev)
	}
	empIDs := make([]uint64, 0, len(byEmp))
	for id := range byEmp {
		empIDs = append(empIDs, id)
	}
	emps, err := e.loadEmployees(empIDs)
	if err != nil {
		return err
	}

	for _, emp := range emps {
		loc := timeutil.LoadLocation(emp.TimeZone)
		seen := map[string]bool{}
		for _, ev := range byEmp[emp.ID] {
			for _, r := range e.RefsForEvent(ev, loc, rules) {
				k := r.Scope + "|" + r.WindowStart.Format(time.RFC3339Nano)
				if seen[k] {
					continue
				}
				seen[k] = true
				// Event-driven evaluation is forced: the window may already
				// have an evaluation at this version, and a late event can
				// change the outcome.
				if err := e.EvaluateWindow(emp, r, rules, true); err != nil {
					return err
				}
			}
		}
	}
	return nil
}

func (e *Engine) loadEmployees(ids []uint64) ([]models.Employee, error) {
	if len(ids) == 0 {
		return nil, nil
	}
	var emps []models.Employee
	err := e.db.Where("id IN ?", ids).Find(&emps).Error
	return emps, err
}

// EvaluateWindow evaluates one window against the given rules. force=true
// re-evaluates even if the window already has an evaluation at the same rule
// version (used when new events arrive, including late/backfilled ones). The
// alert upsert makes repeated evaluation safe: no duplicate alerts are
// created, evidence is refreshed and stale alerts are withdrawn.
func (e *Engine) EvaluateWindow(emp models.Employee, ref WindowRef, rules map[string]RuleParams, force bool) error {
	now := e.now()

	switch ref.Scope {
	case models.ScopeBurst:
		p, ok := rules[models.RuleDownloadBurst]
		if !ok || !p.Enabled {
			return nil
		}
		// Only evaluate completed buckets, unless forced by an event inside it.
		if !force && ref.WindowEnd.After(now) {
			return nil
		}
		return e.evalBurst(emp, p, ref, force)
	case models.ScopeNight:
		p, ok := rules[models.RuleNightActivity]
		if !ok || !p.Enabled {
			return nil
		}
		if !force && ref.WindowEnd.After(now) {
			return nil // the night has not finished yet
		}
		return e.evalNight(emp, p, ref, force)
	case models.ScopeStat:
		p, ok := rules[models.RuleStatistical]
		if !ok || !p.Enabled {
			return nil
		}
		// Statistical evaluation always waits for the local day to complete,
		// even when forced by an incoming event; the sweep evaluates it once
		// local midnight has passed.
		if ref.WindowEnd.After(now) {
			return nil
		}
		return e.evalStat(emp, p, ref, timeutil.LoadLocation(emp.TimeZone), force)
	case models.ScopeFirstUSB:
		p, ok := rules[models.RuleFirstUSB]
		if !ok || !p.Enabled {
			return nil
		}
		return e.evalFirstUSB(emp, p, force)
	}
	return nil
}

// alreadyEvaluated reports whether a row exists for the window at the given
// version. Used by sweeps to avoid pointless re-reads; forced evaluation
// bypasses it.
func (e *Engine) alreadyEvaluated(ruleCode string, version uint32, empID uint64, ref WindowRef) (bool, error) {
	var n int64
	err := e.db.Model(&models.WindowEvaluation{}).
		Where("rule_code = ? AND rule_version = ? AND employee_id = ? AND window_scope = ? AND window_date = ? AND window_start = ?",
			ruleCode, version, empID, ref.Scope, ref.LabelDate, ref.WindowStart).
		Count(&n).Error
	return n > 0, err
}

func (e *Engine) evalBurst(emp models.Employee, p RuleParams, ref WindowRef, force bool) error {
	if !force {
		exists, err := e.alreadyEvaluated(models.RuleDownloadBurst, p.Version, emp.ID, ref)
		if err != nil || exists {
			return err
		}
	}
	var events []models.Event
	if err := e.db.Where("employee_id = ? AND event_type = ? AND occurred_at >= ? AND occurred_at < ?",
		emp.ID, models.EventTypeFileDownload, ref.WindowStart, ref.WindowEnd).
		Order("occurred_at").Find(&events).Error; err != nil {
		return err
	}
	count := len(events)
	fires := count > p.Threshold

	var first, last *time.Time
	var sampleIDs []string
	if count > 0 {
		t1 := events[0].OccurredAt
		t2 := events[count-1].OccurredAt
		first, last = &t1, &t2
		for _, ev := range events {
			if len(sampleIDs) >= 10 {
				break
			}
			sampleIDs = append(sampleIDs, ev.EventID)
		}
	}
	evidence := models.JSONMap{
		"window_start":     ref.WindowStart,
		"window_end":       ref.WindowEnd,
		"download_count":   count,
		"threshold":        p.Threshold,
		"window_minutes":   p.Window.Minutes(),
		"first_event_at":   first,
		"last_event_at":    last,
		"sample_event_ids": sampleIDs,
	}
	dedup := dedupFor(ref, models.RuleDownloadBurst, p.Version, emp.ID)
	title := fmt.Sprintf("%d file downloads within %d minutes", count, int(p.Window.Minutes()))
	if !fires {
		return e.withdraw(dedup, p, emp, ref, evidence)
	}
	return e.upsertAlert(alertInput{
		Dedup:       dedup,
		RuleCode:    models.RuleDownloadBurst,
		Version:     p.Version,
		EmpID:       emp.ID,
		WindowStart: &ref.WindowStart,
		WindowEnd:   &ref.WindowEnd,
		Severity:    "medium",
		Title:       title,
		Evidence:    evidence,
		LastEventAt: last,
	}, ref, true)
}

func (e *Engine) evalNight(emp models.Employee, p RuleParams, ref WindowRef, force bool) error {
	if !force {
		exists, err := e.alreadyEvaluated(models.RuleNightActivity, p.Version, emp.ID, ref)
		if err != nil || exists {
			return err
		}
	}
	var events []models.Event
	if err := e.db.Where("employee_id = ? AND occurred_at >= ? AND occurred_at < ?",
		emp.ID, ref.WindowStart, ref.WindowEnd).
		Order("occurred_at").Find(&events).Error; err != nil {
		return err
	}
	count := len(events)
	fires := count > 0
	byType := map[string]int{}
	var sampleIDs []string
	var last *time.Time
	for _, ev := range events {
		byType[ev.EventType]++
		if last == nil || ev.OccurredAt.After(*last) {
			t := ev.OccurredAt
			last = &t
		}
		if len(sampleIDs) < 10 {
			sampleIDs = append(sampleIDs, ev.EventID)
		}
	}
	evidence := models.JSONMap{
		"window_start":     ref.WindowStart,
		"window_end":       ref.WindowEnd,
		"local_date":       ref.LabelDate.Format("2006-01-02"),
		"time_zone":        emp.TimeZone,
		"start_hour":       p.StartHour,
		"end_hour":         p.EndHour,
		"event_count":      count,
		"by_type":          byType,
		"sample_event_ids": sampleIDs,
	}
	dedup := dedupFor(ref, models.RuleNightActivity, p.Version, emp.ID)
	title := fmt.Sprintf("Night activity (%02d:00-%02d:00 %s) on %s",
		p.StartHour, p.EndHour, emp.TimeZone, ref.LabelDate.Format("2006-01-02"))
	if !fires {
		return e.withdraw(dedup, p, emp, ref, evidence)
	}
	return e.upsertAlert(alertInput{
		Dedup:       dedup,
		RuleCode:    models.RuleNightActivity,
		Version:     p.Version,
		EmpID:       emp.ID,
		WindowStart: &ref.WindowStart,
		WindowEnd:   &ref.WindowEnd,
		Severity:    "medium",
		Title:       title,
		Evidence:    evidence,
		LastEventAt: last,
	}, ref, true)
}

func (e *Engine) evalFirstUSB(emp models.Employee, p RuleParams, force bool) error {
	ref := WindowRef{
		Scope:       models.ScopeFirstUSB,
		LabelDate:   sentinelDate,
		WindowStart: sentinelDate,
		WindowEnd:   sentinelDate,
	}
	var first models.Event
	err := e.db.Where("employee_id = ? AND event_type = ?", emp.ID, models.EventTypeUSB).
		Order("occurred_at ASC, id ASC").First(&first).Error
	if err != nil {
		if err == gorm.ErrRecordNotFound {
			// No USB events at all: record a no-alert evaluation so sweeps skip.
			return e.recordEvaluation(p, emp, ref, false, nil)
		}
		return err
	}
	if !force {
		// Sweep-driven call: skip when already evaluated at this version after
		// the current earliest USB event was received (a late earlier USB
		// arriving after that forces an event-driven refresh).
		var we models.WindowEvaluation
		lookupErr := e.db.Where("rule_code = ? AND rule_version = ? AND employee_id = ? AND window_scope = ?",
			models.RuleFirstUSB, p.Version, emp.ID, models.ScopeFirstUSB).First(&we).Error
		if lookupErr == nil && !we.EvaluatedAt.Before(first.ReceivedAt) {
			return nil
		} else if lookupErr != nil && lookupErr != gorm.ErrRecordNotFound {
			return lookupErr
		}
	}
	loc := timeutil.LoadLocation(emp.TimeZone)
	when := first.OccurredAt.In(loc)
	evidence := models.JSONMap{
		"first_usb_at":    first.OccurredAt,
		"first_usb_local": when.Format("2006-01-02T15:04:05Z07:00"),
		"time_zone":       emp.TimeZone,
		"event_id":        first.EventID,
		"device":          first.Metadata["device"],
		"vendor":          first.Metadata["vendor"],
	}
	dedup := dedupFor(ref, models.RuleFirstUSB, p.Version, emp.ID)
	title := fmt.Sprintf("First USB device use by %s", emp.Name)
	t := first.OccurredAt
	return e.upsertAlert(alertInput{
		Dedup:       dedup,
		RuleCode:    models.RuleFirstUSB,
		Version:     p.Version,
		EmpID:       emp.ID,
		Severity:    "high",
		Title:       title,
		Evidence:    evidence,
		LastEventAt: &t,
	}, ref, true)
}

func (e *Engine) evalStat(emp models.Employee, p RuleParams, ref WindowRef, loc *time.Location, force bool) error {
	if !force {
		exists, err := e.alreadyEvaluated(models.RuleStatistical, p.Version, emp.ID, ref)
		if err != nil || exists {
			return err
		}
	}
	// Load all events in [day-historyDays, dayEnd), then bucket by local date.
	start := ref.WindowStart.AddDate(0, 0, -p.HistoryDays)
	var events []models.Event
	if err := e.db.Where("employee_id = ? AND occurred_at >= ? AND occurred_at < ?",
		emp.ID, start, ref.WindowEnd).
		Order("occurred_at").Find(&events).Error; err != nil {
		return err
	}
	counts := map[string]float64{}
	for _, ev := range events {
		d := timeutil.LocalDate(ev.OccurredAt, loc).Format("2006-01-02")
		counts[d]++
	}
	targetLabel := ref.LabelDate.Format("2006-01-02")
	target := counts[targetLabel]

	// Baseline: the p.HistoryDays local days strictly before the target day.
	var samples []float64
	var sampleDays []string
	for i := 1; i <= p.HistoryDays; i++ {
		label := ref.LabelDate.AddDate(0, 0, -i).Format("2006-01-02")
		if c, ok := counts[label]; ok {
			samples = append(samples, c)
			sampleDays = append(sampleDays, label)
		}
	}
	sort.Strings(sampleDays)

	evidence := models.JSONMap{
		"local_date":      targetLabel,
		"time_zone":       emp.TimeZone,
		"day_event_count": target,
		"sample_size":     len(samples),
		"min_samples":     p.MinSamples,
		"z_threshold":     p.ZScore,
	}
	// Insufficient history: fixed rules only, no statistical alert.
	if len(samples) < p.MinSamples {
		evidence["insufficient_history"] = true
		return e.recordEvaluation(p, emp, ref, false, evidence)
	}
	mean, sd := MeanStddev(samples)
	z := ZScore(mean, sd, target)
	evidence["mean"] = mean
	evidence["stddev"] = sd
	// JSON cannot encode ±Inf: store the finite z when stddev>0, and a capped
	// marker + constant baseline flag when every baseline day was identical.
	if math.IsInf(z, 0) {
		evidence["z_score"] = nil
		evidence["z_score_note"] = "infinite: zero baseline variance and target above mean"
		evidence["constant_baseline"] = true
	} else {
		evidence["z_score"] = z
	}
	evidence["sample_days"] = sampleDays
	fires := z > p.ZScore // upward anomaly only (z=+Inf compares true)
	if !fires {
		return e.withdrawWithEvidence(p, emp, ref, evidence)
	}
	last := ref.WindowEnd.Add(-time.Second)
	dedup := dedupFor(ref, models.RuleStatistical, p.Version, emp.ID)
	title := fmt.Sprintf("Unusual activity volume on %s: %.0f events (z=%.2f)",
		targetLabel, target, z)
	return e.upsertAlert(alertInput{
		Dedup:       dedup,
		RuleCode:    models.RuleStatistical,
		Version:     p.Version,
		EmpID:       emp.ID,
		WindowStart: &ref.WindowStart,
		WindowEnd:   &ref.WindowEnd,
		Severity:    "high",
		Title:       title,
		Evidence:    evidence,
		LastEventAt: &last,
	}, ref, true)
}

type alertInput struct {
	Dedup       string
	RuleCode    string
	Version     uint32
	EmpID       uint64
	Severity    string
	Title       string
	Evidence    models.JSONMap
	WindowStart *time.Time
	WindowEnd   *time.Time
	LastEventAt *time.Time
}

// upsertAlert inserts the alert or refreshes evidence on the existing one
// (deduplication by dedup_key). Window evaluation is recorded in the same
// transaction.
func (e *Engine) upsertAlert(in alertInput, ref WindowRef, hadAlert bool) error {
	now := e.now()
	return e.db.Transaction(func(tx *gorm.DB) error {
		var existing models.Alert
		err := tx.Where("dedup_key = ?", in.Dedup).First(&existing).Error
		if err == gorm.ErrRecordNotFound {
			a := models.Alert{
				RuleCode:    in.RuleCode,
				RuleVersion: in.Version,
				EmployeeID:  in.EmpID,
				Status:      models.AlertStatusNew,
				Severity:    in.Severity,
				Title:       in.Title,
				DedupKey:    in.Dedup,
				WindowStart: in.WindowStart,
				WindowEnd:   in.WindowEnd,
				Evidence:    in.Evidence,
				FiredAt:     now,
				LastEventAt: in.LastEventAt,
				UpdatedAt:   now,
			}
			if err := tx.Create(&a).Error; err != nil {
				return err
			}
		} else if err != nil {
			return err
		} else {
			// Keep the original firing time, rule version and status; a
			// recomputation refreshes evidence only (the version that fired it
			// is preserved for auditability).
			if err := tx.Model(&existing).Updates(map[string]any{
				"evidence":      in.Evidence,
				"last_event_at": in.LastEventAt,
				"updated_at":    now,
			}).Error; err != nil {
				return err
			}
		}
		return recordEval(tx, in.RuleCode, in.Version, in.EmpID, ref, hadAlert, now)
	})
}

// withdraw removes a non-terminal alert and records a no-alert evaluation.
// Used when a backfilled recomputation shows the window no longer qualifies.
// Resolved / false-positive alerts are historical outcomes and are kept.
func (e *Engine) withdraw(dedup string, p RuleParams, emp models.Employee, ref WindowRef, evidence models.JSONMap) error {
	now := e.now()
	return e.db.Transaction(func(tx *gorm.DB) error {
		var existing models.Alert
		err := tx.Where("dedup_key = ?", dedup).First(&existing).Error
		if err == nil {
			if existing.Status != models.AlertStatusResolved &&
				existing.Status != models.AlertStatusFalsePositive {
				if err := tx.Delete(&existing).Error; err != nil {
					return err
				}
			} else {
				if err := tx.Model(&existing).Updates(map[string]any{
					"evidence":   evidence,
					"updated_at": now,
				}).Error; err != nil {
					return err
				}
			}
		} else if err != gorm.ErrRecordNotFound {
			return err
		}
		return recordEval(tx, p.Code, p.Version, emp.ID, ref, false, now)
	})
}

func (e *Engine) withdrawWithEvidence(p RuleParams, emp models.Employee, ref WindowRef, evidence models.JSONMap) error {
	return e.withdraw(dedupFor(ref, p.Code, p.Version, emp.ID), p, emp, ref, evidence)
}

func dedupFor(ref WindowRef, code string, version uint32, empID uint64) string {
	switch ref.Scope {
	case models.ScopeNight, models.ScopeStat:
		return fmt.Sprintf("%s:v%d:emp%d:%s:%s", code, version, empID, ref.Scope, ref.LabelDate.Format("2006-01-02"))
	case models.ScopeBurst:
		return fmt.Sprintf("%s:v%d:emp%d:%s:%s", code, version, empID, models.ScopeBurst, ref.WindowStart.UTC().Format("20060102T150405Z"))
	default:
		return fmt.Sprintf("%s:v%d:emp%d:%s", code, version, empID, ref.Scope)
	}
}

func (e *Engine) recordEvaluation(p RuleParams, emp models.Employee, ref WindowRef, hadAlert bool, evidence models.JSONMap) error {
	_ = evidence
	return e.db.Transaction(func(tx *gorm.DB) error {
		return recordEval(tx, p.Code, p.Version, emp.ID, ref, hadAlert, e.now())
	})
}

// recordEval upserts the window evaluation. Forced re-evaluation at the same
// version refreshes evaluated_at/had_alert; new rule versions insert a new row
// (the unique key includes rule_version).
func recordEval(tx *gorm.DB, code string, version uint32, empID uint64, ref WindowRef, hadAlert bool, at time.Time) error {
	we := models.WindowEvaluation{
		RuleCode:    code,
		RuleVersion: version,
		EmployeeID:  empID,
		WindowScope: ref.Scope,
		WindowDate:  ref.LabelDate,
		WindowStart: ref.WindowStart,
		WindowEnd:   ref.WindowEnd,
		EvaluatedAt: at,
		HadAlert:    hadAlert,
	}
	return tx.Clauses(clause.OnConflict{
		Columns: []clause.Column{
			{Name: "rule_code"}, {Name: "rule_version"}, {Name: "employee_id"},
			{Name: "window_scope"}, {Name: "window_date"}, {Name: "window_start"},
		},
		DoUpdates: clause.AssignmentColumns([]string{"window_end", "evaluated_at", "had_alert"}),
	}).Create(&we).Error
}
