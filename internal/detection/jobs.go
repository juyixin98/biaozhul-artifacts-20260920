package detection

import (
	"context"
	"fmt"
	"time"

	"anomalywatch/internal/models"
	"anomalywatch/internal/timeutil"

	"gorm.io/gorm"
)

// Audit action name shared with the HTTP layer (kept here so the escalation
// job needs no extra package).
const AuditAlertTransition = "alert.transition"

// WithJobLock runs fn while holding a MySQL named lock. Returns (false,nil) if
// the lock could not be acquired (another scheduler instance holds it). The
// lock is held on one dedicated connection and released deterministically, so
// duplicated scheduler ticks / replicas never run the same job concurrently.
func (e *Engine) WithJobLock(ctx context.Context, name string, fn func() error) (bool, error) {
	sqlDB, err := e.db.DB()
	if err != nil {
		return false, err
	}
	conn, err := sqlDB.Conn(ctx)
	if err != nil {
		return false, err
	}
	defer conn.Close()

	var got int
	timeoutSec := int(e.cfg.LockTimeout / time.Second)
	if err := conn.QueryRowContext(ctx, "SELECT GET_LOCK(?, ?)", name, timeoutSec).Scan(&got); err != nil {
		return false, err
	}
	if got != 1 {
		return false, nil
	}
	defer func() {
		_, _ = conn.ExecContext(context.Background(), "SELECT RELEASE_LOCK(?)", name)
	}()
	return true, fn()
}

// RunSweep is the restart-safe backstop: it finds every detection window in
// the allowed backfill horizon that has no evaluation at the current rule
// version and evaluates it. Because event ingestion is the only thing that
// creates windows and sweep + inline processing cover all windows, a restart
// never leaves events unaccounted for.
func (e *Engine) RunSweep(ctx context.Context) error {
	rules, err := LoadRules(e.cfg, e.db)
	if err != nil {
		return err
	}
	var emps []models.Employee
	if err := e.db.WithContext(ctx).Order("id").Find(&emps).Error; err != nil {
		return err
	}
	horizonStart := e.now().Add(-e.cfg.MaxBackfillAge)
	for _, emp := range emps {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if err := e.sweepEmployee(ctx, emp, rules, horizonStart); err != nil {
			return fmt.Errorf("sweep employee %d: %w", emp.ID, err)
		}
	}
	return nil
}

func (e *Engine) sweepEmployee(ctx context.Context, emp models.Employee, rules map[string]RuleParams, horizonStart time.Time) error {
	loc := timeutil.LoadLocation(emp.TimeZone)

	// One read of the employee's events in the horizon; derive window refs
	// from it the same way ingest does.
	var events []models.Event
	if err := e.db.WithContext(ctx).
		Where("employee_id = ? AND occurred_at >= ?", emp.ID, horizonStart).
		Order("occurred_at").Find(&events).Error; err != nil {
		return err
	}

	burstSeen := map[string]bool{}
	nightSeen := map[string]bool{}
	usbExists := false
	for _, ev := range events {
		for _, ref := range e.RefsForEvent(ev, loc, rules) {
			switch ref.Scope {
			case models.ScopeBurst:
				burstSeen[ref.WindowStart.Format(time.RFC3339Nano)] = true
			case models.ScopeNight:
				nightSeen[ref.LabelDate.Format("2006-01-02")] = true
			case models.ScopeFirstUSB:
				usbExists = true
			}
		}
	}

	activeNightKeys := map[string]bool{}

	if p := rules[models.RuleDownloadBurst]; p.Enabled {
		for key := range burstSeen {
			ws, err := time.Parse(time.RFC3339Nano, key)
			if err != nil {
				return err
			}
			ref := WindowRef{
				Scope:       models.ScopeBurst,
				LabelDate:   timeutil.MidnightUTC(ws),
				WindowStart: ws,
				WindowEnd:   ws.Add(p.Window),
			}
			exists, err := e.alreadyEvaluated(models.RuleDownloadBurst, p.Version, emp.ID, ref)
			if err != nil {
				return err
			}
			if !exists {
				if err := e.EvaluateWindow(emp, ref, rules, false); err != nil {
					return err
				}
			}
		}
	}

	if p := rules[models.RuleNightActivity]; p.Enabled {
		for label := range nightSeen {
			day, err := time.ParseInLocation("2006-01-02", label, loc)
			if err != nil {
				return err
			}
			s, end := timeutil.NightBounds(day, p.StartHour, p.EndHour)
			ref := WindowRef{
				Scope:       models.ScopeNight,
				LabelDate:   timeutil.DateLabelOfDate(day),
				WindowStart: s,
				WindowEnd:   end,
			}
			activeNightKeys[dedupFor(ref, models.RuleNightActivity, p.Version, emp.ID)] = true
			exists, err := e.alreadyEvaluated(models.RuleNightActivity, p.Version, emp.ID, ref)
			if err != nil {
				return err
			}
			if !exists {
				if err := e.EvaluateWindow(emp, ref, rules, false); err != nil {
					return err
				}
			}
		}
		// Reconciliation: withdraw open alerts whose window no longer has night
		// activity under the current zone/version (e.g. after a time-zone
		// change, or after the night rule was edited to a version that moved
		// the window). Resolved/false-positive history is preserved.
		if err := e.withdrawStaleAlerts(emp.ID, models.RuleNightActivity, p.Version, activeNightKeys, horizonStart); err != nil {
			return err
		}
	}

	if p := rules[models.RuleFirstUSB]; p.Enabled && usbExists {
		if err := e.evalFirstUSB(emp, p, false); err != nil {
			return err
		}
	}

	if p := rules[models.RuleStatistical]; p.Enabled {
		if err := e.sweepStatWindows(emp, p, rules, horizonStart, loc); err != nil {
			return err
		}
	}
	return nil
}

// withdrawStaleAlerts deletes open (non-terminal) alerts for ruleCode at the
// current version whose dedup key is not in keep, and whose window starts at or
// after horizonStart. It is the reconciliation half of sweeps for rules whose
// windows can disappear under a changed time zone or edited definition.
func (e *Engine) withdrawStaleAlerts(empID uint64, ruleCode string, version uint32, keep map[string]bool, horizonStart time.Time) error {
	var alerts []models.Alert
	if err := e.db.
		Where("employee_id = ? AND rule_code = ? AND rule_version = ?", empID, ruleCode, version).
		Where("status NOT IN ?", []string{models.AlertStatusResolved, models.AlertStatusFalsePositive}).
		Where("window_start IS NULL OR window_start >= ?", horizonStart).
		Find(&alerts).Error; err != nil {
		return err
	}
	for _, a := range alerts {
		if keep[a.DedupKey] {
			continue
		}
		if err := e.db.Delete(&a).Error; err != nil {
			return err
		}
	}
	return nil
}

// sweepStatWindows enumerates every completed local day in the backfill
// horizon and evaluates it if missing at the current version.
func (e *Engine) sweepStatWindows(emp models.Employee, p RuleParams, rules map[string]RuleParams, horizonStart time.Time, loc *time.Location) error {
	now := e.now()
	firstDay := timeutil.LocalDate(horizonStart, loc)
	lastDay := timeutil.LocalDate(now.Add(-24*time.Hour), loc) // completed days only
	for day := firstDay; !day.After(lastDay); day = day.AddDate(0, 0, 1) {
		s := time.Date(day.Year(), day.Month(), day.Day(), 0, 0, 0, 0, loc)
		ref := WindowRef{
			Scope:       models.ScopeStat,
			LabelDate:   timeutil.DateLabelOfDate(s),
			WindowStart: s.UTC(),
			WindowEnd:   s.AddDate(0, 0, 1).UTC(),
		}
		exists, err := e.alreadyEvaluated(models.RuleStatistical, p.Version, emp.ID, ref)
		if err != nil {
			return err
		}
		if !exists {
			if err := e.EvaluateWindow(emp, ref, rules, false); err != nil {
				return err
			}
		}
	}
	return nil
}

// EscalateOverdue moves every still-"new" alert older than the escalation age
// to "escalated" and writes an audit row per alert (actor = system). It is
// idempotent: only status='new' rows are touched.
func (e *Engine) EscalateOverdue() (int, error) {
	cutoff := e.now().Add(-e.cfg.EscalationAge)
	var escalated int
	err := e.db.Transaction(func(tx *gorm.DB) error {
		var alerts []models.Alert
		if err := tx.Where("status = ? AND fired_at <= ?", models.AlertStatusNew, cutoff).
			Find(&alerts).Error; err != nil {
			return err
		}
		for _, a := range alerts {
			now := e.now()
			if err := tx.Model(&a).Updates(map[string]any{
				"status":          models.AlertStatusEscalated,
				"escalated_at":    now,
				"acknowledged_at": now,
				"updated_at":      now,
			}).Error; err != nil {
				return err
			}
			detail := models.JSONMap{
				"from":   models.AlertStatusNew,
				"to":     models.AlertStatusEscalated,
				"reason": "auto_escalation_timeout",
			}
			if err := tx.Create(&models.AuditLog{
				ActorName:  "system",
				Action:     AuditAlertTransition,
				EntityType: "alert",
				EntityID:   fmt.Sprintf("%d", a.ID),
				Detail:     detail,
				CreatedAt:  now,
			}).Error; err != nil {
				return err
			}
			escalated++
		}
		return nil
	})
	return escalated, err
}

// (Time-zone invalidation is performed by the admin service in the same
// transaction as the employee update.)
