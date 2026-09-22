// Package service contains the CostLens business logic: idempotent batch
// import with transactional rollback, summary recomputation, budget versioning
// with threshold alerts, and versioned anomaly detection.
package service

import (
	"time"

	"costlens/internal/db"
	"github.com/jackc/pgx/v5/pgxpool"
)

// advisoryLockID serializes every mutating maintenance operation (imports,
// budget version changes, full rebuilds). It makes incremental recomputation
// and rebuild mutually exclusive, which is what guarantees that a rebuild
// neither loses nor double-counts concurrent imports: contending transactions
// block at the lock and apply against committed data.
const advisoryLockID int64 = 88117335 // arbitrary 'COSTLENS'

type Service struct {
	pool *pgxpool.Pool
	q    *db.Queries
}

func New(pool *pgxpool.Pool) *Service {
	return &Service{pool: pool, q: db.New(pool)}
}

func (s *Service) Pool() *pgxpool.Pool { return s.pool }

// ---------- JSON DTOs (amounts always serialized as decimal strings) ----------

type OrgDTO struct {
	ID         int64     `json:"id"`
	ExternalID string    `json:"external_id"`
	Name       string    `json:"name"`
	CreatedAt  time.Time `json:"created_at"`
}

type CostCenterDTO struct {
	ID    int64  `json:"id"`
	OrgID int64  `json:"org_id"`
	Code  string `json:"code"`
	Name  string `json:"name"`
}

type AccountDTO struct {
	ID           int64  `json:"id"`
	OrgID        int64  `json:"org_id"`
	CostCenterID int64  `json:"cost_center_id"`
	ExternalID   string `json:"external_id"`
	Name         string `json:"name"`
}

type SummaryDTO struct {
	Scope        string `json:"scope"`
	AccountID    *int64 `json:"account_id,omitempty"`
	CostCenterID *int64 `json:"cost_center_id,omitempty"`
	Date         string `json:"date"`
	Currency     string `json:"currency"`
	TotalAmount  string `json:"total_amount"`
	RecordCount  int32  `json:"record_count"`
}

type BillingRecordDTO struct {
	ID          int64  `json:"id"`
	AccountID   int64  `json:"account_id"`
	ResourceID  string `json:"resource_id"`
	Service     string `json:"service"`
	Date        string `json:"date"`
	Currency    string `json:"currency"`
	Amount      string `json:"amount"`
	ContentHash string `json:"content_hash"`
	BatchID     int64  `json:"batch_id"`
}

type BatchDTO struct {
	ID            int64     `json:"id"`
	AccountID     int64     `json:"account_id"`
	Filename      string    `json:"filename"`
	TotalRows     int32     `json:"total_rows"`
	InsertedRows  int32     `json:"inserted_rows"`
	DuplicateRows int32     `json:"duplicate_rows"`
	Status        string    `json:"status"`
	CreatedBy     *int64    `json:"created_by,omitempty"`
	CreatedAt     time.Time `json:"created_at"`
}

type BudgetDTO struct {
	ID           int64     `json:"id"`
	CostCenterID int64     `json:"cost_center_id"`
	Currency     string    `json:"currency"`
	Month        string    `json:"month"`
	Version      int32     `json:"version"`
	MonthlyLimit string    `json:"monthly_limit"`
	Active       bool      `json:"active"`
	CreatedAt    time.Time `json:"created_at"`
}

type BudgetAlertDTO struct {
	ID            int64     `json:"id"`
	BudgetID      int64     `json:"budget_id"`
	BudgetVersion int32     `json:"budget_version"`
	CostCenterID  int64     `json:"cost_center_id"`
	ThresholdPct  int32     `json:"threshold_pct"`
	SpentAmount   string    `json:"spent_amount"`
	Month         string    `json:"month"`
	Currency      string    `json:"currency"`
	TriggeredAt   time.Time `json:"triggered_at"`
}

type AnomalyDTO struct {
	ID              int64   `json:"id"`
	AccountID       int64   `json:"account_id"`
	Date            string  `json:"date"`
	Currency        string  `json:"currency"`
	Version         int32   `json:"version"`
	Status          string  `json:"status"`
	ActualAmount    string  `json:"actual_amount"`
	BaselineMean    *string `json:"baseline_mean,omitempty"`
	BaselineStd     *string `json:"baseline_std,omitempty"`
	ThresholdAmount *string `json:"threshold_amount,omitempty"`
	BaselineStart   *string `json:"baseline_start,omitempty"`
	BaselineEnd     *string `json:"baseline_end,omitempty"`
	BaselineDays    int32   `json:"baseline_days"`
}

type Paged[T any] struct {
	Items  []T   `json:"items"`
	Limit  int32 `json:"limit"`
	Offset int32 `json:"offset"`
	Total  int64 `json:"total,omitempty"`
}

func monthStart(t time.Time) time.Time {
	y, m, _ := t.Date()
	return time.Date(y, m, 1, 0, 0, 0, 0, t.Location())
}
