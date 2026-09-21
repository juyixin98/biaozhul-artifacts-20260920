// Package service implements the TargetCraft decision and settlement core.
//
// Concurrency & consistency invariants (guarded by tests in service_test.go):
//
//   - All money is integer cents; all times UTC, truncated to milliseconds
//     (MySQL datetime(3)) before being written.
//   - A decide that accepts runs in ONE transaction that takes FOR UPDATE row
//     locks in the fixed order campaign -> creative -> budget_day -> user_hour
//     and only then checks and increments counters. Counter rows are created
//     with ON DUPLICATE KEY UPDATE id=id before being locked. Concurrent
//     requests therefore serialize on the campaign row and can never
//     overspend budget or break frequency caps.
//   - Reservation lifecycle: reserved -> confirmed | released. Both confirm
//     and expiry use a conditional UPDATE ... WHERE status='reserved' and
//     decide ownership by RowsAffected, so exactly one of them wins and
//     nothing is ever double-counted.
//   - Recovery is DB-only: SweepExpired selects status='reserved' AND
//     expires_at<=now. It runs once at startup and then on a ticker; the
//     ticker only speeds things up. A late Confirm releases the reservation
//     itself inside its own transaction and then rejects. No authoritative
//     state lives in process memory.
//   - Business rejections are HTTP 200 with accepted:false (and are
//     persisted so a replay returns the same answer); only malformed input
//     and infrastructure failures are errors. The same request_id with a
//     different body is a 409 (SHA-256 of the canonical request).
package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"gorm.io/gorm"
	"gorm.io/gorm/clause"

	"targetcraft/internal/model"
)

const (
	// MaxUserImpressionsPerHour is the cross-campaign per-user per-UTC-hour cap.
	MaxUserImpressionsPerHour = 3
	// MaxCampaignImpressionsPerDay is the per-campaign per-UTC-day cap.
	MaxCampaignImpressionsPerDay = 20
)

// APIError is an error with an HTTP status and a machine-readable code.
type APIError struct {
	Status  int    `json:"-"`
	Code    string `json:"code"`
	Message string `json:"message"`
}

func (e *APIError) Error() string { return e.Code + ": " + e.Message }

func apiError(status int, code, msg string) *APIError {
	return &APIError{Status: status, Code: code, Message: msg}
}

// DecideRequest is one ad-decision call. RequestID is the idempotency key.
type DecideRequest struct {
	RequestID  string    `json:"request_id" binding:"required,max=64"`
	CreativeID uint64    `json:"creative_id" binding:"required"`
	UserID     string    `json:"user_id" binding:"required,max=64"`
	Region     string    `json:"region" binding:"max=64"`
	Device     string    `json:"device" binding:"max=32"`
	OccurredAt time.Time `json:"occurred_at" binding:"required"`
	CostCents  int64     `json:"cost_cents" binding:"required,gt=0"`
}

// DecideResponse is the canonical decide output; it is stored verbatim and
// replayed for duplicate request IDs.
type DecideResponse struct {
	RequestID    string     `json:"request_id"`
	Accepted     bool       `json:"accepted"`
	Status       string     `json:"status"`
	CampaignID   uint64     `json:"campaign_id,omitempty"`
	CreativeID   uint64     `json:"creative_id,omitempty"`
	UserID       string     `json:"user_id"`
	CostCents    int64      `json:"cost_cents"`
	Day          string     `json:"day"`
	RejectCode   string     `json:"reject_code,omitempty"`
	RejectReason string     `json:"reject_reason,omitempty"`
	ExpiresAt    *time.Time `json:"expires_at,omitempty"`
	DecidedAt    time.Time  `json:"decided_at"`
}

// ConfirmResponse is returned when a reservation becomes real consumption.
type ConfirmResponse struct {
	RequestID   string    `json:"request_id"`
	Status      string    `json:"status"`
	AmountCents int64     `json:"amount_cents"`
	Day         string    `json:"day"`
	ConfirmedAt time.Time `json:"confirmed_at"`
}

// Service holds the dependencies of the decision core. The clock is
// injectable so tests can travel in time.
type Service struct {
	db             *gorm.DB
	now            func() time.Time
	reservationTTL time.Duration
}

func New(gdb *gorm.DB) *Service {
	return &Service{db: gdb, now: time.Now, reservationTTL: 5 * time.Minute}
}

// SetClock overrides the clock (tests only).
func (s *Service) SetClock(now func() time.Time) { s.now = now }

// SetReservationTTL overrides the 5-minute reservation TTL (tests only).
func (s *Service) SetReservationTTL(d time.Duration) { s.reservationTTL = d }

// utcNow returns the current time in UTC truncated to milliseconds.
func (s *Service) utcNow() time.Time { return s.now().UTC().Truncate(time.Millisecond) }

// ---------------------------------------------------------------------------
// Campaign / creative management
// ---------------------------------------------------------------------------

type CreateCampaignRequest struct {
	Name             string    `json:"name" binding:"required,max=128"`
	StartAt          time.Time `json:"start_at" binding:"required"`
	EndAt            time.Time `json:"end_at" binding:"required"`
	TotalBudgetCents int64     `json:"total_budget_cents" binding:"gte=0"`
	DailyCapCents    int64     `json:"daily_cap_cents" binding:"gte=0"`
	Regions          []string  `json:"regions"`
	Devices          []string  `json:"devices"`
	HourWindows      []string  `json:"hour_windows"`
}

func (s *Service) CreateCampaign(_ context.Context, req CreateCampaignRequest) (*model.Campaign, error) {
	start := req.StartAt.UTC().Truncate(time.Millisecond)
	end := req.EndAt.UTC().Truncate(time.Millisecond)
	if !end.After(start) {
		return nil, apiError(400, "INVALID_SCHEDULE", "end_at must be after start_at")
	}
	for _, w := range req.HourWindows {
		if _, _, err := parseHourWindow(w); err != nil {
			return nil, apiError(400, "INVALID_HOUR_WINDOW", fmt.Sprintf("hour window %q: %v", w, err))
		}
	}
	c := &model.Campaign{
		Name:             req.Name,
		Status:           model.CampaignStatusActive,
		StartAt:          start,
		EndAt:            end,
		TotalBudgetCents: req.TotalBudgetCents,
		DailyCapCents:    req.DailyCapCents,
		Regions:          req.Regions,
		Devices:          req.Devices,
		HourWindows:      req.HourWindows,
	}
	if err := s.db.Create(c).Error; err != nil {
		return nil, err
	}
	return c, nil
}

func (s *Service) GetCampaign(_ context.Context, id uint64) (*model.Campaign, error) {
	var c model.Campaign
	if err := s.db.First(&c, id).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return nil, apiError(404, "CAMPAIGN_NOT_FOUND", fmt.Sprintf("campaign %d not found", id))
		}
		return nil, err
	}
	return &c, nil
}

// SetCampaignStatus pauses or resumes a campaign. Pausing blocks new
// decisions immediately; existing reservations stay confirmable until they
// expire, because Confirm never looks at the campaign status.
func (s *Service) SetCampaignStatus(_ context.Context, id uint64, status string) (*model.Campaign, error) {
	if status != model.CampaignStatusActive && status != model.CampaignStatusPaused {
		return nil, apiError(400, "INVALID_STATUS", "status must be active or paused")
	}
	res := s.db.Model(&model.Campaign{}).Where("id = ?", id).Update("status", status)
	if res.Error != nil {
		return nil, res.Error
	}
	if res.RowsAffected == 0 {
		return nil, apiError(404, "CAMPAIGN_NOT_FOUND", fmt.Sprintf("campaign %d not found", id))
	}
	return s.GetCampaign(context.Background(), id)
}

type CreateCreativeRequest struct {
	CampaignID uint64 `json:"campaign_id" binding:"required"`
	Name       string `json:"name" binding:"required,max=128"`
}

func (s *Service) CreateCreative(_ context.Context, req CreateCreativeRequest) (*model.Creative, error) {
	if _, err := s.GetCampaign(context.Background(), req.CampaignID); err != nil {
		return nil, err
	}
	c := &model.Creative{
		CampaignID: req.CampaignID,
		Name:       req.Name,
		Status:     model.CreativeStatusActive,
	}
	if err := s.db.Create(c).Error; err != nil {
		return nil, err
	}
	return c, nil
}

// ---------------------------------------------------------------------------
// Decide
// ---------------------------------------------------------------------------

// Decide evaluates one ad request. It is idempotent on RequestID: a replay
// with the identical body returns the stored first response; the same ID
// with a different body is a 409 conflict.
func (s *Service) Decide(_ context.Context, req DecideRequest) (*DecideResponse, error) {
	if req.RequestID == "" || req.UserID == "" || req.CreativeID == 0 {
		return nil, apiError(400, "INVALID_REQUEST", "request_id, creative_id and user_id are required")
	}
	if req.CostCents <= 0 {
		return nil, apiError(400, "INVALID_REQUEST", "cost_cents must be positive")
	}
	if req.OccurredAt.IsZero() {
		return nil, apiError(400, "INVALID_REQUEST", "occurred_at is required")
	}
	req.OccurredAt = req.OccurredAt.UTC().Truncate(time.Millisecond)
	hash := hashRequest(req)

	// Fast path: an identical request was already processed.
	if existing, err := s.findDecision(req.RequestID); err != nil {
		return nil, err
	} else if existing != nil {
		return replayDecision(existing, hash)
	}

	now := s.utcNow()
	var resp DecideResponse
	err := s.db.Transaction(func(tx *gorm.DB) error {
		r, err := s.decideInTx(tx, req, now)
		if err != nil {
			return err
		}
		resp = r
		return s.persistDecision(tx, req, hash, &resp)
	})
	if err != nil {
		if isDuplicateKey(err) {
			// A concurrent request with the same ID committed first.
			existing, ferr := s.findDecision(req.RequestID)
			if ferr != nil {
				return nil, ferr
			}
			if existing == nil {
				return nil, fmt.Errorf("duplicate request_id %q but no stored decision", req.RequestID)
			}
			return replayDecision(existing, hash)
		}
		return nil, err
	}
	return &resp, nil
}

// decideInTx performs the locked check-and-reserve. It increments counters
// only when every rule passes; the caller persists the decision row in the
// same transaction.
func (s *Service) decideInTx(tx *gorm.DB, req DecideRequest, now time.Time) (DecideResponse, error) {
	base := DecideResponse{
		RequestID:  req.RequestID,
		CreativeID: req.CreativeID,
		UserID:     req.UserID,
		CostCents:  req.CostCents,
		Day:        dayOf(req.OccurredAt),
		DecidedAt:  now,
	}
	reject := func(code, reason string) (DecideResponse, error) {
		r := base
		r.Accepted = false
		r.Status = model.DecisionStatusRejected
		r.RejectCode = code
		r.RejectReason = reason
		return r, nil
	}

	// Load the creative first (unlocked) to learn the campaign ID, then take
	// locks in the fixed order campaign -> creative -> budget_day -> user_hour.
	var creative model.Creative
	if err := tx.First(&creative, req.CreativeID).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return reject("CREATIVE_NOT_FOUND", fmt.Sprintf("creative %d does not exist", req.CreativeID))
		}
		return base, err
	}
	base.CampaignID = creative.CampaignID

	var campaign model.Campaign
	if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).
		First(&campaign, creative.CampaignID).Error; err != nil {
		if errors.Is(err, gorm.ErrRecordNotFound) {
			return reject("CAMPAIGN_NOT_FOUND", fmt.Sprintf("campaign %d does not exist", creative.CampaignID))
		}
		return base, err
	}
	if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).
		First(&creative, req.CreativeID).Error; err != nil {
		return base, err
	}

	if creative.Status != model.CreativeStatusActive {
		return reject("CREATIVE_NOT_ACTIVE", fmt.Sprintf("creative %d is %s", creative.ID, creative.Status))
	}
	if campaign.Status != model.CampaignStatusActive {
		return reject("CAMPAIGN_NOT_ACTIVE", fmt.Sprintf("campaign %d is %s", campaign.ID, campaign.Status))
	}
	if req.OccurredAt.Before(campaign.StartAt) {
		return reject("CAMPAIGN_NOT_STARTED", fmt.Sprintf("campaign starts at %s", campaign.StartAt.Format(time.RFC3339)))
	}
	if !req.OccurredAt.Before(campaign.EndAt) {
		return reject("CAMPAIGN_ENDED", fmt.Sprintf("campaign ended at %s", campaign.EndAt.Format(time.RFC3339)))
	}
	if len(campaign.Regions) > 0 && !contains(campaign.Regions, req.Region) {
		return reject("REGION_NOT_MATCHED", fmt.Sprintf("region %q not in campaign targeting", req.Region))
	}
	if len(campaign.Devices) > 0 && !contains(campaign.Devices, req.Device) {
		return reject("DEVICE_NOT_MATCHED", fmt.Sprintf("device %q not in campaign targeting", req.Device))
	}
	if len(campaign.HourWindows) > 0 && !inAnyHourWindow(campaign.HourWindows, req.OccurredAt) {
		return reject("TIME_WINDOW_NOT_MATCHED", "request time outside campaign hour windows")
	}

	// Budget checks against counters maintained under the campaign lock.
	if campaign.TotalSpentCents+campaign.TotalReservedCents+req.CostCents > campaign.TotalBudgetCents {
		return reject("TOTAL_BUDGET_EXHAUSTED", "campaign total budget exhausted")
	}

	day, err := lockBudgetDay(tx, campaign.ID, base.Day)
	if err != nil {
		return base, err
	}
	if day.SpentCents+day.ReservedCents+req.CostCents > campaign.DailyCapCents {
		return reject("DAILY_CAP_EXCEEDED", "campaign daily budget cap reached")
	}
	if day.ReservedCount+day.ConfirmedCount >= MaxCampaignImpressionsPerDay {
		return reject("CAMPAIGN_DAILY_CAP_REACHED", fmt.Sprintf("campaign daily impression cap of %d reached", MaxCampaignImpressionsPerDay))
	}

	hour, err := lockUserHour(tx, req.UserID, req.OccurredAt)
	if err != nil {
		return base, err
	}
	if hour.ReservedCount+hour.ConfirmedCount >= MaxUserImpressionsPerHour {
		return reject("USER_HOURLY_CAP_REACHED", fmt.Sprintf("user hourly cap of %d reached", MaxUserImpressionsPerHour))
	}

	// All checks passed: atomically reserve budget and frequency slots.
	if err := tx.Model(&model.Campaign{}).Where("id = ?", campaign.ID).
		Update("total_reserved_cents", gorm.Expr("total_reserved_cents + ?", req.CostCents)).Error; err != nil {
		return base, err
	}
	if err := tx.Model(&model.BudgetDay{}).Where("id = ?", day.ID).
		Updates(map[string]any{
			"reserved_cents": gorm.Expr("reserved_cents + ?", req.CostCents),
			"reserved_count": gorm.Expr("reserved_count + 1"),
		}).Error; err != nil {
		return base, err
	}
	if err := tx.Model(&model.UserHour{}).Where("id = ?", hour.ID).
		Update("reserved_count", gorm.Expr("reserved_count + 1")).Error; err != nil {
		return base, err
	}

	expires := now.Add(s.reservationTTL)
	resp := base
	resp.Accepted = true
	resp.Status = model.DecisionStatusReserved
	resp.ExpiresAt = &expires
	return resp, nil
}

// persistDecision stores the decision row with its canonical response so a
// later identical request replays byte-identical output.
func (s *Service) persistDecision(tx *gorm.DB, req DecideRequest, hash string, resp *DecideResponse) error {
	body, err := json.Marshal(resp)
	if err != nil {
		return err
	}
	d := model.Decision{
		RequestID:    req.RequestID,
		RequestHash:  hash,
		CampaignID:   resp.CampaignID,
		CreativeID:   req.CreativeID,
		UserID:       req.UserID,
		Region:       req.Region,
		Device:       req.Device,
		OccurredAt:   req.OccurredAt,
		CostCents:    req.CostCents,
		Accepted:     resp.Accepted,
		RejectCode:   resp.RejectCode,
		RejectReason: resp.RejectReason,
		Status:       resp.Status,
		ResponseBody: string(body),
	}
	if resp.Accepted {
		d.ExpiresAt = resp.ExpiresAt
	}
	return tx.Create(&d).Error
}

func (s *Service) findDecision(requestID string) (*model.Decision, error) {
	var d model.Decision
	err := s.db.Where("request_id = ?", requestID).First(&d).Error
	if errors.Is(err, gorm.ErrRecordNotFound) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &d, nil
}

// replayDecision returns the stored response for an identical replay, or a
// 409 when the same request_id carries a different body.
func replayDecision(d *model.Decision, hash string) (*DecideResponse, error) {
	if d.RequestHash != hash {
		return nil, apiError(409, "REQUEST_ID_CONFLICT",
			fmt.Sprintf("request_id %q already used with a different payload", d.RequestID))
	}
	var resp DecideResponse
	if err := json.Unmarshal([]byte(d.ResponseBody), &resp); err != nil {
		return nil, err
	}
	return &resp, nil
}

// ---------------------------------------------------------------------------
// Confirm
// ---------------------------------------------------------------------------

// Confirm turns a reservation into real consumption. It is idempotent: a
// repeated confirm returns the original result without double-counting. A
// confirm arriving after the reservation expired releases the reservation
// itself and is rejected with 410.
func (s *Service) Confirm(_ context.Context, requestID string) (*ConfirmResponse, error) {
	now := s.utcNow()
	var resp ConfirmResponse
	err := s.db.Transaction(func(tx *gorm.DB) error {
		var d model.Decision
		if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).
			Where("request_id = ?", requestID).First(&d).Error; err != nil {
			if errors.Is(err, gorm.ErrRecordNotFound) {
				return apiError(404, "DECISION_NOT_FOUND", fmt.Sprintf("decision %q not found", requestID))
			}
			return err
		}

		switch d.Status {
		case model.DecisionStatusRejected:
			return apiError(409, "DECISION_NOT_ACCEPTED", "decision was rejected; nothing to confirm")
		case model.DecisionStatusConfirmed:
			// Idempotent replay of an earlier confirm.
			resp = ConfirmResponse{
				RequestID:   d.RequestID,
				Status:      model.DecisionStatusConfirmed,
				AmountCents: d.CostCents,
				Day:         dayOf(d.OccurredAt),
				ConfirmedAt: *d.ConfirmedAt,
			}
			return nil
		case model.DecisionStatusReleased:
			return apiError(410, "RESERVATION_RELEASED", "reservation was already released")
		}

		// d.Status == reserved
		if d.ExpiresAt == nil || !now.Before(*d.ExpiresAt) {
			// Too late: release inside this transaction, then reject. The
			// conditional update guards against a concurrent sweeper.
			if err := releaseInTx(tx, &d, now); err != nil {
				return err
			}
			return apiError(410, "CONFIRMATION_EXPIRED", "reservation expired before confirmation")
		}

		res := tx.Model(&model.Decision{}).
			Where("id = ? AND status = ?", d.ID, model.DecisionStatusReserved).
			Updates(map[string]any{
				"status":       model.DecisionStatusConfirmed,
				"confirmed_at": now,
			})
		if res.Error != nil {
			return res.Error
		}
		if res.RowsAffected != 1 {
			return apiError(409, "RESERVATION_RACE_LOST", "reservation changed state concurrently")
		}
		if err := applyConfirmCounters(tx, &d); err != nil {
			return err
		}
		if err := insertSettlement(tx, &d, model.SettlementTypeConfirm, now); err != nil {
			return err
		}
		resp = ConfirmResponse{
			RequestID:   d.RequestID,
			Status:      model.DecisionStatusConfirmed,
			AmountCents: d.CostCents,
			Day:         dayOf(d.OccurredAt),
			ConfirmedAt: now,
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return &resp, nil
}

// applyConfirmCounters moves reserved -> spent on every counter. The caller
// holds the decision row lock; campaign/budget_day/user_hour rows are
// updated with atomic SQL expressions.
func applyConfirmCounters(tx *gorm.DB, d *model.Decision) error {
	day := dayOf(d.OccurredAt)
	hour := hourOf(d.OccurredAt)
	if err := tx.Model(&model.Campaign{}).Where("id = ?", d.CampaignID).
		Updates(map[string]any{
			"total_reserved_cents": gorm.Expr("total_reserved_cents - ?", d.CostCents),
			"total_spent_cents":    gorm.Expr("total_spent_cents + ?", d.CostCents),
		}).Error; err != nil {
		return err
	}
	if err := tx.Model(&model.BudgetDay{}).
		Where("campaign_id = ? AND day = ?", d.CampaignID, day).
		Updates(map[string]any{
			"reserved_cents":  gorm.Expr("reserved_cents - ?", d.CostCents),
			"spent_cents":     gorm.Expr("spent_cents + ?", d.CostCents),
			"reserved_count":  gorm.Expr("reserved_count - 1"),
			"confirmed_count": gorm.Expr("confirmed_count + 1"),
		}).Error; err != nil {
		return err
	}
	return tx.Model(&model.UserHour{}).
		Where("user_id = ? AND hour_start = ?", d.UserID, hour).
		Updates(map[string]any{
			"reserved_count":  gorm.Expr("reserved_count - 1"),
			"confirmed_count": gorm.Expr("confirmed_count + 1"),
		}).Error
}

// applyReleaseCounters gives a reservation back on every counter.
func applyReleaseCounters(tx *gorm.DB, d *model.Decision) error {
	day := dayOf(d.OccurredAt)
	hour := hourOf(d.OccurredAt)
	if err := tx.Model(&model.Campaign{}).Where("id = ?", d.CampaignID).
		Update("total_reserved_cents", gorm.Expr("total_reserved_cents - ?", d.CostCents)).Error; err != nil {
		return err
	}
	if err := tx.Model(&model.BudgetDay{}).
		Where("campaign_id = ? AND day = ?", d.CampaignID, day).
		Updates(map[string]any{
			"reserved_cents": gorm.Expr("reserved_cents - ?", d.CostCents),
			"reserved_count": gorm.Expr("reserved_count - 1"),
		}).Error; err != nil {
		return err
	}
	return tx.Model(&model.UserHour{}).
		Where("user_id = ? AND hour_start = ?", d.UserID, hour).
		Update("reserved_count", gorm.Expr("reserved_count - 1")).Error
}

func insertSettlement(tx *gorm.DB, d *model.Decision, typ string, now time.Time) error {
	return tx.Create(&model.Settlement{
		DecisionID:  d.ID,
		RequestID:   d.RequestID,
		CampaignID:  d.CampaignID,
		UserID:      d.UserID,
		Type:        typ,
		AmountCents: d.CostCents,
		Day:         dayOf(d.OccurredAt),
		CreatedAt:   now,
	}).Error
}

// ---------------------------------------------------------------------------
// Expiry sweep & recovery
// ---------------------------------------------------------------------------

// SweepExpired releases every reservation whose TTL has lapsed. It is safe
// to run at any time, from any process, any number of times: each release is
// a conditional UPDATE ... WHERE status='reserved', so a reservation is
// released exactly once no matter how confirm and sweeps interleave. Running
// it at startup is what recovers unprocessed reservations after a restart —
// no in-memory timers are involved.
func (s *Service) SweepExpired(_ context.Context) (int, error) {
	now := s.utcNow()
	var ids []uint64
	if err := s.db.Model(&model.Decision{}).
		Where("status = ? AND expires_at <= ?", model.DecisionStatusReserved, now).
		Limit(500).Pluck("id", &ids).Error; err != nil {
		return 0, err
	}
	released := 0
	for _, id := range ids {
		ok, err := s.releaseIfReserved(id, now)
		if err != nil {
			return released, err
		}
		if ok {
			released++
		}
	}
	return released, nil
}

func (s *Service) releaseIfReserved(id uint64, now time.Time) (bool, error) {
	released := false
	err := s.db.Transaction(func(tx *gorm.DB) error {
		res := tx.Model(&model.Decision{}).
			Where("id = ? AND status = ?", id, model.DecisionStatusReserved).
			Updates(map[string]any{
				"status":      model.DecisionStatusReleased,
				"released_at": now,
			})
		if res.Error != nil {
			return res.Error
		}
		if res.RowsAffected == 0 {
			return nil // confirm won the race; nothing to do
		}
		var d model.Decision
		if err := tx.First(&d, id).Error; err != nil {
			return err
		}
		if err := applyReleaseCounters(tx, &d); err != nil {
			return err
		}
		if err := insertSettlement(tx, &d, model.SettlementTypeRelease, now); err != nil {
			return err
		}
		released = true
		return nil
	})
	return released, err
}

// releaseInTx releases a reservation inside an existing transaction (used by
// a late Confirm). The caller holds the decision row lock.
func releaseInTx(tx *gorm.DB, d *model.Decision, now time.Time) error {
	res := tx.Model(&model.Decision{}).
		Where("id = ? AND status = ?", d.ID, model.DecisionStatusReserved).
		Updates(map[string]any{
			"status":      model.DecisionStatusReleased,
			"released_at": now,
		})
	if res.Error != nil {
		return res.Error
	}
	if res.RowsAffected != 1 {
		return apiError(409, "RESERVATION_RACE_LOST", "reservation changed state concurrently")
	}
	if err := applyReleaseCounters(tx, d); err != nil {
		return err
	}
	return insertSettlement(tx, d, model.SettlementTypeRelease, now)
}

// RunSweeper runs SweepExpired once immediately (startup recovery) and then
// on the given interval until ctx is cancelled. The ticker only accelerates
// cleanup; correctness never depends on it.
func (s *Service) RunSweeper(ctx context.Context, interval time.Duration) {
	_, _ = s.SweepExpired(ctx)
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			_, _ = s.SweepExpired(ctx)
		}
	}
}

// ---------------------------------------------------------------------------
// Queries
// ---------------------------------------------------------------------------

// GetDecision returns one stored decision by request ID.
func (s *Service) GetDecision(_ context.Context, requestID string) (*model.Decision, error) {
	d, err := s.findDecision(requestID)
	if err != nil {
		return nil, err
	}
	if d == nil {
		return nil, apiError(404, "DECISION_NOT_FOUND", fmt.Sprintf("decision %q not found", requestID))
	}
	return d, nil
}

type DecisionFilter struct {
	CampaignID uint64
	UserID     string
	Status     string
	Day        string
	Page       int
	PageSize   int
}

func (s *Service) ListDecisions(_ context.Context, f DecisionFilter) ([]model.Decision, int64, error) {
	q := s.db.Model(&model.Decision{})
	if f.CampaignID != 0 {
		q = q.Where("campaign_id = ?", f.CampaignID)
	}
	if f.UserID != "" {
		q = q.Where("user_id = ?", f.UserID)
	}
	if f.Status != "" {
		q = q.Where("status = ?", f.Status)
	}
	if f.Day != "" {
		q = q.Where("occurred_at >= ? AND occurred_at < ?",
			f.Day+" 00:00:00", nextDay(f.Day)+" 00:00:00")
	}
	var total int64
	if err := q.Count(&total).Error; err != nil {
		return nil, 0, err
	}
	page, size := normalizePage(f.Page, f.PageSize)
	var rows []model.Decision
	err := q.Order("id DESC").Offset((page - 1) * size).Limit(size).Find(&rows).Error
	return rows, total, err
}

type SettlementFilter struct {
	CampaignID uint64
	Day        string
	Type       string
	Page       int
	PageSize   int
}

func (s *Service) ListSettlements(_ context.Context, f SettlementFilter) ([]model.Settlement, int64, error) {
	q := s.db.Model(&model.Settlement{})
	if f.CampaignID != 0 {
		q = q.Where("campaign_id = ?", f.CampaignID)
	}
	if f.Day != "" {
		q = q.Where("day = ?", f.Day)
	}
	if f.Type != "" {
		q = q.Where("type = ?", f.Type)
	}
	var total int64
	if err := q.Count(&total).Error; err != nil {
		return nil, 0, err
	}
	page, size := normalizePage(f.Page, f.PageSize)
	var rows []model.Settlement
	err := q.Order("id DESC").Offset((page - 1) * size).Limit(size).Find(&rows).Error
	return rows, total, err
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

func lockBudgetDay(tx *gorm.DB, campaignID uint64, day string) (*model.BudgetDay, error) {
	row := model.BudgetDay{CampaignID: campaignID, Day: day}
	if err := tx.Clauses(clause.OnConflict{DoNothing: true}).Create(&row).Error; err != nil {
		return nil, err
	}
	var locked model.BudgetDay
	if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).
		Where("campaign_id = ? AND day = ?", campaignID, day).First(&locked).Error; err != nil {
		return nil, err
	}
	return &locked, nil
}

func lockUserHour(tx *gorm.DB, userID string, occurredAt time.Time) (*model.UserHour, error) {
	hour := hourOf(occurredAt)
	row := model.UserHour{UserID: userID, HourStart: hour}
	if err := tx.Clauses(clause.OnConflict{DoNothing: true}).Create(&row).Error; err != nil {
		return nil, err
	}
	var locked model.UserHour
	if err := tx.Clauses(clause.Locking{Strength: "UPDATE"}).
		Where("user_id = ? AND hour_start = ?", userID, hour).First(&locked).Error; err != nil {
		return nil, err
	}
	return &locked, nil
}

func dayOf(t time.Time) string  { return t.UTC().Format("2006-01-02") }
func hourOf(t time.Time) time.Time {
	return t.UTC().Truncate(time.Hour)
}

func nextDay(day string) string {
	t, err := time.Parse("2006-01-02", day)
	if err != nil {
		return day
	}
	return t.AddDate(0, 0, 1).Format("2006-01-02")
}

func contains(list []string, v string) bool {
	for _, x := range list {
		if x == v {
			return true
		}
	}
	return false
}

// parseHourWindow parses "HH:MM-HH:MM" into minute-of-day bounds.
func parseHourWindow(w string) (startMin, endMin int, err error) {
	parts := strings.Split(w, "-")
	if len(parts) != 2 {
		return 0, 0, fmt.Errorf("want HH:MM-HH:MM")
	}
	parse := func(s string) (int, error) {
		var h, m int
		if _, err := fmt.Sscanf(s, "%d:%d", &h, &m); err != nil {
			return 0, err
		}
		if h < 0 || h > 23 || m < 0 || m > 59 {
			return 0, fmt.Errorf("invalid time %q", s)
		}
		return h*60 + m, nil
	}
	if startMin, err = parse(parts[0]); err != nil {
		return 0, 0, err
	}
	if endMin, err = parse(parts[1]); err != nil {
		return 0, 0, err
	}
	return startMin, endMin, nil
}

// inAnyHourWindow reports whether t's UTC time-of-day falls in any window.
// Windows may wrap midnight ("22:00-02:00"); the end is exclusive.
func inAnyHourWindow(windows []string, t time.Time) bool {
	mins := t.UTC().Hour()*60 + t.UTC().Minute()
	for _, w := range windows {
		start, end, err := parseHourWindow(w)
		if err != nil {
			continue
		}
		if start <= end {
			if mins >= start && mins < end {
				return true
			}
		} else if mins >= start || mins < end {
			return true
		}
	}
	return false
}

// hashRequest hashes the canonical form of the decide request so that the
// same request_id with different content is detected as a conflict.
func hashRequest(req DecideRequest) string {
	canonical := fmt.Sprintf("%s|%d|%s|%s|%s|%s|%d",
		req.RequestID, req.CreativeID, req.UserID, req.Region, req.Device,
		req.OccurredAt.UTC().Format("2006-01-02T15:04:05.000Z"), req.CostCents)
	sum := sha256.Sum256([]byte(canonical))
	return hex.EncodeToString(sum[:])
}

func isDuplicateKey(err error) bool {
	return err != nil && strings.Contains(err.Error(), "Duplicate entry")
}

func normalizePage(page, size int) (int, int) {
	if page < 1 {
		page = 1
	}
	if size < 1 || size > 200 {
		size = 50
	}
	return page, size
}
