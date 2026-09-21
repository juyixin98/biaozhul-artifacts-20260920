package models

import (
	"encoding/json"
	"time"
)

// Campaign statuses.
const (
	CampaignStatusActive = "active"
	CampaignStatusPaused = "paused"
)

// Creative statuses.
const (
	CreativeStatusActive = "active"
	CreativeStatusPaused = "paused"
)

// Decision statuses.
const (
	DecisionStatusEvaluating = "evaluating" // row reserved, checks in flight (crash-safe)
	DecisionStatusReserved   = "reserved"   // budget & frequency pre-occupied
	DecisionStatusConfirmed  = "confirmed"  // impression confirmed, budget consumed
	DecisionStatusReleased   = "released"   // reservation expired, resources refunded
	DecisionStatusRejected   = "rejected"   // decision denied, nothing occupied
)

// Rules is the targeting rule set of a campaign. An empty slice means "match all".
type Rules struct {
	Regions []string `json:"regions"` // e.g. ["CN","US"]; empty = any region
	Devices []string `json:"devices"` // e.g. ["ios","android"]; empty = any device
	Hours   []int    `json:"hours"`   // UTC hours 0-23; empty = any hour
}

func (r Rules) MatchRegion(region string) bool {
	if len(r.Regions) == 0 {
		return true
	}
	for _, v := range r.Regions {
		if v == region {
			return true
		}
	}
	return false
}

func (r Rules) MatchDevice(device string) bool {
	if len(r.Devices) == 0 {
		return true
	}
	for _, v := range r.Devices {
		if v == device {
			return true
		}
	}
	return false
}

func (r Rules) MatchHour(hour int) bool {
	if len(r.Hours) == 0 {
		return true
	}
	for _, v := range r.Hours {
		if v == hour {
			return true
		}
	}
	return false
}

func (r Rules) Marshal() string {
	b, _ := json.Marshal(r)
	return string(b)
}

func ParseRules(s string) Rules {
	var r Rules
	if s != "" {
		_ = json.Unmarshal([]byte(s), &r)
	}
	return r
}

// Campaign is an ad campaign. Budgets are integer cents.
// ReservedTotal/SpentTotal are maintained atomically via conditional SQL updates.
type Campaign struct {
	ID            uint64    `gorm:"primaryKey" json:"id"`
	Name          string    `gorm:"size:128;not null" json:"name"`
	Status        string    `gorm:"size:16;not null;index" json:"status"`
	StartAt       time.Time `gorm:"not null" json:"start_at"`
	EndAt         time.Time `gorm:"not null" json:"end_at"`
	TotalBudget   int64     `gorm:"not null" json:"total_budget"` // cents
	DailyCap      int64     `gorm:"not null;default:0" json:"daily_cap"`
	ReservedTotal int64     `gorm:"not null;default:0" json:"reserved_total"`
	SpentTotal    int64     `gorm:"not null;default:0" json:"spent_total"`
	Rules         string    `gorm:"type:json" json:"-"`
	CreatedAt     time.Time `json:"created_at"`
	UpdatedAt     time.Time `json:"updated_at"`
}

// Creative belongs to a campaign; only active creatives may serve.
type Creative struct {
	ID         uint64    `gorm:"primaryKey" json:"id"`
	CampaignID uint64    `gorm:"not null;index" json:"campaign_id"`
	Name       string    `gorm:"size:128;not null" json:"name"`
	Content    string    `gorm:"type:text" json:"content"`
	Status     string    `gorm:"size:16;not null" json:"status"`
	CreatedAt  time.Time `json:"created_at"`
	UpdatedAt  time.Time `json:"updated_at"`
}

// Decision records one decision request and its outcome. RequestID is the
// idempotency key; PayloadHash detects same-ID/different-payload conflicts.
type Decision struct {
	ID           uint64     `gorm:"primaryKey" json:"id"`
	RequestID    string     `gorm:"size:64;not null;uniqueIndex" json:"request_id"`
	PayloadHash  string     `gorm:"size:64;not null" json:"-"`
	CampaignID   uint64     `gorm:"not null;index" json:"campaign_id"`
	CreativeID   uint64     `gorm:"not null;default:0" json:"creative_id"`
	UserID       string     `gorm:"size:64;not null;index" json:"user_id"`
	Region       string     `gorm:"size:32" json:"region"`
	Device       string     `gorm:"size:32" json:"device"`
	Cost         int64      `gorm:"not null" json:"cost"` // cents
	OccurredAt   time.Time  `gorm:"not null" json:"occurred_at"`
	Day          string     `gorm:"size:10;not null;index" json:"day"` // UTC day of OccurredAt, YYYY-MM-DD
	Hour         int        `gorm:"not null" json:"hour"`              // UTC hour of OccurredAt
	Status       string     `gorm:"size:16;not null;index" json:"status"`
	RejectReason string     `gorm:"size:64" json:"reject_reason,omitempty"`
	ExpiresAt    *time.Time `json:"expires_at,omitempty"`
	ConfirmedAt  *time.Time `json:"confirmed_at,omitempty"`
	CreatedAt    time.Time  `json:"created_at"`
	UpdatedAt    time.Time  `json:"updated_at"`
}

// DailyBudget tracks per-campaign per-UTC-day budget (cents).
type DailyBudget struct {
	ID         uint64    `gorm:"primaryKey" json:"id"`
	CampaignID uint64    `gorm:"not null;uniqueIndex:uk_campaign_day" json:"campaign_id"`
	Day        string    `gorm:"size:10;not null;uniqueIndex:uk_campaign_day" json:"day"`
	Reserved   int64     `gorm:"not null;default:0" json:"reserved"`
	Spent      int64     `gorm:"not null;default:0" json:"spent"`
	CreatedAt  time.Time `json:"created_at"`
	UpdatedAt  time.Time `json:"updated_at"`
}

// Frequency scopes.
const (
	ScopeUserHour    = "user_hour" // max 3 impressions per user per UTC hour
	ScopeCampaignDay = "camp_day"  // max 20 impressions per campaign per UTC day
)

// FreqCounter is a generic frequency counter keyed by (Scope, Key).
type FreqCounter struct {
	ID        uint64    `gorm:"primaryKey" json:"id"`
	Scope     string    `gorm:"size:32;not null;uniqueIndex:uk_scope_key" json:"scope"`
	Key       string    `gorm:"size:191;not null;uniqueIndex:uk_scope_key" json:"key"`
	Count     int64     `gorm:"not null;default:0" json:"count"`
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}
