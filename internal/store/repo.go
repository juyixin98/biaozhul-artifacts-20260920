package store

import (
	"context"
	"errors"

	"gorm.io/gorm"

	"sitevitals/internal/models"
)

// ErrNotFound mirrors gorm's not-found for callers that don't import gorm.
var ErrNotFound = gorm.ErrRecordNotFound

// Repo groups read/write helpers for the API layer.
type Repo struct{ DB *gorm.DB }

// NewRepo constructs a Repo.
func NewRepo(db *gorm.DB) *Repo { return &Repo{DB: db} }

// --- Sites & allow-list -----------------------------------------------------

// CreateSite inserts a site.
func (r *Repo) CreateSite(ctx context.Context, s *models.Site) error {
	return r.DB.WithContext(ctx).Create(s).Error
}

// ListSites returns all sites ordered by ID.
func (r *Repo) ListSites(ctx context.Context) ([]models.Site, error) {
	var out []models.Site
	err := r.DB.WithContext(ctx).Order("id ASC").Find(&out).Error
	return out, err
}

// ActivePolicyData returns the enabled sites and rules used to compile the checker.
func (r *Repo) ActivePolicyData(ctx context.Context) ([]models.Site, []models.AllowedURL, error) {
	var sites []models.Site
	if err := r.DB.WithContext(ctx).Where("enabled = ?", true).Find(&sites).Error; err != nil {
		return nil, nil, err
	}
	var rules []models.AllowedURL
	if err := r.DB.WithContext(ctx).Where("enabled = ?", true).Find(&rules).Error; err != nil {
		return nil, nil, err
	}
	return sites, rules, nil
}

// CreateAllowedURL inserts an allow-list rule.
func (r *Repo) CreateAllowedURL(ctx context.Context, u *models.AllowedURL) error {
	return r.DB.WithContext(ctx).Create(u).Error
}

// ListAllowedURLs returns all rules, optionally filtered by site.
func (r *Repo) ListAllowedURLs(ctx context.Context, siteID *uint64) ([]models.AllowedURL, error) {
	var out []models.AllowedURL
	q := r.DB.WithContext(ctx).Order("id ASC")
	if siteID != nil {
		q = q.Where("site_id = ?", *siteID)
	}
	err := q.Find(&out).Error
	return out, err
}

// --- Jobs / runs ------------------------------------------------------------

// GetJob loads one job.
func (r *Repo) GetJob(ctx context.Context, id uint64) (*models.Job, error) {
	var j models.Job
	if err := r.DB.WithContext(ctx).Take(&j, id).Error; err != nil {
		return nil, err
	}
	return &j, nil
}

// ListJobs returns recent jobs.
func (r *Repo) ListJobs(ctx context.Context, limit int) ([]models.Job, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	var out []models.Job
	err := r.DB.WithContext(ctx).Order("id DESC").Limit(limit).Find(&out).Error
	return out, err
}

// GetRun loads one run.
func (r *Repo) GetRun(ctx context.Context, id uint64) (*models.Run, error) {
	var run models.Run
	if err := r.DB.WithContext(ctx).Take(&run, id).Error; err != nil {
		return nil, err
	}
	return &run, nil
}

// LatestSucceededRun returns the most recent successful run for url+viewport.
func (r *Repo) LatestSucceededRun(ctx context.Context, targetURL, viewport string, beforeRunID uint64) (*models.Run, error) {
	var run models.Run
	q := r.DB.WithContext(ctx).
		Where("target_url = ? AND viewport = ? AND status = ?", targetURL, viewport, models.RunSucceeded)
	if beforeRunID > 0 {
		q = q.Where("id < ?", beforeRunID)
	}
	if err := q.Order("id DESC").Take(&run).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, nil
		}
		return nil, err
	}
	return &run, nil
}

// RunArtifacts bundles everything persisted for a run.
type RunArtifacts struct {
	Run       *models.Run
	Metrics   []models.Metric
	Resources []models.ResourceEntry
	Events    []models.RunEvent
}

// GetRunArtifacts loads a run plus metrics, waterfall and events.
func (r *Repo) GetRunArtifacts(ctx context.Context, runID uint64) (*RunArtifacts, error) {
	run, err := r.GetRun(ctx, runID)
	if err != nil {
		return nil, err
	}
	a := &RunArtifacts{Run: run}
	if err := r.DB.WithContext(ctx).Where("run_id = ?", runID).Order("id ASC").Find(&a.Metrics).Error; err != nil {
		return nil, err
	}
	if err := r.DB.WithContext(ctx).Where("run_id = ?", runID).
		Order("start_ms IS NULL, start_ms ASC, id ASC").Find(&a.Resources).Error; err != nil {
		return nil, err
	}
	if err := r.DB.WithContext(ctx).Where("run_id = ?", runID).Order("id ASC").Find(&a.Events).Error; err != nil {
		return nil, err
	}
	return a, nil
}

// ListRunsForJob returns every attempt of a job.
func (r *Repo) ListRunsForJob(ctx context.Context, jobID uint64) ([]models.Run, error) {
	var out []models.Run
	err := r.DB.WithContext(ctx).Where("job_id = ?", jobID).Order("attempt ASC").Find(&out).Error
	return out, err
}

// --- Budgets ---------------------------------------------------------------

// CreateBudget inserts a budget threshold.
func (r *Repo) CreateBudget(ctx context.Context, b *models.Budget) error {
	return r.DB.WithContext(ctx).Create(b).Error
}

// ListBudgets returns budgets optionally filtered by URL.
func (r *Repo) ListBudgets(ctx context.Context, targetURL *string) ([]models.Budget, error) {
	var out []models.Budget
	q := r.DB.WithContext(ctx).Order("id ASC")
	if targetURL != nil {
		q = q.Where("target_url = ?", *targetURL)
	}
	err := q.Find(&out).Error
	return out, err
}

// MatchingBudgets returns enabled budgets applicable to url/viewport.
// A budget with viewport "" applies to every viewport.
func (r *Repo) MatchingBudgets(ctx context.Context, targetURL, viewport string) ([]models.Budget, error) {
	var out []models.Budget
	err := r.DB.WithContext(ctx).
		Where("enabled = ? AND target_url = ? AND (viewport = ? OR viewport = ?)",
			true, targetURL, viewport, "").
		Order("id ASC").Find(&out).Error
	return out, err
}

// CreateBudgetEvaluation inserts one evaluation row.
func (r *Repo) CreateBudgetEvaluation(ctx context.Context, e *models.BudgetEvaluation) error {
	return r.DB.WithContext(ctx).Create(e).Error
}

// ListBudgetEvaluations returns recent evaluation rows for a run or URL.
func (r *Repo) ListBudgetEvaluations(ctx context.Context, runID *uint64, limit int) ([]models.BudgetEvaluation, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	var out []models.BudgetEvaluation
	q := r.DB.WithContext(ctx).Order("id DESC").Limit(limit)
	if runID != nil {
		q = q.Where("run_id = ?", *runID)
	}
	err := q.Find(&out).Error
	return out, err
}
