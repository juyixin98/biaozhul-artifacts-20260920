package service_test

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"gorm.io/gorm"

	"targetcraft/internal/model"
	"targetcraft/internal/service"
	"targetcraft/internal/testsupport"
)

// manualClock is a test clock the test can advance at will.
type manualClock struct {
	mu sync.Mutex
	t  time.Time
}

func newManualClock(t time.Time) *manualClock { return &manualClock{t: t} }

func (c *manualClock) now() time.Time {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.t
}

func (c *manualClock) advance(d time.Duration) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.t = c.t.Add(d)
}

var baseTime = time.Date(2026, 9, 20, 12, 0, 0, 0, time.UTC)

var reqSeq int64

func newService(t *testing.T) (*service.Service, *manualClock, *gorm.DB) {
	t.Helper()
	gdb := testsupport.OpenTestDB(t)
	clock := newManualClock(baseTime)
	svc := service.New(gdb)
	svc.SetClock(clock.now)
	return svc, clock, gdb
}

func mkCampaign(t *testing.T, svc *service.Service, mutate func(*service.CreateCampaignRequest)) *model.Campaign {
	t.Helper()
	req := service.CreateCampaignRequest{
		Name:             "test-campaign",
		StartAt:          baseTime.Add(-time.Hour),
		EndAt:            baseTime.Add(24 * time.Hour),
		TotalBudgetCents: 1_000_000,
		DailyCapCents:    1_000_000,
	}
	if mutate != nil {
		mutate(&req)
	}
	c, err := svc.CreateCampaign(context.Background(), req)
	if err != nil {
		t.Fatalf("create campaign: %v", err)
	}
	return c
}

func mkCreative(t *testing.T, svc *service.Service, campaignID uint64) *model.Creative {
	t.Helper()
	c, err := svc.CreateCreative(context.Background(), service.CreateCreativeRequest{
		CampaignID: campaignID,
		Name:       "test-creative",
	})
	if err != nil {
		t.Fatalf("create creative: %v", err)
	}
	return c
}

func decideReq(creativeID uint64, userID string, cost int64) service.DecideRequest {
	return service.DecideRequest{
		RequestID:  fmt.Sprintf("req-%d", atomic.AddInt64(&reqSeq, 1)),
		CreativeID: creativeID,
		UserID:     userID,
		Region:     "CN",
		Device:     "ios",
		OccurredAt: baseTime,
		CostCents:  cost,
	}
}

func mustDecide(t *testing.T, svc *service.Service, req service.DecideRequest) *service.DecideResponse {
	t.Helper()
	resp, err := svc.Decide(context.Background(), req)
	if err != nil {
		t.Fatalf("decide %s: %v", req.RequestID, err)
	}
	return resp
}

func getCampaign(t *testing.T, gdb *gorm.DB, id uint64) model.Campaign {
	t.Helper()
	var c model.Campaign
	if err := gdb.First(&c, id).Error; err != nil {
		t.Fatalf("load campaign: %v", err)
	}
	return c
}

func getBudgetDay(t *testing.T, gdb *gorm.DB, campaignID uint64, day string) model.BudgetDay {
	t.Helper()
	var bd model.BudgetDay
	if err := gdb.Where("campaign_id = ? AND day = ?", campaignID, day).First(&bd).Error; err != nil {
		t.Fatalf("load budget day: %v", err)
	}
	return bd
}

func getUserHour(t *testing.T, gdb *gorm.DB, userID string, at time.Time) model.UserHour {
	t.Helper()
	var uh model.UserHour
	if err := gdb.Where("user_id = ? AND hour_start = ?", userID, at.UTC().Truncate(time.Hour)).First(&uh).Error; err != nil {
		t.Fatalf("load user hour: %v", err)
	}
	return uh
}

func countSettlements(t *testing.T, gdb *gorm.DB, requestID, typ string) int64 {
	t.Helper()
	var n int64
	q := gdb.Model(&model.Settlement{}).Where("request_id = ?", requestID)
	if typ != "" {
		q = q.Where("type = ?", typ)
	}
	if err := q.Count(&n).Error; err != nil {
		t.Fatalf("count settlements: %v", err)
	}
	return n
}

func getDecision(t *testing.T, gdb *gorm.DB, requestID string) model.Decision {
	t.Helper()
	var d model.Decision
	if err := gdb.Where("request_id = ?", requestID).First(&d).Error; err != nil {
		t.Fatalf("load decision: %v", err)
	}
	return d
}

// --- 并发预算：不超支 -------------------------------------------------------

func TestConcurrentBudgetNoOverspend(t *testing.T) {
	svc, _, gdb := newService(t)
	camp := mkCampaign(t, svc, func(r *service.CreateCampaignRequest) {
		r.TotalBudgetCents = 1000
	})
	cr := mkCreative(t, svc, camp.ID)

	const workers = 50
	results := make(chan *service.DecideResponse, workers)
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			resp, err := svc.Decide(context.Background(), decideReq(cr.ID, fmt.Sprintf("user-%d", i), 100))
			if err != nil {
				t.Errorf("decide: %v", err)
				return
			}
			results <- resp
		}(i)
	}
	wg.Wait()
	close(results)

	accepted := 0
	for resp := range results {
		if resp.Accepted {
			accepted++
		} else if resp.RejectCode != "TOTAL_BUDGET_EXHAUSTED" {
			t.Errorf("unexpected reject code %q", resp.RejectCode)
		}
	}
	if accepted != 10 {
		t.Fatalf("accepted = %d, want exactly 10 (budget 1000 / cost 100)", accepted)
	}
	c := getCampaign(t, gdb, camp.ID)
	if c.TotalReservedCents != 1000 || c.TotalSpentCents != 0 {
		t.Fatalf("campaign counters reserved=%d spent=%d, want 1000/0", c.TotalReservedCents, c.TotalSpentCents)
	}
	bd := getBudgetDay(t, gdb, camp.ID, "2026-09-20")
	if bd.ReservedCents != 1000 || bd.ReservedCount != 10 {
		t.Fatalf("budget day reserved=%d count=%d, want 1000/10", bd.ReservedCents, bd.ReservedCount)
	}
}

// --- 频控边界：每用户每小时 3 次、每活动每天 20 次 ----------------------------

func TestUserHourlyCapBoundary(t *testing.T) {
	svc, clock, _ := newService(t)
	camp := mkCampaign(t, svc, nil)
	cr := mkCreative(t, svc, camp.ID)

	// Sequential: 3 pass, 4th rejected.
	for i := 0; i < 3; i++ {
		if resp := mustDecide(t, svc, decideReq(cr.ID, "user-a", 10)); !resp.Accepted {
			t.Fatalf("decide %d rejected: %s", i, resp.RejectCode)
		}
	}
	resp := mustDecide(t, svc, decideReq(cr.ID, "user-a", 10))
	if resp.Accepted || resp.RejectCode != "USER_HOURLY_CAP_REACHED" {
		t.Fatalf("4th decide = %+v, want USER_HOURLY_CAP_REACHED", resp)
	}

	// Next UTC hour frees the slot.
	clock.advance(time.Hour)
	req := decideReq(cr.ID, "user-a", 10)
	req.OccurredAt = clock.now()
	if resp := mustDecide(t, svc, req); !resp.Accepted {
		t.Fatalf("decide after hour rollover rejected: %s", resp.RejectCode)
	}
}

func TestUserHourlyCapConcurrent(t *testing.T) {
	svc, _, _ := newService(t)
	camp := mkCampaign(t, svc, nil)
	cr := mkCreative(t, svc, camp.ID)

	const workers = 10
	var accepted int64
	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			resp, err := svc.Decide(context.Background(), decideReq(cr.ID, "hot-user", 10))
			if err != nil {
				t.Errorf("decide: %v", err)
				return
			}
			if resp.Accepted {
				atomic.AddInt64(&accepted, 1)
			}
		}()
	}
	wg.Wait()
	if accepted != service.MaxUserImpressionsPerHour {
		t.Fatalf("accepted = %d, want %d", accepted, service.MaxUserImpressionsPerHour)
	}
}

func TestCampaignDailyImpressionCap(t *testing.T) {
	svc, _, _ := newService(t)
	camp := mkCampaign(t, svc, nil)
	cr := mkCreative(t, svc, camp.ID)

	for i := 0; i < service.MaxCampaignImpressionsPerDay; i++ {
		resp := mustDecide(t, svc, decideReq(cr.ID, fmt.Sprintf("user-%d", i), 10))
		if !resp.Accepted {
			t.Fatalf("decide %d rejected: %s", i, resp.RejectCode)
		}
	}
	resp := mustDecide(t, svc, decideReq(cr.ID, "user-overflow", 10))
	if resp.Accepted || resp.RejectCode != "CAMPAIGN_DAILY_CAP_REACHED" {
		t.Fatalf("21st decide = %+v, want CAMPAIGN_DAILY_CAP_REACHED", resp)
	}
}

func TestDailyCapCents(t *testing.T) {
	svc, _, _ := newService(t)
	camp := mkCampaign(t, svc, func(r *service.CreateCampaignRequest) {
		r.DailyCapCents = 100
	})
	cr := mkCreative(t, svc, camp.ID)

	if resp := mustDecide(t, svc, decideReq(cr.ID, "u1", 60)); !resp.Accepted {
		t.Fatalf("first decide rejected: %s", resp.RejectCode)
	}
	resp := mustDecide(t, svc, decideReq(cr.ID, "u2", 60))
	if resp.Accepted || resp.RejectCode != "DAILY_CAP_EXCEEDED" {
		t.Fatalf("second decide = %+v, want DAILY_CAP_EXCEEDED", resp)
	}
}

// --- 幂等：重放返回原结果，同 ID 不同内容报冲突 ------------------------------

func TestIdempotentReplayAndConflict(t *testing.T) {
	svc, _, _ := newService(t)
	camp := mkCampaign(t, svc, nil)
	cr := mkCreative(t, svc, camp.ID)

	req := decideReq(cr.ID, "user-a", 100)
	first := mustDecide(t, svc, req)
	if !first.Accepted {
		t.Fatalf("first decide rejected: %s", first.RejectCode)
	}
	second := mustDecide(t, svc, req)
	j1, _ := json.Marshal(first)
	j2, _ := json.Marshal(second)
	if string(j1) != string(j2) {
		t.Fatalf("replay differs:\nfirst:  %s\nsecond: %s", j1, j2)
	}

	conflict := req
	conflict.CostCents = 200
	_, err := svc.Decide(context.Background(), conflict)
	var apiErr *service.APIError
	if !errors.As(err, &apiErr) || apiErr.Status != 409 || apiErr.Code != "REQUEST_ID_CONFLICT" {
		t.Fatalf("conflicting replay err = %v, want 409 REQUEST_ID_CONFLICT", err)
	}

	// Rejections are stored and replayed identically too.
	badReq := decideReq(cr.ID, "user-a", 100)
	badReq.Region = "NOWHERE"
	camp2 := mkCampaign(t, svc, func(r *service.CreateCampaignRequest) { r.Regions = []string{"CN"} })
	cr2 := mkCreative(t, svc, camp2.ID)
	badReq.CreativeID = cr2.ID
	r1 := mustDecide(t, svc, badReq)
	r2 := mustDecide(t, svc, badReq)
	if r1.Accepted || r1.RejectCode != "REGION_NOT_MATCHED" {
		t.Fatalf("bad region decide = %+v", r1)
	}
	j1, _ = json.Marshal(r1)
	j2, _ = json.Marshal(r2)
	if string(j1) != string(j2) {
		t.Fatalf("rejection replay differs:\nfirst:  %s\nsecond: %s", j1, j2)
	}
}

// --- 重复确认：不双扣 -------------------------------------------------------

func TestDuplicateConfirm(t *testing.T) {
	svc, _, gdb := newService(t)
	camp := mkCampaign(t, svc, nil)
	cr := mkCreative(t, svc, camp.ID)

	req := decideReq(cr.ID, "user-a", 100)
	mustDecide(t, svc, req)

	c1, err := svc.Confirm(context.Background(), req.RequestID)
	if err != nil {
		t.Fatalf("confirm: %v", err)
	}
	c2, err := svc.Confirm(context.Background(), req.RequestID)
	if err != nil {
		t.Fatalf("duplicate confirm: %v", err)
	}
	if !c1.ConfirmedAt.Equal(c2.ConfirmedAt) {
		t.Fatalf("confirmed_at changed between confirms: %v vs %v", c1.ConfirmedAt, c2.ConfirmedAt)
	}

	c := getCampaign(t, gdb, camp.ID)
	if c.TotalSpentCents != 100 || c.TotalReservedCents != 0 {
		t.Fatalf("campaign spent=%d reserved=%d, want 100/0 (charged exactly once)",
			c.TotalSpentCents, c.TotalReservedCents)
	}
	if n := countSettlements(t, gdb, req.RequestID, model.SettlementTypeConfirm); n != 1 {
		t.Fatalf("confirm settlements = %d, want 1", n)
	}
	uh := getUserHour(t, gdb, "user-a", baseTime)
	if uh.ConfirmedCount != 1 || uh.ReservedCount != 0 {
		t.Fatalf("user hour confirmed=%d reserved=%d, want 1/0", uh.ConfirmedCount, uh.ReservedCount)
	}
}

// --- 到期释放与过期确认 -----------------------------------------------------

func TestExpiryReleaseAndLateConfirm(t *testing.T) {
	svc, clock, gdb := newService(t)
	camp := mkCampaign(t, svc, nil)
	cr := mkCreative(t, svc, camp.ID)

	req := decideReq(cr.ID, "user-a", 100)
	mustDecide(t, svc, req)

	clock.advance(6 * time.Minute) // past the 5-minute TTL
	released, err := svc.SweepExpired(context.Background())
	if err != nil {
		t.Fatalf("sweep: %v", err)
	}
	if released != 1 {
		t.Fatalf("sweep released %d, want 1", released)
	}

	c := getCampaign(t, gdb, camp.ID)
	if c.TotalReservedCents != 0 {
		t.Fatalf("reserved after release = %d, want 0", c.TotalReservedCents)
	}
	bd := getBudgetDay(t, gdb, camp.ID, "2026-09-20")
	if bd.ReservedCents != 0 || bd.ReservedCount != 0 {
		t.Fatalf("budget day reserved=%d count=%d after release, want 0/0", bd.ReservedCents, bd.ReservedCount)
	}

	// Late confirm is explicitly rejected.
	_, err = svc.Confirm(context.Background(), req.RequestID)
	var apiErr *service.APIError
	if !errors.As(err, &apiErr) || apiErr.Status != 410 {
		t.Fatalf("late confirm err = %v, want 410", err)
	}

	// A second sweep releases nothing and nothing is double-counted.
	released, err = svc.SweepExpired(context.Background())
	if err != nil {
		t.Fatalf("second sweep: %v", err)
	}
	if released != 0 {
		t.Fatalf("second sweep released %d, want 0", released)
	}
	if n := countSettlements(t, gdb, req.RequestID, model.SettlementTypeRelease); n != 1 {
		t.Fatalf("release settlements = %d, want 1", n)
	}
}

// --- 跨日预占：预算记在发生时间所在的 UTC 日 ---------------------------------

func TestCrossDayReservation(t *testing.T) {
	svc, clock, gdb := newService(t)
	// Move to 23:58 so the reservation expires after midnight.
	clock.advance(11*time.Hour + 58*time.Minute) // 2026-09-20 23:58
	camp := mkCampaign(t, svc, func(r *service.CreateCampaignRequest) {
		r.StartAt = baseTime
		r.EndAt = baseTime.Add(48 * time.Hour)
	})
	cr := mkCreative(t, svc, camp.ID)

	req := decideReq(cr.ID, "user-a", 100)
	req.OccurredAt = clock.now()
	resp := mustDecide(t, svc, req)
	if !resp.Accepted || resp.Day != "2026-09-20" {
		t.Fatalf("decide = %+v, want accepted on day 2026-09-20", resp)
	}

	clock.advance(3 * time.Minute) // 2026-09-21 00:01, still within TTL
	conf, err := svc.Confirm(context.Background(), req.RequestID)
	if err != nil {
		t.Fatalf("cross-midnight confirm: %v", err)
	}
	if conf.Day != "2026-09-20" {
		t.Fatalf("confirm day = %s, want 2026-09-20 (the decision day)", conf.Day)
	}

	// Consumption lands on the decision's UTC day, not the confirm day.
	bd := getBudgetDay(t, gdb, camp.ID, "2026-09-20")
	if bd.SpentCents != 100 || bd.ReservedCents != 0 {
		t.Fatalf("day 2026-09-20 spent=%d reserved=%d, want 100/0", bd.SpentCents, bd.ReservedCents)
	}
	var n int64
	if err := gdb.Model(&model.BudgetDay{}).Where("campaign_id = ? AND day = ?", camp.ID, "2026-09-21").
		Count(&n).Error; err != nil {
		t.Fatalf("count day rows: %v", err)
	}
	if n != 0 {
		t.Fatalf("unexpected budget day row for 2026-09-21")
	}
	if n := countSettlements(t, gdb, req.RequestID, model.SettlementTypeConfirm); n != 1 {
		t.Fatalf("confirm settlements = %d, want 1", n)
	}
}

// --- 重启恢复：新进程实例凭数据库即可确认/释放 -------------------------------

func TestRecoveryAfterRestart(t *testing.T) {
	svc, clock, gdb := newService(t)
	camp := mkCampaign(t, svc, nil)
	cr := mkCreative(t, svc, camp.ID)

	validReq := decideReq(cr.ID, "user-a", 100)
	mustDecide(t, svc, validReq)
	expiringReq := decideReq(cr.ID, "user-b", 100)
	mustDecide(t, svc, expiringReq)

	// Simulate a process restart: a brand-new Service over the same DB.
	clock.advance(6 * time.Minute) // expiringReq is now stale; validReq too...
	_ = validReq

	// validReq was created at the same instant, so make a fresh one that is
	// still inside its TTL for the "confirm still works" half of the test.
	freshReq := decideReq(cr.ID, "user-c", 100)
	freshReq.OccurredAt = clock.now()
	mustDecide(t, svc, freshReq)

	restarted := service.New(gdb)
	restarted.SetClock(clock.now)

	// Recovery sweep releases only the expired reservations.
	released, err := restarted.SweepExpired(context.Background())
	if err != nil {
		t.Fatalf("recovery sweep: %v", err)
	}
	if released != 2 {
		t.Fatalf("recovery sweep released %d, want 2", released)
	}

	// The still-valid reservation confirms normally after the "restart".
	if _, err := restarted.Confirm(context.Background(), freshReq.RequestID); err != nil {
		t.Fatalf("confirm after restart: %v", err)
	}
	// The expired ones are rejected.
	for _, id := range []string{validReq.RequestID, expiringReq.RequestID} {
		_, err := restarted.Confirm(context.Background(), id)
		var apiErr *service.APIError
		if !errors.As(err, &apiErr) || apiErr.Status != 410 {
			t.Fatalf("confirm %s after expiry err = %v, want 410", id, err)
		}
	}

	c := getCampaign(t, gdb, camp.ID)
	if c.TotalSpentCents != 100 || c.TotalReservedCents != 0 {
		t.Fatalf("campaign spent=%d reserved=%d, want 100/0", c.TotalSpentCents, c.TotalReservedCents)
	}
}

// --- 确认与到期竞争：只能成功一个 -------------------------------------------

func TestConfirmVsSweepRace(t *testing.T) {
	svc, clock, gdb := newService(t)

	for i := 0; i < 30; i++ {
		camp := mkCampaign(t, svc, nil)
		cr := mkCreative(t, svc, camp.ID)
		req := decideReq(cr.ID, fmt.Sprintf("race-user-%d", i), 100)
		mustDecide(t, svc, req)

		clock.advance(6 * time.Minute) // reservation expired

		var wg sync.WaitGroup
		var confirmErr error
		wg.Add(2)
		go func() {
			defer wg.Done()
			_, confirmErr = svc.Confirm(context.Background(), req.RequestID)
		}()
		go func() {
			defer wg.Done()
			_, _ = svc.SweepExpired(context.Background())
		}()
		wg.Wait()

		// Expired confirm must be rejected (it releases and 410s).
		var apiErr *service.APIError
		if !errors.As(confirmErr, &apiErr) || apiErr.Status != 410 {
			t.Fatalf("iter %d: confirm err = %v, want 410", i, confirmErr)
		}
		d := getDecision(t, gdb, req.RequestID)
		if d.Status != model.DecisionStatusReleased {
			t.Fatalf("iter %d: status = %s, want released", i, d.Status)
		}
		if n := countSettlements(t, gdb, req.RequestID, ""); n != 1 {
			t.Fatalf("iter %d: settlements = %d, want exactly 1", i, n)
		}
		bd := getBudgetDay(t, gdb, camp.ID, "2026-09-20")
		if bd.ReservedCents != 0 || bd.ReservedCount != 0 || bd.SpentCents != 0 {
			t.Fatalf("iter %d: counters after race = %+v, want all zero", i, bd)
		}
	}
}

// --- 暂停活动：立即阻止新决策，已预占仍可确认 ---------------------------------

func TestPauseBlocksNewDecisions(t *testing.T) {
	svc, _, _ := newService(t)
	camp := mkCampaign(t, svc, nil)
	cr := mkCreative(t, svc, camp.ID)

	req := decideReq(cr.ID, "user-a", 100)
	mustDecide(t, svc, req)

	if _, err := svc.SetCampaignStatus(context.Background(), camp.ID, model.CampaignStatusPaused); err != nil {
		t.Fatalf("pause: %v", err)
	}

	resp := mustDecide(t, svc, decideReq(cr.ID, "user-b", 100))
	if resp.Accepted || resp.RejectCode != "CAMPAIGN_NOT_ACTIVE" {
		t.Fatalf("decide on paused campaign = %+v, want CAMPAIGN_NOT_ACTIVE", resp)
	}

	// The reservation made before the pause still confirms within its TTL.
	if _, err := svc.Confirm(context.Background(), req.RequestID); err != nil {
		t.Fatalf("confirm pre-pause reservation: %v", err)
	}
}

// --- 定向规则 ---------------------------------------------------------------

func TestTargetingRules(t *testing.T) {
	svc, clock, _ := newService(t)
	camp := mkCampaign(t, svc, func(r *service.CreateCampaignRequest) {
		r.Regions = []string{"CN", "JP"}
		r.Devices = []string{"ios"}
		r.HourWindows = []string{"08:00-20:00"}
	})
	cr := mkCreative(t, svc, camp.ID)

	cases := []struct {
		name   string
		mutate func(*service.DecideRequest)
		code   string
	}{
		{"region not matched", func(r *service.DecideRequest) { r.Region = "US" }, "REGION_NOT_MATCHED"},
		{"device not matched", func(r *service.DecideRequest) { r.Device = "android" }, "DEVICE_NOT_MATCHED"},
		{"outside hour window", func(r *service.DecideRequest) {
			r.OccurredAt = time.Date(2026, 9, 20, 21, 0, 0, 0, time.UTC)
		}, "TIME_WINDOW_NOT_MATCHED"},
		{"before start", func(r *service.DecideRequest) {
			r.OccurredAt = baseTime.Add(-2 * time.Hour)
		}, "CAMPAIGN_NOT_STARTED"},
		{"after end", func(r *service.DecideRequest) {
			r.OccurredAt = baseTime.Add(25 * time.Hour)
		}, "CAMPAIGN_ENDED"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := decideReq(cr.ID, "user-a", 10)
			tc.mutate(&req)
			resp := mustDecide(t, svc, req)
			if resp.Accepted || resp.RejectCode != tc.code {
				t.Fatalf("decide = %+v, want reject %s", resp, tc.code)
			}
		})
	}

	// A matching request passes; hour window wraps correctly at 12:00 UTC.
	_ = clock
	req := decideReq(cr.ID, "user-a", 10)
	if resp := mustDecide(t, svc, req); !resp.Accepted {
		t.Fatalf("matching decide rejected: %s", resp.RejectCode)
	}
}

func TestCreativeAndCampaignNotFound(t *testing.T) {
	svc, _, _ := newService(t)
	resp := mustDecide(t, svc, decideReq(999999, "user-a", 10))
	if resp.Accepted || resp.RejectCode != "CREATIVE_NOT_FOUND" {
		t.Fatalf("decide = %+v, want CREATIVE_NOT_FOUND", resp)
	}
}
