package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	mysqlconn "github.com/go-sql-driver/mysql"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"

	"sitevitals/internal/models"
)

// ErrNotFound is returned when a row does not exist.
var ErrNotFound = errors.New("not found")

// ErrLeaseLost means the caller's lease token is no longer authoritative:
// the task was reclaimed by another worker, already committed, or cancelled.
var ErrLeaseLost = errors.New("task lease lost")

// ErrConflict means a state transition is not allowed
// (e.g. retrying an already succeeded task).
var ErrConflict = errors.New("task state conflict")

// Store wraps GORM and owns all durable queue/report operations.
type Store struct {
	db *gorm.DB
}

func New(db *gorm.DB) *Store { return &Store{db: db} }

// DB exposes the underlying handle for migrations and tests.
func (s *Store) DB() *gorm.DB { return s.db }

// AutoMigrate creates the initial schema. Safe to run repeatedly; it only
// adds missing tables/columns. deploy/sql/001_init.sql documents the schema.
// Composite indexes touching url(VARCHAR 2048) are created explicitly with
// prefix columns because MySQL index keys are capped at 3072 bytes.
func (s *Store) AutoMigrate(ctx context.Context) error {
	if err := s.db.WithContext(ctx).Set("gorm:table_options", "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4").
		AutoMigrate(
			&models.Site{},
			&models.Task{},
			&models.Run{},
			&models.Report{},
			&models.Budget{},
			&models.BudgetAlert{},
			&models.Comparison{},
		); err != nil {
		return fmt.Errorf("auto migrate: %w", err)
	}
	if err := s.ensurePrefixIndexes(ctx); err != nil {
		return fmt.Errorf("create indexes: %w", err)
	}
	return nil
}

// ensurePrefixIndexes is idempotent: it skips indexes MySQL already reports
// in information_schema (avoiding a noisy ER_DUP_KEYNAME error on every boot).
func (s *Store) ensurePrefixIndexes(ctx context.Context) error {
	type existing struct {
		Name string
	}
	var have []existing
	if err := s.db.WithContext(ctx).Raw(`
		SELECT DISTINCT index_name AS name
		FROM information_schema.statistics
		WHERE table_schema = DATABASE()`).Scan(&have).Error; err != nil {
		return err
	}
	exists := map[string]bool{}
	for _, h := range have {
		exists[h.Name] = true
	}
	indexes := map[string]string{
		"idx_tasks_dispatch":           `CREATE INDEX idx_tasks_dispatch ON tasks (url(191), viewport, status, created_at)`,
		"idx_runs_compare":             `CREATE INDEX idx_runs_compare ON runs (url(191), viewport, status)`,
		"idx_comparisons_url_viewport": `CREATE INDEX idx_comparisons_url_viewport ON comparisons (url(191), viewport)`,
	}
	for name, stmt := range indexes {
		if exists[name] {
			continue
		}
		if err := s.db.WithContext(ctx).Exec(stmt).Error; err != nil {
			var myErr *mysqlconn.MySQLError
			if errors.As(err, &myErr) && myErr.Number == 1061 {
				continue // created concurrently
			}
			return err
		}
	}
	return nil
}

// ---------- sites ----------

func (s *Store) CreateSite(ctx context.Context, site *models.Site) error {
	return s.db.WithContext(ctx).Create(site).Error
}

func (s *Store) UpdateSite(ctx context.Context, site *models.Site) error {
	return s.db.WithContext(ctx).Save(site).Error
}

func (s *Store) GetSite(ctx context.Context, id uint) (*models.Site, error) {
	var site models.Site
	if err := s.db.WithContext(ctx).First(&site, id).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &site, nil
}

func (s *Store) ListSites(ctx context.Context, includeDisabled bool) ([]models.Site, error) {
	q := s.db.WithContext(ctx).Order("id asc")
	if !includeDisabled {
		q = q.Where("enabled = ?", true)
	}
	var sites []models.Site
	err := q.Find(&sites).Error
	return sites, err
}

func (s *Store) DeleteSite(ctx context.Context, id uint) error {
	res := s.db.WithContext(ctx).Delete(&models.Site{}, id)
	if res.Error != nil {
		return res.Error
	}
	if res.RowsAffected == 0 {
		return ErrNotFound
	}
	return nil
}

// ---------- tasks ----------

// EnqueueRequest carries validated task input.
type EnqueueRequest struct {
	URL         string
	Viewport    models.Viewport
	Priority    int
	MaxAttempts int
}

// Enqueue creates a new task in the queued state.
func (s *Store) Enqueue(ctx context.Context, req EnqueueRequest) (*models.Task, error) {
	if req.MaxAttempts <= 0 {
		req.MaxAttempts = 3
	}
	t := &models.Task{
		URL:         req.URL,
		Viewport:    req.Viewport,
		Status:      models.StateQueued,
		Priority:    req.Priority,
		MaxAttempts: req.MaxAttempts,
		RunAfter:    time.Now().UTC(),
	}
	if err := s.db.WithContext(ctx).Create(t).Error; err != nil {
		return nil, err
	}
	return t, nil
}

func (s *Store) GetTask(ctx context.Context, id uint) (*models.Task, error) {
	var t models.Task
	if err := s.db.WithContext(ctx).First(&t, id).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &t, nil
}

func (s *Store) ListTasks(ctx context.Context, status string, limit, offset int) ([]models.Task, int64, error) {
	q := s.db.WithContext(ctx).Model(&models.Task{})
	if status != "" {
		q = q.Where("status = ?", status)
	}
	var total int64
	if err := q.Count(&total).Error; err != nil {
		return nil, 0, err
	}
	if limit <= 0 || limit > 200 {
		limit = 50
	}
	var tasks []models.Task
	err := q.Order("id desc").Limit(limit).Offset(offset).Find(&tasks).Error
	return tasks, total, err
}

// ClaimResult is the claimed task plus the freshly created run row.
type ClaimResult struct {
	Task *models.Task
	Run  *models.Run
}

// Claim atomically takes the oldest due queued task (or an expired lease),
// creates the run row for this attempt, and stamps a fresh lease. Uses
// SELECT ... FOR UPDATE SKIP LOCKED so concurrent workers never claim the
// same task, and every claimed attempt has a durable run row even if the
// process dies before writing anything else.
func (s *Store) Claim(ctx context.Context, owner string, lease time.Duration) (*ClaimResult, error) {
	var result *ClaimResult
	err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		now := time.Now().UTC()
		var task models.Task
		// Due = queued tasks past run_after, plus tasks with an expired lease
		// (crash recovery): their status stays running until reclaimed here.
		// Limit+Find (rather than First) keeps an empty queue quiet: First
		// would log ErrRecordNotFound on every poll.
		res := tx.Clauses(clause.Locking{Strength: "UPDATE", Options: "SKIP LOCKED"}).
			Where(
				tx.Where("status = ? AND run_after <= ?", models.StateQueued, now).
					Or("status = ? AND leased_until IS NOT NULL AND leased_until < ?", models.StateRunning, now),
			).
			Order("priority desc, id asc").
			Limit(1).
			Find(&task)
		if res.Error != nil {
			return res.Error
		}
		if res.RowsAffected == 0 {
			return nil // nothing to do
		}

		isReclaim := task.Status == models.StateRunning
		attempt := task.Attempts + 1
		token := newLeaseToken()
		until := now.Add(lease)
		started := now

		updates := map[string]interface{}{
			"status":       models.StateRunning,
			"attempts":     attempt,
			"owner":        owner,
			"lease_token":  token,
			"leased_until": until,
			"started_at":   started,
			"updated_at":   now,
			"error_code":   "",
			"error_msg":    "",
		}
		if isReclaim {
			// A recovered task keeps its retry budget; run_after is cleared
			// implicitly by the running status. Backoff does not apply to a
			// reclaim (the previous attempt never finished).
			updates["run_after"] = now
		}
		if err := tx.Model(&models.Task{}).Where("id = ?", task.ID).Updates(updates).Error; err != nil {
			return err
		}

		// Mark any previous in-flight attempt as abandoned. Normally a queued
		// task's prior run is already failed; the only running leftovers come
		// from crash recovery (either reclaimed here directly or reset to
		// queued by the defensive reaper). Its late commits must never
		// overwrite this new run's result.
		if err := tx.Model(&models.Run{}).
			Where("task_id = ? AND status = ? AND attempt_no <= ?", task.ID, models.StateRunning, task.Attempts).
			Updates(map[string]interface{}{
				"status":      models.StateAbandoned,
				"error_code":  "LEASE_EXPIRED",
				"error_msg":   "worker lease expired; attempt reclaimed by another worker",
				"finished_at": now,
				"updated_at":  now,
			}).Error; err != nil {
			return err
		}

		run := &models.Run{
			TaskID:    task.ID,
			AttemptNo: attempt,
			Status:    models.StateRunning,
			Owner:     owner,
			Viewport:  task.Viewport,
			URL:       task.URL,
			StartedAt: now,
		}
		if err := tx.Create(run).Error; err != nil {
			return err
		}

		task.Status = models.StateRunning
		task.Attempts = attempt
		task.Owner = strPtr(owner)
		task.LeaseToken = strPtr(token)
		task.LeasedUntil = &until
		task.StartedAt = &started
		result = &ClaimResult{Task: &task, Run: run}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return result, nil
}

// Heartbeat extends a lease while a browser run is in progress. A failed
// heartbeat (ErrLeaseLost) means the worker must abort: it was reclaimed.
func (s *Store) Heartbeat(ctx context.Context, taskID uint, token string, lease time.Duration) error {
	now := time.Now().UTC()
	res := s.db.WithContext(ctx).Model(&models.Task{}).
		Where("id = ? AND lease_token = ? AND status = ?", taskID, token, models.StateRunning).
		Updates(map[string]interface{}{"leased_until": now.Add(lease), "updated_at": now})
	if res.Error != nil {
		return res.Error
	}
	if res.RowsAffected == 0 {
		return ErrLeaseLost
	}
	return nil
}

// SuccessInput is everything a successful commit persists.
type SuccessInput struct {
	TaskID      uint
	Token       string
	RunID       uint
	FinalURL    string
	Collected   *RunMetrics
	ReportMD    string
	WriteAlerts func(runID uint) error // optional, executed inside the same tx
}

// CommitSuccess commits a successful run under the lease-token guard. It is
// idempotent against late commits: an old executor (whose token was rotated
// on reclaim, or whose run was marked abandoned) affects zero rows. Exactly
// one report can ever exist per task (DB unique index), so retries after a
// success cannot duplicate reports.
func (s *Store) CommitSuccess(ctx context.Context, in SuccessInput) error {
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		now := time.Now().UTC()
		var task models.Task
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).First(&task, in.TaskID).Error; err != nil {
			return err
		}
		if task.LeaseToken == nil || *task.LeaseToken != in.Token || task.Status != models.StateRunning {
			return ErrLeaseLost
		}

		runUpdate := in.Collected.ColumnUpdates(now)
		runUpdate["status"] = models.StateSucceeded
		runUpdate["final_url"] = in.FinalURL
		res := tx.Model(&models.Run{}).
			Where("id = ? AND task_id = ? AND status = ?", in.RunID, in.TaskID, models.StateRunning).
			Updates(runUpdate)
		if res.Error != nil {
			return res.Error
		}
		if res.RowsAffected == 0 {
			// Run was abandoned after a reclaim: stale executor, drop commit.
			return ErrLeaseLost
		}

		if err := tx.Model(&models.Task{}).
			Where("id = ? AND lease_token = ?", in.TaskID, in.Token).
			Updates(map[string]interface{}{
				"status":       models.StateSucceeded,
				"final_url":    in.FinalURL,
				"finished_at":  now,
				"leased_until": nil,
				"owner":        nil,
				"error_code":   "",
				"error_msg":    "",
				"updated_at":   now,
			}).Error; err != nil {
			return err
		}

		// Single report per task; IGNORE protects against a duplicate from any
		// pathological re-entry (the lease guard already prevents it).
		report := models.Report{TaskID: in.TaskID, RunID: in.RunID, Markdown: in.ReportMD, CreatedAt: now}
		if err := tx.Clauses(clause.OnConflict{DoNothing: true}).Create(&report).Error; err != nil {
			return err
		}

		if in.WriteAlerts != nil {
			if err := in.WriteAlerts(in.RunID); err != nil {
				return err
			}
		}
		return nil
	})
}

// FailureInput describes a failed attempt.
type FailureInput struct {
	TaskID    uint
	Token     string
	RunID     uint
	ErrorCode string
	ErrorMsg  string
}

// CommitFailure marks the run failed and either re-queues the task with
// exponential-style backoff (attempts remain) or moves it to dead when the
// retry budget is exhausted. Guarded by the same token+run-status checks.
func (s *Store) CommitFailure(ctx context.Context, in FailureInput) error {
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		now := time.Now().UTC()
		var task models.Task
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).First(&task, in.TaskID).Error; err != nil {
			return err
		}
		if task.LeaseToken == nil || *task.LeaseToken != in.Token || task.Status != models.StateRunning {
			return ErrLeaseLost
		}

		res := tx.Model(&models.Run{}).
			Where("id = ? AND task_id = ? AND status = ?", in.RunID, in.TaskID, models.StateRunning).
			Updates(map[string]interface{}{
				"status":      models.StateFailed,
				"error_code":  in.ErrorCode,
				"error_msg":   truncate(in.ErrorMsg, 1000),
				"finished_at": now,
				"updated_at":  now,
			})
		if res.Error != nil {
			return res.Error
		}
		if res.RowsAffected == 0 {
			return ErrLeaseLost
		}

		exhausted := task.Attempts >= task.MaxAttempts
		nextStatus := models.StateQueued
		runAfter := now.Add(backoff(task.Attempts))
		if exhausted {
			nextStatus = models.StateDead
			runAfter = now
		}
		taskUpdates := map[string]interface{}{
			"status":      nextStatus,
			"error_code":  in.ErrorCode,
			"error_msg":   truncate(in.ErrorMsg, 1000),
			"finished_at": now,
			"run_after":   runAfter,
			"updated_at":  now,
		}
		if exhausted {
			taskUpdates["leased_until"] = nil
			taskUpdates["owner"] = nil
		}
		if err := tx.Model(&models.Task{}).
			Where("id = ? AND lease_token = ?", in.TaskID, in.Token).
			Updates(taskUpdates).Error; err != nil {
			return err
		}
		return nil
	})
}

// RequeueExpired is a defensive sweep for running tasks whose lease expired
// but that Claim has not yet picked up (e.g. idle workers). It only frees the
// lease; the next Claim re-creates ownership. Returns the number touched.
func (s *Store) RequeueExpired(ctx context.Context) (int64, error) {
	now := time.Now().UTC()
	res := s.db.WithContext(ctx).Model(&models.Task{}).
		Where("status = ? AND leased_until IS NOT NULL AND leased_until < ?", models.StateRunning, now).
		Updates(map[string]interface{}{
			"status":       models.StateQueued,
			"leased_until": nil,
			"owner":        nil,
			"run_after":    now,
			"updated_at":   now,
		})
	return res.RowsAffected, res.Error
}

// RetryTask manually re-queues a failed/dead task. Succeeded tasks cannot be
// retried (409): re-running a URL is a new task, and this guarantees a
// successful task never gets a second report.
func (s *Store) RetryTask(ctx context.Context, id uint) (*models.Task, error) {
	var t *models.Task
	err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var task models.Task
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).First(&task, id).Error; err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				return ErrNotFound
			}
			return err
		}
		switch task.Status {
		case models.StateFailed, models.StateDead:
			task.Status = models.StateQueued
			task.RunAfter = time.Now().UTC()
			task.FinishedAt = nil
			task.ErrorCode = ""
			task.ErrorMsg = ""
			task.LeasedUntil = nil
			task.Owner = nil
			task.MaxAttempts += 2 // give an explicitly retried task fresh budget
			if err := tx.Save(&task).Error; err != nil {
				return err
			}
			t = &task
			return nil
		case models.StateSucceeded:
			return fmt.Errorf("%w: task %d already succeeded; enqueue a new task to re-measure", ErrConflict, id)
		default:
			return fmt.Errorf("%w: task %d is %s, retry only after it settles", ErrConflict, id, task.Status)
		}
	})
	return t, err
}

// ---------- runs / reports / comparisons ----------

func (s *Store) GetRun(ctx context.Context, id uint) (*models.Run, error) {
	var r models.Run
	if err := s.db.WithContext(ctx).First(&r, id).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	if err := hydrateRun(&r); err != nil {
		return nil, err
	}
	return &r, nil
}

func (s *Store) ListRunsByTask(ctx context.Context, taskID uint) ([]models.Run, error) {
	var rs []models.Run
	if err := s.db.WithContext(ctx).Where("task_id = ?", taskID).Order("attempt_no asc").Find(&rs).Error; err != nil {
		return nil, err
	}
	for i := range rs {
		if err := hydrateRun(&rs[i]); err != nil {
			return nil, err
		}
	}
	return rs, nil
}

func (s *Store) GetReportByTask(ctx context.Context, taskID uint) (*models.Report, error) {
	var r models.Report
	if err := s.db.WithContext(ctx).Where("task_id = ?", taskID).First(&r).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &r, nil
}

// LatestSuccessfulRun returns the most recent successful run for a normalized
// URL + viewport, used as the automatic comparison baseline.
func (s *Store) LatestSuccessfulRun(ctx context.Context, normalizedURL string, vp models.Viewport, excludeTaskID uint) (*models.Run, error) {
	var r models.Run
	err := s.db.WithContext(ctx).
		Where("status = ? AND url = ? AND viewport = ? AND task_id <> ?",
			models.StateSucceeded, normalizedURL, vp, excludeTaskID).
		Order("id desc").First(&r).Error
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	if err := hydrateRun(&r); err != nil {
		return nil, err
	}
	return &r, nil
}

func (s *Store) SaveComparison(ctx context.Context, c *models.Comparison) error {
	return s.db.WithContext(ctx).Create(c).Error
}

func (s *Store) ListComparisons(ctx context.Context, limit int) ([]models.Comparison, error) {
	if limit <= 0 {
		limit = 20
	}
	var cs []models.Comparison
	if err := s.db.WithContext(ctx).Order("id desc").Limit(limit).Find(&cs).Error; err != nil {
		return nil, err
	}
	for i := range cs {
		_ = json.Unmarshal([]byte(cs[i].DiffJSON), &cs[i].Diff)
	}
	return cs, nil
}

// CreateAlert persists one budget breach.
func (s *Store) CreateAlert(ctx context.Context, a *models.BudgetAlert) error {
	return s.db.WithContext(ctx).Create(a).Error
}

func (s *Store) ListAlerts(ctx context.Context, runID uint, limit int) ([]models.BudgetAlert, error) {
	q := s.db.WithContext(ctx)
	if runID != 0 {
		q = q.Where("run_id = ?", runID)
	}
	if limit <= 0 {
		limit = 100
	}
	var as []models.BudgetAlert
	err := q.Order("id desc").Limit(limit).Find(&as).Error
	return as, err
}

// ---------- budgets ----------

func (s *Store) UpsertBudget(ctx context.Context, b *models.Budget) error {
	return s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		var existing models.Budget
		err := tx.Where("site_id = ?", b.SiteID).First(&existing).Error
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return tx.Create(b).Error
		}
		if err != nil {
			return err
		}
		b.ID = existing.ID
		b.CreatedAt = existing.CreatedAt
		return tx.Save(b).Error
	})
}

func (s *Store) GetBudget(ctx context.Context, siteID uint) (*models.Budget, error) {
	var b models.Budget
	if err := s.db.WithContext(ctx).Where("site_id = ?", siteID).First(&b).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &b, nil
}

// EffectiveBudget returns the per-site budget merged over global defaults.
func (s *Store) EffectiveBudget(ctx context.Context, siteID uint) (*models.Budget, error) {
	var global models.Budget
	gErr := s.db.WithContext(ctx).Where("site_id = 0").First(&global).Error
	if gErr != nil && !errors.Is(gErr, gorm.ErrRecordNotFound) {
		return nil, gErr
	}
	eff := &BudgetDefaults
	if gErr == nil {
		eff = mergeBudget(&BudgetDefaults, &global)
	}
	if siteID == 0 {
		return eff, nil
	}
	var site models.Budget
	if err := s.db.WithContext(ctx).Where("site_id = ?", siteID).First(&site).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return eff, nil
		}
		return nil, err
	}
	return mergeBudget(eff, &site), nil
}
