// Package service implements the ad decision, confirmation and expiry logic.
//
// Concurrency model: every counter mutation is a conditional SQL UPDATE whose
// WHERE clause enforces the invariant (e.g. reserved + spent + cost <= cap).
// MySQL row locks make these atomic, so concurrent deciders can never exceed
// budget or frequency limits. Status transitions (reserved -> confirmed /
// released) are also conditional updates, so exactly one side wins a race.
package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"

	"targetcraft/internal/models"
)

const (
	MaxUserHourlyImpressions    = 3
	MaxCampaignDailyImpressions = 20
)

// Rejection reasons (also persisted on the decision row).
const (
	ReasonCampaignNotFound  = "campaign_not_found"
	ReasonCampaignNotActive = "campaign_not_active"
	ReasonOutsideSchedule   = "outside_schedule_window"
	ReasonRegionNotTargeted = "region_not_targeted"
	ReasonDeviceNotTargeted = "device_not_targeted"
	ReasonHourNotTargeted   = "hour_not_targeted"
	ReasonCreativeNotFound  = "creative_not_found"
	ReasonCreativeNotActive = "creative_not_active"
	ReasonCreativeMismatch  = "creative_campaign_mismatch"
	ReasonNoActiveCreative  = "no_active_creative"
	ReasonTotalBudget       = "total_budget_exceeded"
	ReasonDailyBudget       = "daily_budget_exceeded"
	ReasonUserHourlyFreq    = "user_hourly_frequency_exceeded"
	ReasonCampaignDailyFreq = "campaign_daily_frequency_exceeded"
	ReasonInterrupted       = "interrupted"
)

// Typed errors mapped to HTTP statuses by the API layer.
var (
	ErrNotFound = errors.New("decision not found")
	ErrConflict = errors.New("request_id already used with a different payload")
	ErrExpired  = errors.New("reservation expired")
	ErrReleased = errors.New("reservation already released")
)

// budgetExceeded is an internal sentinel carrying the rejection reason.
type budgetExceeded struct{ reason string }

func (e *budgetExceeded) Error() string { return e.reason }

type DecideRequest struct {
	RequestID  string    `json:"request_id" binding:"required"`
	UserID     string    `json:"user_id" binding:"required"`
	CampaignID uint64    `json:"campaign_id" binding:"required"`
	CreativeID uint64    `json:"creative_id"` // optional; 0 = pick any active creative
	Region     string    `json:"region"`
	Device     string    `json:"device"`
	OccurredAt time.Time `json:"occurred_at" binding:"required"`
	Cost       int64     `json:"cost" binding:"required,gt=0"` // cents
}

type DecideResult struct {
	Approved bool             `json:"approved"`
	Reason   string           `json:"reason,omitempty"`
	Decision *models.Decision `json:"decision"`
}

type Service struct {
	db  *gorm.DB
	ttl time.Duration
	now func() time.Time // injectable clock for tests
}

func New(db *gorm.DB, reservationTTL time.Duration) *Service {
	return &Service{db: db, ttl: reservationTTL, now: func() time.Time { return time.Now().UTC() }}
}

// SetClock overrides the clock (tests only).
func (s *Service) SetClock(now func() time.Time) { s.now = now }

func payloadHash(req DecideRequest) string {
	h := sha256.New()
	fmt.Fprintf(h, "%s|%d|%d|%s|%s|%s|%d",
		req.UserID, req.CampaignID, req.CreativeID, req.Region, req.Device,
		req.OccurredAt.UTC().Format(time.RFC3339Nano), req.Cost)
	return hex.EncodeToString(h.Sum(nil))
}

func utcBucket(t time.Time) (day string, hour int) {
	u := t.UTC()
	return u.Format("2006-01-02"), u.Hour()
}

func userHourKey(userID string, t time.Time) string {
	return fmt.Sprintf("%s|%s", userID, t.UTC().Format("2006-01-02T15"))
}

func campaignDayKey(campaignID uint64, day string) string {
	return fmt.Sprintf("%d|%s", campaignID, day)
}

// Decide evaluates a decision request. It is idempotent: the same RequestID
// with the same payload replays the original outcome; a different payload
// yields ErrConflict.
func (s *Service) Decide(ctx context.Context, req DecideRequest) (*DecideResult, error) {
	if req.RequestID == "" || req.UserID == "" || req.CampaignID == 0 || req.Cost <= 0 || req.OccurredAt.IsZero() {
		return nil, fmt.Errorf("invalid request: request_id, user_id, campaign_id, occurred_at and cost>0 are required")
	}

	day, hour := utcBucket(req.OccurredAt)
	d := models.Decision{
		RequestID:   req.RequestID,
		PayloadHash: payloadHash(req),
		CampaignID:  req.CampaignID,
		CreativeID:  req.CreativeID,
		UserID:      req.UserID,
		Region:      req.Region,
		Device:      req.Device,
		Cost:        req.Cost,
		OccurredAt:  req.OccurredAt.UTC(),
		Day:         day,
		Hour:        hour,
		Status:      models.DecisionStatusEvaluating,
	}

	// Reserve the request ID first: this makes the decision idempotent even
	// for rejections, and survives crashes (stale 'evaluating' rows are
	// cleaned by the sweeper).
	res := s.db.WithContext(ctx).Clauses(clause.OnConflict{DoNothing: true}).Create(&d)
	if res.Error != nil {
		return nil, res.Error
	}
	if res.RowsAffected == 0 {
		var existing models.Decision
		if err := s.db.WithContext(ctx).Where("request_id = ?", req.RequestID).First(&existing).Error; err != nil {
			return nil, err
		}
		if existing.PayloadHash != d.PayloadHash {
			return nil, fmt.Errorf("%w: %s", ErrConflict, req.RequestID)
		}
		return resultOf(&existing), nil
	}

	return s.evaluateAndReserve(ctx, &d, req), nil
}

func resultOf(d *models.Decision) *DecideResult {
	return &DecideResult{
		Approved: d.Status == models.DecisionStatusReserved || d.Status == models.DecisionStatusConfirmed,
		Reason:   d.RejectReason,
		Decision: d,
	}
}

// reject persists the rejection and returns it as the result.
func (s *Service) reject(ctx context.Context, d *models.Decision, reason string) *DecideResult {
	_ = s.db.WithContext(ctx).Model(d).Updates(map[string]any{
		"status":        models.DecisionStatusRejected,
		"reject_reason": reason,
	}).Error
	d.Status = models.DecisionStatusRejected
	d.RejectReason = reason
	return &DecideResult{Approved: false, Reason: reason, Decision: d}
}

func (s *Service) evaluateAndReserve(ctx context.Context, d *models.Decision, req DecideRequest) *DecideResult {
	var campaign models.Campaign
	if err := s.db.WithContext(ctx).First(&campaign, req.CampaignID).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return s.reject(ctx, d, ReasonCampaignNotFound)
		}
		return s.reject(ctx, d, ReasonCampaignNotFound)
	}
	if campaign.Status != models.CampaignStatusActive {
		return s.reject(ctx, d, ReasonCampaignNotActive)
	}
	occ := req.OccurredAt.UTC()
	if occ.Before(campaign.StartAt) || occ.After(campaign.EndAt) {
		return s.reject(ctx, d, ReasonOutsideSchedule)
	}
	rules := models.ParseRules(campaign.Rules)
	if !rules.MatchRegion(req.Region) {
		return s.reject(ctx, d, ReasonRegionNotTargeted)
	}
	if !rules.MatchDevice(req.Device) {
		return s.reject(ctx, d, ReasonDeviceNotTargeted)
	}
	if !rules.MatchHour(d.Hour) {
		return s.reject(ctx, d, ReasonHourNotTargeted)
	}

	creativeID, reason := s.pickCreative(ctx, req)
	if reason != "" {
		return s.reject(ctx, d, reason)
	}
	d.CreativeID = creativeID

	// Atomic reservation: all counter increments in one transaction, each
	// guarded by its limit. Any failure rolls the whole reservation back.
	err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if err := s.reserveBudget(tx, &campaign, d); err != nil {
			return err
		}
		if err := s.reserveFreq(tx, models.ScopeUserHour, userHourKey(req.UserID, req.OccurredAt), MaxUserHourlyImpressions); err != nil {
			return err
		}
		if err := s.reserveFreq(tx, models.ScopeCampaignDay, campaignDayKey(campaign.ID, d.Day), MaxCampaignDailyImpressions); err != nil {
			return err
		}
		expiresAt := s.now().Add(s.ttl)
		return tx.Model(d).Updates(map[string]any{
			"status":      models.DecisionStatusReserved,
			"creative_id": creativeID,
			"expires_at":  expiresAt,
		}).Error
	})
	if err != nil {
		var be *budgetExceeded
		if errors.As(err, &be) {
			return s.reject(ctx, d, be.reason)
		}
		// Unexpected store error: leave the row 'evaluating' so the sweeper
		// can mark it interrupted; surface the error to the caller.
		return &DecideResult{Approved: false, Reason: ReasonInterrupted, Decision: d}
	}

	d.Status = models.DecisionStatusReserved
	expiresAt := s.now().Add(s.ttl)
	d.ExpiresAt = &expiresAt
	return &DecideResult{Approved: true, Decision: d}
}

func (s *Service) pickCreative(ctx context.Context, req DecideRequest) (uint64, string) {
	if req.CreativeID != 0 {
		var c models.Creative
		if err := s.db.WithContext(ctx).First(&c, req.CreativeID).Error; err != nil {
			return 0, ReasonCreativeNotFound
		}
		if c.CampaignID != req.CampaignID {
			return 0, ReasonCreativeMismatch
		}
		if c.Status != models.CreativeStatusActive {
			return 0, ReasonCreativeNotActive
		}
		return c.ID, ""
	}
	var c models.Creative
	err := s.db.WithContext(ctx).
		Where("campaign_id = ? AND status = ?", req.CampaignID, models.CreativeStatusActive).
		Order("id").First(&c).Error
	if err != nil {
		return 0, ReasonNoActiveCreative
	}
	return c.ID, ""
}

// reserveBudget atomically pre-occupies total and daily budget.
func (s *Service) reserveBudget(tx *gorm.DB, campaign *models.Campaign, d *models.Decision) error {
	// Total budget (campaign row already exists).
	r := tx.Exec(
		"UPDATE campaigns SET reserved_total = reserved_total + ? WHERE id = ? AND reserved_total + spent_total + ? <= total_budget",
		d.Cost, campaign.ID, d.Cost)
	if r.Error != nil {
		return r.Error
	}
	if r.RowsAffected == 0 {
		return &budgetExceeded{ReasonTotalBudget}
	}

	// Daily budget (row may not exist yet for this UTC day).
	daily := models.DailyBudget{CampaignID: campaign.ID, Day: d.Day}
	if err := tx.Clauses(clause.OnConflict{DoNothing: true}).Create(&daily).Error; err != nil {
		return err
	}
	if campaign.DailyCap > 0 {
		r = tx.Exec(
			"UPDATE daily_budgets SET reserved = reserved + ? WHERE campaign_id = ? AND day = ? AND reserved + spent + ? <= ?",
			d.Cost, campaign.ID, d.Day, d.Cost, campaign.DailyCap)
	} else {
		r = tx.Exec(
			"UPDATE daily_budgets SET reserved = reserved + ? WHERE campaign_id = ? AND day = ?",
			d.Cost, campaign.ID, d.Day)
	}
	if r.Error != nil {
		return r.Error
	}
	if r.RowsAffected == 0 {
		return &budgetExceeded{ReasonDailyBudget}
	}
	return nil
}

// reserveFreq atomically increments a frequency counter if below max.
func (s *Service) reserveFreq(tx *gorm.DB, scope, key string, max int64) error {
	counter := models.FreqCounter{Scope: scope, Key: key}
	if err := tx.Clauses(clause.OnConflict{DoNothing: true}).Create(&counter).Error; err != nil {
		return err
	}
	reason := ReasonUserHourlyFreq
	if scope == models.ScopeCampaignDay {
		reason = ReasonCampaignDailyFreq
	}
	r := tx.Exec(
		"UPDATE freq_counters SET count = count + 1 WHERE scope = ? AND `key` = ? AND count < ?",
		scope, key, max)
	if r.Error != nil {
		return r.Error
	}
	if r.RowsAffected == 0 {
		return &budgetExceeded{reason}
	}
	return nil
}

// Confirm turns a reservation into actual consumption. Idempotent: confirming
// an already-confirmed reservation returns the original result. Confirming an
// expired or released reservation fails explicitly.
func (s *Service) Confirm(ctx context.Context, requestID string) (*models.Decision, error) {
	now := s.now()
	var d models.Decision

	err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
		if err := tx.Where("request_id = ?", requestID).First(&d).Error; err != nil {
			return err
		}
		// Conditional transition: only a still-valid reservation can confirm.
		// This is what makes confirm-vs-expiry race-safe — exactly one wins.
		r := tx.Exec(
			"UPDATE decisions SET status = ?, confirmed_at = ? WHERE request_id = ? AND status = ? AND expires_at > ?",
			models.DecisionStatusConfirmed, now, requestID, models.DecisionStatusReserved, now)
		if r.Error != nil {
			return r.Error
		}
		if r.RowsAffected == 0 {
			return nil // handled after the transaction by re-reading state
		}
		// Move reserved -> spent in both budget ledgers.
		if err := tx.Exec(
			"UPDATE campaigns SET reserved_total = reserved_total - ?, spent_total = spent_total + ? WHERE id = ?",
			d.Cost, d.Cost, d.CampaignID).Error; err != nil {
			return err
		}
		return tx.Exec(
			"UPDATE daily_budgets SET reserved = reserved - ?, spent = spent + ? WHERE campaign_id = ? AND day = ?",
			d.Cost, d.Cost, d.CampaignID, d.Day).Error
	})
	if err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}

	// Re-read to determine the outcome of the conditional update.
	var fresh models.Decision
	if err := s.db.WithContext(ctx).Where("request_id = ?", requestID).First(&fresh).Error; err != nil {
		return nil, err
	}
	switch fresh.Status {
	case models.DecisionStatusConfirmed:
		return &fresh, nil // confirmed by us, or idempotent replay
	case models.DecisionStatusReleased:
		return nil, ErrReleased
	case models.DecisionStatusReserved:
		if fresh.ExpiresAt != nil && !fresh.ExpiresAt.After(now) {
			return nil, ErrExpired
		}
		return nil, fmt.Errorf("confirm failed for reservation in state %q", fresh.Status)
	default:
		return nil, fmt.Errorf("cannot confirm decision in state %q", fresh.Status)
	}
}

// SweepExpired releases reservations whose TTL has elapsed and refunds their
// budget and frequency. It is the recovery mechanism as well: because all
// state lives in MySQL, running it after a restart picks up every pending
// reservation left behind by a crashed or stopped process.
func (s *Service) SweepExpired(ctx context.Context) (released int, err error) {
	now := s.now()

	var expired []models.Decision
	if err := s.db.WithContext(ctx).
		Where("status = ? AND expires_at <= ?", models.DecisionStatusReserved, now).
		Limit(500).Find(&expired).Error; err != nil {
		return 0, err
	}

	for _, d := range expired {
		d := d
		err := s.db.WithContext(ctx).Transaction(func(tx *gorm.DB) error {
			// Conditional transition — if a confirm won the race, skip.
			r := tx.Exec(
				"UPDATE decisions SET status = ? WHERE id = ? AND status = ?",
				models.DecisionStatusReleased, d.ID, models.DecisionStatusReserved)
			if r.Error != nil {
				return r.Error
			}
			if r.RowsAffected == 0 {
				return errLostRace
			}
			if err := tx.Exec(
				"UPDATE campaigns SET reserved_total = GREATEST(reserved_total - ?, 0) WHERE id = ?",
				d.Cost, d.CampaignID).Error; err != nil {
				return err
			}
			if err := tx.Exec(
				"UPDATE daily_budgets SET reserved = GREATEST(reserved - ?, 0) WHERE campaign_id = ? AND day = ?",
				d.Cost, d.CampaignID, d.Day).Error; err != nil {
				return err
			}
			if err := s.refundFreq(tx, models.ScopeUserHour, userHourKey(d.UserID, d.OccurredAt)); err != nil {
				return err
			}
			return s.refundFreq(tx, models.ScopeCampaignDay, campaignDayKey(d.CampaignID, d.Day))
		})
		if err != nil && !errors.Is(err, errLostRace) {
			return released, err
		}
		if err == nil {
			released++
		}
	}

	// Clean up rows left in 'evaluating' by a crash between idempotency
	// insert and reservation commit. No counters were ever incremented for
	// them, so a plain status update is enough.
	staleBefore := now.Add(-time.Minute)
	if err := s.db.WithContext(ctx).Model(&models.Decision{}).
		Where("status = ? AND created_at < ?", models.DecisionStatusEvaluating, staleBefore).
		Updates(map[string]any{"status": models.DecisionStatusRejected, "reject_reason": ReasonInterrupted}).Error; err != nil {
		return released, err
	}
	return released, nil
}

var errLostRace = errors.New("lost status transition race")

func (s *Service) refundFreq(tx *gorm.DB, scope, key string) error {
	return tx.Exec(
		"UPDATE freq_counters SET count = GREATEST(count - 1, 0) WHERE scope = ? AND `key` = ?",
		scope, key).Error
}

// GetDecision returns one decision by request ID.
func (s *Service) GetDecision(ctx context.Context, requestID string) (*models.Decision, error) {
	var d models.Decision
	if err := s.db.WithContext(ctx).Where("request_id = ?", requestID).First(&d).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, ErrNotFound
		}
		return nil, err
	}
	return &d, nil
}

// ListDecisions returns decisions filtered by the given criteria.
func (s *Service) ListDecisions(ctx context.Context, campaignID uint64, userID, status string, limit int) ([]models.Decision, error) {
	q := s.db.WithContext(ctx).Order("id DESC")
	if campaignID != 0 {
		q = q.Where("campaign_id = ?", campaignID)
	}
	if userID != "" {
		q = q.Where("user_id = ?", userID)
	}
	if status != "" {
		q = q.Where("status = ?", status)
	}
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	var out []models.Decision
	return out, q.Limit(limit).Find(&out).Error
}

// ListSettlements returns confirmed decisions (the settlement records).
func (s *Service) ListSettlements(ctx context.Context, campaignID uint64, day string, limit int) ([]models.Decision, error) {
	q := s.db.WithContext(ctx).Where("status = ?", models.DecisionStatusConfirmed).Order("id DESC")
	if campaignID != 0 {
		q = q.Where("campaign_id = ?", campaignID)
	}
	if day != "" {
		q = q.Where("day = ?", day)
	}
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	var out []models.Decision
	return out, q.Limit(limit).Find(&out).Error
}

// SetCampaignStatus flips a campaign between active and paused. Pausing takes
// effect for new decisions immediately; existing reservations stay confirmable
// until their TTL elapses.
func (s *Service) SetCampaignStatus(ctx context.Context, id uint64, status string) error {
	r := s.db.WithContext(ctx).Model(&models.Campaign{}).Where("id = ?", id).Update("status", status)
	if r.Error != nil {
		return r.Error
	}
	if r.RowsAffected == 0 {
		return ErrNotFound
	}
	return nil
}
