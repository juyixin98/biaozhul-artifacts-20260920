// Package model defines the persistent entities of TargetCraft.
//
// Invariants enforced here and in internal/service:
//   - All money is integer cents; all times are UTC with millisecond precision
//     (MySQL datetime(3)).
//   - Budget is accounted per UTC day (BudgetDay.Day = "YYYY-MM-DD" of the
//     decision's OccurredAt); user frequency per UTC natural hour
//     (UserHour.HourStart).
package model

import "time"

const (
	CampaignStatusActive = "active"
	CampaignStatusPaused = "paused"

	CreativeStatusActive = "active"
	CreativeStatusPaused = "paused"

	DecisionStatusReserved  = "reserved"
	DecisionStatusConfirmed = "confirmed"
	DecisionStatusReleased  = "released"
	DecisionStatusRejected  = "rejected"

	SettlementTypeConfirm = "confirm"
	SettlementTypeRelease = "release"
)

// Campaign is an ad campaign with a schedule, integer-cent budgets and
// targeting rules. TotalReservedCents/TotalSpentCents are maintained under
// the campaign row lock and mirror the sum over its BudgetDay rows.
type Campaign struct {
	ID                   uint64    `gorm:"primaryKey" json:"id"`
	Name                 string    `gorm:"size:128;not null" json:"name"`
	Status               string    `gorm:"size:16;not null;index" json:"status"`
	StartAt              time.Time `gorm:"type:datetime(3);not null" json:"start_at"`
	EndAt                time.Time `gorm:"type:datetime(3);not null" json:"end_at"`
	TotalBudgetCents     int64     `gorm:"not null" json:"total_budget_cents"`
	DailyCapCents        int64     `gorm:"not null" json:"daily_cap_cents"`
	TotalReservedCents   int64     `gorm:"not null;default:0" json:"total_reserved_cents"`
	TotalSpentCents      int64     `gorm:"not null;default:0" json:"total_spent_cents"`
	Regions              []string  `gorm:"serializer:json" json:"regions"`      // empty = all regions
	Devices              []string  `gorm:"serializer:json" json:"devices"`      // empty = all devices
	HourWindows          []string  `gorm:"serializer:json" json:"hour_windows"` // "HH:MM-HH:MM" UTC, empty = all day
	CreatedAt            time.Time `json:"created_at"`
	UpdatedAt            time.Time `json:"updated_at"`
}

// Creative is a servable ad unit belonging to exactly one campaign.
type Creative struct {
	ID         uint64    `gorm:"primaryKey" json:"id"`
	CampaignID uint64    `gorm:"index;not null" json:"campaign_id"`
	Name       string    `gorm:"size:128;not null" json:"name"`
	Status     string    `gorm:"size:16;not null" json:"status"`
	CreatedAt  time.Time `json:"created_at"`
	UpdatedAt  time.Time `json:"updated_at"`
}

// BudgetDay holds per-campaign per-UTC-day accounting. Rows are created with
// INSERT ... ON DUPLICATE KEY UPDATE id=id and then locked FOR UPDATE.
type BudgetDay struct {
	ID             uint64    `gorm:"primaryKey" json:"id"`
	CampaignID     uint64    `gorm:"uniqueIndex:uk_budget_day;not null" json:"campaign_id"`
	Day            string    `gorm:"type:char(10);uniqueIndex:uk_budget_day;not null" json:"day"` // YYYY-MM-DD, UTC
	ReservedCents  int64     `gorm:"not null;default:0" json:"reserved_cents"`
	SpentCents     int64     `gorm:"not null;default:0" json:"spent_cents"`
	ReservedCount  int       `gorm:"not null;default:0" json:"reserved_count"`
	ConfirmedCount int       `gorm:"not null;default:0" json:"confirmed_count"`
	CreatedAt      time.Time `json:"created_at"`
	UpdatedAt      time.Time `json:"updated_at"`
}

// UserHour holds per-user per-UTC-hour frequency counters (cross-campaign).
type UserHour struct {
	ID             uint64    `gorm:"primaryKey" json:"id"`
	UserID         string    `gorm:"size:64;uniqueIndex:uk_user_hour;not null" json:"user_id"`
	HourStart      time.Time `gorm:"type:datetime(3);uniqueIndex:uk_user_hour;not null" json:"hour_start"` // UTC, truncated to hour
	ReservedCount  int       `gorm:"not null;default:0" json:"reserved_count"`
	ConfirmedCount int       `gorm:"not null;default:0" json:"confirmed_count"`
	CreatedAt      time.Time `json:"created_at"`
	UpdatedAt      time.Time `json:"updated_at"`
}

// Decision is the idempotent record of one decide call, keyed by RequestID.
// ResponseBody stores the exact first response so a replayed request returns
// byte-identical output.
type Decision struct {
	ID           uint64     `gorm:"primaryKey" json:"id"`
	RequestID    string     `gorm:"size:64;uniqueIndex;not null" json:"request_id"`
	RequestHash  string     `gorm:"size:64;not null" json:"request_hash"`
	CampaignID   uint64     `gorm:"index;not null" json:"campaign_id"`
	CreativeID   uint64     `gorm:"not null" json:"creative_id"`
	UserID       string     `gorm:"size:64;index;not null" json:"user_id"`
	Region       string     `gorm:"size:64" json:"region"`
	Device       string     `gorm:"size:32" json:"device"`
	OccurredAt   time.Time  `gorm:"type:datetime(3);not null" json:"occurred_at"`
	CostCents    int64      `gorm:"not null" json:"cost_cents"`
	Accepted     bool       `gorm:"not null" json:"accepted"`
	RejectCode   string     `gorm:"size:64" json:"reject_code,omitempty"`
	RejectReason string     `gorm:"size:256" json:"reject_reason,omitempty"`
	Status       string     `gorm:"size:16;index;not null" json:"status"`
	ResponseBody string     `gorm:"type:json;not null" json:"-"`
	ExpiresAt    *time.Time `gorm:"type:datetime(3);index" json:"expires_at,omitempty"`
	ConfirmedAt  *time.Time `gorm:"type:datetime(3)" json:"confirmed_at,omitempty"`
	ReleasedAt   *time.Time `gorm:"type:datetime(3)" json:"released_at,omitempty"`
	CreatedAt    time.Time  `json:"created_at"`
	UpdatedAt    time.Time  `json:"updated_at"`
}

// Settlement is the immutable ledger entry produced when a reservation turns
// into real consumption (confirm) or is given back (release/expire).
type Settlement struct {
	ID          uint64    `gorm:"primaryKey" json:"id"`
	DecisionID  uint64    `gorm:"index;not null" json:"decision_id"`
	RequestID   string    `gorm:"size:64;index;not null" json:"request_id"`
	CampaignID  uint64    `gorm:"index;not null" json:"campaign_id"`
	UserID      string    `gorm:"size:64;index;not null" json:"user_id"`
	Type        string    `gorm:"size:16;index;not null" json:"type"` // confirm | release
	AmountCents int64     `gorm:"not null" json:"amount_cents"`
	Day         string    `gorm:"type:char(10);index;not null" json:"day"` // budget day the entry applies to
	CreatedAt   time.Time `json:"created_at"`
}
