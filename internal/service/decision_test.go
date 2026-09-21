package service

import (
	"context"
	"errors"
	"fmt"
	"os"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"gorm.io/driver/mysql"
	"gorm.io/gorm"
	gormlogger "gorm.io/gorm/logger"

	"targetcraft/internal/models"
	"targetcraft/internal/store"
)

var testDB *gorm.DB

func TestMain(m *testing.M) {
	dsn := os.Getenv("TARGETCRAFT_TEST_DSN")
	if dsn == "" {
		dsn = "root@tcp(127.0.0.1:3306)/?charset=utf8mb4&parseTime=true&loc=UTC"
	}
	admin, err := gorm.Open(mysql.Open(dsn), &gorm.Config{Logger: gormlogger.Default.LogMode(gormlogger.Silent)})
	if err != nil {
		fmt.Fprintf(os.Stderr, "SKIP: cannot connect to MySQL for tests: %v\n", err)
		os.Exit(0)
	}
	dbName := "targetcraft_test"
	if err := admin.Exec("DROP DATABASE IF EXISTS " + dbName).Error; err != nil {
		fmt.Fprintf(os.Stderr, "drop test db: %v\n", err)
		os.Exit(1)
	}
	if err := admin.Exec("CREATE DATABASE " + dbName + " CHARACTER SET utf8mb4").Error; err != nil {
		fmt.Fprintf(os.Stderr, "create test db: %v\n", err)
		os.Exit(1)
	}

	testDSN := os.Getenv("TARGETCRAFT_TEST_DSN")
	if testDSN == "" {
		testDSN = "root@tcp(127.0.0.1:3306)/" + dbName + "?charset=utf8mb4&parseTime=true&loc=UTC"
	}
	testDB, err = store.Open(testDSN)
	if err != nil {
		fmt.Fprintf(os.Stderr, "open test db: %v\n", err)
		os.Exit(1)
	}
	if err := store.Migrate(testDB); err != nil {
		fmt.Fprintf(os.Stderr, "migrate test db: %v\n", err)
		os.Exit(1)
	}

	code := m.Run()
	_ = admin.Exec("DROP DATABASE IF EXISTS " + dbName).Error
	os.Exit(code)
}

// newFixture creates a fresh active campaign with one active creative.
func newFixture(t *testing.T, totalBudget, dailyCap int64) (models.Campaign, models.Creative) {
	t.Helper()
	now := time.Now().UTC()
	campaign := models.Campaign{
		Name:        fmt.Sprintf("camp-%d", time.Now().UnixNano()),
		Status:      models.CampaignStatusActive,
		StartAt:     now.Add(-time.Hour),
		EndAt:       now.Add(24 * time.Hour),
		TotalBudget: totalBudget,
		DailyCap:    dailyCap,
		Rules:       models.Rules{}.Marshal(), // match everything
	}
	if err := testDB.Create(&campaign).Error; err != nil {
		t.Fatal(err)
	}
	creative := models.Creative{
		CampaignID: campaign.ID,
		Name:       "creative-1",
		Status:     models.CreativeStatusActive,
	}
	if err := testDB.Create(&creative).Error; err != nil {
		t.Fatal(err)
	}
	return campaign, creative
}

func newService(ttl time.Duration) *Service {
	return New(testDB, ttl)
}

func req(id, user string, campaignID uint64, cost int64) DecideRequest {
	return DecideRequest{
		RequestID:  id,
		UserID:     user,
		CampaignID: campaignID,
		Region:     "CN",
		Device:     "ios",
		OccurredAt: time.Now().UTC(),
		Cost:       cost,
	}
}

func mustApprove(t *testing.T, svc *Service, r DecideRequest) {
	t.Helper()
	res, err := svc.Decide(context.Background(), r)
	if err != nil {
		t.Fatalf("decide %s: %v", r.RequestID, err)
	}
	if !res.Approved {
		t.Fatalf("decide %s: expected approved, got rejected: %s", r.RequestID, res.Reason)
	}
}

func campaignState(t *testing.T, id uint64) models.Campaign {
	t.Helper()
	var c models.Campaign
	if err := testDB.First(&c, id).Error; err != nil {
		t.Fatal(err)
	}
	return c
}

// ---- concurrency: budget must never be exceeded ----

func TestConcurrentBudgetNeverExceeded(t *testing.T) {
	campaign, _ := newFixture(t, 1000, 0) // 1000 cents total, cost 100 each -> exactly 10
	svc := newService(5 * time.Minute)

	const goroutines = 30
	var approved int64
	var wg sync.WaitGroup
	for i := 0; i < goroutines; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			res, err := svc.Decide(context.Background(), req(
				fmt.Sprintf("conc-%d", i), fmt.Sprintf("user-%d", i), campaign.ID, 100))
			if err != nil {
				t.Errorf("decide: %v", err)
				return
			}
			if res.Approved {
				atomic.AddInt64(&approved, 1)
			} else if res.Reason != ReasonTotalBudget {
				t.Errorf("unexpected reject reason: %s", res.Reason)
			}
		}(i)
	}
	wg.Wait()

	if approved != 10 {
		t.Fatalf("expected exactly 10 approvals, got %d", approved)
	}
	c := campaignState(t, campaign.ID)
	if c.ReservedTotal != 1000 || c.SpentTotal != 0 {
		t.Fatalf("budget ledgers wrong: reserved=%d spent=%d", c.ReservedTotal, c.SpentTotal)
	}
}

// ---- frequency caps ----

func TestUserHourlyFrequencyBoundary(t *testing.T) {
	campaign, _ := newFixture(t, 1_000_000, 0)
	svc := newService(5 * time.Minute)
	now := time.Now().UTC()

	// 3 requests in the same UTC hour pass, the 4th is rejected.
	for i := 0; i < MaxUserHourlyImpressions; i++ {
		r := req(fmt.Sprintf("freq-%d", i), "user-freq", campaign.ID, 10)
		r.OccurredAt = now
		mustApprove(t, svc, r)
	}
	r := req("freq-3", "user-freq", campaign.ID, 10)
	r.OccurredAt = now
	res, err := svc.Decide(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if res.Approved || res.Reason != ReasonUserHourlyFreq {
		t.Fatalf("4th request in same hour: approved=%v reason=%s", res.Approved, res.Reason)
	}

	// Same user, next UTC hour: allowed again.
	r = req("freq-next-hour", "user-freq", campaign.ID, 10)
	r.OccurredAt = now.Add(time.Hour).Truncate(time.Hour)
	mustApprove(t, svc, r)
}

func TestCampaignDailyFrequencyBoundary(t *testing.T) {
	campaign, _ := newFixture(t, 1_000_000, 0)
	svc := newService(5 * time.Minute)

	for i := 0; i < MaxCampaignDailyImpressions; i++ {
		mustApprove(t, svc, req(fmt.Sprintf("cfreq-%d", i), fmt.Sprintf("user-%d", i), campaign.ID, 10))
	}
	res, err := svc.Decide(context.Background(), req("cfreq-20", "user-20", campaign.ID, 10))
	if err != nil {
		t.Fatal(err)
	}
	if res.Approved || res.Reason != ReasonCampaignDailyFreq {
		t.Fatalf("21st impression: approved=%v reason=%s", res.Approved, res.Reason)
	}
}

func TestDailyBudgetCap(t *testing.T) {
	campaign, _ := newFixture(t, 1_000_000, 250) // daily cap 250, cost 100 -> 2 pass
	svc := newService(5 * time.Minute)

	mustApprove(t, svc, req("daily-0", "u0", campaign.ID, 100))
	mustApprove(t, svc, req("daily-1", "u1", campaign.ID, 100))
	res, err := svc.Decide(context.Background(), req("daily-2", "u2", campaign.ID, 100))
	if err != nil {
		t.Fatal(err)
	}
	if res.Approved || res.Reason != ReasonDailyBudget {
		t.Fatalf("expected daily budget rejection, got approved=%v reason=%s", res.Approved, res.Reason)
	}
}

// ---- idempotency ----

func TestIdempotentReplayAndConflict(t *testing.T) {
	campaign, _ := newFixture(t, 1_000_000, 0)
	svc := newService(5 * time.Minute)

	r := req("idem-1", "user-idem", campaign.ID, 100)
	first, err := svc.Decide(context.Background(), r)
	if err != nil || !first.Approved {
		t.Fatalf("first decide: %v approved=%v", err, first.Approved)
	}

	// Same ID, same payload: original result returned, counters untouched.
	second, err := svc.Decide(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if !second.Approved || second.Decision.ID != first.Decision.ID {
		t.Fatalf("replay mismatch: id %d vs %d", second.Decision.ID, first.Decision.ID)
	}
	if c := campaignState(t, campaign.ID); c.ReservedTotal != 100 {
		t.Fatalf("replay double-reserved budget: reserved=%d", c.ReservedTotal)
	}

	// Same ID, different payload: conflict.
	r2 := r
	r2.Cost = 200
	_, err = svc.Decide(context.Background(), r2)
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("expected conflict, got %v", err)
	}
}

// ---- confirm ----

func TestConfirmMovesReservedToSpent(t *testing.T) {
	campaign, _ := newFixture(t, 1_000_000, 0)
	svc := newService(5 * time.Minute)

	mustApprove(t, svc, req("conf-1", "user-conf", campaign.ID, 100))
	d, err := svc.Confirm(context.Background(), "conf-1")
	if err != nil {
		t.Fatal(err)
	}
	if d.Status != models.DecisionStatusConfirmed {
		t.Fatalf("status=%s", d.Status)
	}
	c := campaignState(t, campaign.ID)
	if c.ReservedTotal != 0 || c.SpentTotal != 100 {
		t.Fatalf("reserved=%d spent=%d", c.ReservedTotal, c.SpentTotal)
	}
}

func TestDuplicateConfirmDoesNotDoubleSpend(t *testing.T) {
	campaign, _ := newFixture(t, 1_000_000, 0)
	svc := newService(5 * time.Minute)

	mustApprove(t, svc, req("dup-1", "user-dup", campaign.ID, 100))
	if _, err := svc.Confirm(context.Background(), "dup-1"); err != nil {
		t.Fatal(err)
	}
	// Second confirm: idempotent success, spent stays 100.
	if _, err := svc.Confirm(context.Background(), "dup-1"); err != nil {
		t.Fatalf("duplicate confirm should succeed idempotently: %v", err)
	}
	// Concurrent duplicate confirms: exactly one spends.
	var wg sync.WaitGroup
	for i := 0; i < 10; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, _ = svc.Confirm(context.Background(), "dup-1")
		}()
	}
	wg.Wait()
	if c := campaignState(t, campaign.ID); c.SpentTotal != 100 || c.ReservedTotal != 0 {
		t.Fatalf("double spend: reserved=%d spent=%d", c.ReservedTotal, c.SpentTotal)
	}
}

// ---- expiry & release ----

func TestExpiryReleaseRefundsBudgetAndFreq(t *testing.T) {
	campaign, _ := newFixture(t, 1_000_000, 0)
	svc := newService(5 * time.Minute)

	// Freeze the clock so we can control expiry deterministically.
	now := time.Now().UTC()
	svc.SetClock(func() time.Time { return now })

	mustApprove(t, svc, req("exp-1", "user-exp", campaign.ID, 100))

	// Advance past the TTL and sweep. Other tests may have left expired
	// reservations behind, so assert on this decision specifically.
	svc.SetClock(func() time.Time { return now.Add(6 * time.Minute) })
	if _, err := svc.SweepExpired(context.Background()); err != nil {
		t.Fatal(err)
	}

	var d models.Decision
	if err := testDB.Where("request_id = ?", "exp-1").First(&d).Error; err != nil {
		t.Fatal(err)
	}
	if d.Status != models.DecisionStatusReleased {
		t.Fatalf("expected released, got %s", d.Status)
	}

	c := campaignState(t, campaign.ID)
	if c.ReservedTotal != 0 || c.SpentTotal != 0 {
		t.Fatalf("budget not refunded: reserved=%d spent=%d", c.ReservedTotal, c.SpentTotal)
	}
	var counter models.FreqCounter
	err := testDB.Where("scope = ? AND `key` = ?", models.ScopeUserHour, userHourKey("user-exp", now)).First(&counter).Error
	if err != nil {
		t.Fatal(err)
	}
	if counter.Count != 0 {
		t.Fatalf("frequency not refunded: count=%d", counter.Count)
	}

	// Confirming after expiry is explicitly rejected.
	if _, err := svc.Confirm(context.Background(), "exp-1"); !errors.Is(err, ErrReleased) {
		t.Fatalf("expected ErrReleased, got %v", err)
	}
}

func TestExpiredConfirmRejectedBeforeSweep(t *testing.T) {
	campaign, _ := newFixture(t, 1_000_000, 0)
	svc := newService(5 * time.Minute)

	now := time.Now().UTC()
	svc.SetClock(func() time.Time { return now })
	mustApprove(t, svc, req("exp-2", "user-exp2", campaign.ID, 100))

	// Clock past TTL, sweeper has NOT run yet: confirm must still fail.
	svc.SetClock(func() time.Time { return now.Add(6 * time.Minute) })
	if _, err := svc.Confirm(context.Background(), "exp-2"); !errors.Is(err, ErrExpired) {
		t.Fatalf("expected ErrExpired, got %v", err)
	}
}

func TestConfirmVsReleaseRaceExactlyOneWins(t *testing.T) {
	for i := 0; i < 20; i++ {
		campaign, _ := newFixture(t, 1_000_000, 0)
		svc := newService(5 * time.Minute)
		now := time.Now().UTC()
		svc.SetClock(func() time.Time { return now })

		rid := fmt.Sprintf("race-%d", i)
		mustApprove(t, svc, req(rid, fmt.Sprintf("user-race-%d", i), campaign.ID, 100))

		// Move clock exactly to the expiry boundary.
		svc.SetClock(func() time.Time { return now.Add(5 * time.Minute) })

		var wg sync.WaitGroup
		var confirmErr error
		wg.Add(2)
		go func() { defer wg.Done(); _, confirmErr = svc.Confirm(context.Background(), rid) }()
		go func() { defer wg.Done(); _, _ = svc.SweepExpired(context.Background()) }()
		wg.Wait()

		var d models.Decision
		if err := testDB.Where("request_id = ?", rid).First(&d).Error; err != nil {
			t.Fatal(err)
		}
		c := campaignState(t, campaign.ID)
		switch d.Status {
		case models.DecisionStatusConfirmed:
			if c.SpentTotal != 100 || c.ReservedTotal != 0 {
				t.Fatalf("iter %d: confirmed but ledgers wrong: %+v", i, c)
			}
		case models.DecisionStatusReleased:
			if c.SpentTotal != 0 || c.ReservedTotal != 0 {
				t.Fatalf("iter %d: released but ledgers wrong: %+v", i, c)
			}
			if confirmErr == nil {
				t.Fatalf("iter %d: released but confirm reported success", i)
			}
		default:
			t.Fatalf("iter %d: unexpected final status %s", i, d.Status)
		}
	}
}

// ---- cross-day reservation ----

func TestCrossDayReservationSettlesOnDecisionDay(t *testing.T) {
	campaign, _ := newFixture(t, 1_000_000, 0)
	svc := newService(5 * time.Minute)

	// Decision occurs at 23:59:30 UTC; budget day must be that UTC day even
	// though the confirm lands after midnight.
	day1 := time.Now().UTC().Truncate(24 * time.Hour)
	r := req("xday-1", "user-xday", campaign.ID, 100)
	r.OccurredAt = day1.Add(23*time.Hour + 59*time.Minute + 30*time.Second)
	mustApprove(t, svc, r)

	if _, err := svc.Confirm(context.Background(), "xday-1"); err != nil {
		t.Fatal(err)
	}

	dayStr := day1.Format("2006-01-02")
	var daily models.DailyBudget
	if err := testDB.Where("campaign_id = ? AND day = ?", campaign.ID, dayStr).First(&daily).Error; err != nil {
		t.Fatal(err)
	}
	if daily.Spent != 100 || daily.Reserved != 0 {
		t.Fatalf("day1 ledger wrong: %+v", daily)
	}
	var count int64
	testDB.Model(&models.DailyBudget{}).Where("campaign_id = ? AND day <> ?", campaign.ID, dayStr).Count(&count)
	if count != 0 {
		t.Fatalf("unexpected ledger rows for other days")
	}
}

// ---- recovery after restart ----

func TestRecoveryAfterRestartReleasesPendingReservations(t *testing.T) {
	campaign, _ := newFixture(t, 1_000_000, 0)

	// "Process 1": makes a reservation, then dies (no sweep, clock abandoned).
	svc1 := newService(5 * time.Minute)
	now := time.Now().UTC()
	svc1.SetClock(func() time.Time { return now })
	mustApprove(t, svc1, req("rec-1", "user-rec", campaign.ID, 100))

	// Simulate crash: svc1 is gone. "Process 2" boots against the same DB
	// with a later clock and runs its startup sweep.
	svc2 := newService(5 * time.Minute)
	svc2.SetClock(func() time.Time { return now.Add(10 * time.Minute) })
	if _, err := svc2.SweepExpired(context.Background()); err != nil {
		t.Fatal(err)
	}
	var d models.Decision
	if err := testDB.Where("request_id = ?", "rec-1").First(&d).Error; err != nil {
		t.Fatal(err)
	}
	if d.Status != models.DecisionStatusReleased {
		t.Fatalf("recovery sweep: status=%s, want released", d.Status)
	}
	if c := campaignState(t, campaign.ID); c.ReservedTotal != 0 {
		t.Fatalf("reserved budget not recovered: %d", c.ReservedTotal)
	}
}

func TestRecoveryCleansInterruptedEvaluatingRows(t *testing.T) {
	campaign, _ := newFixture(t, 1_000_000, 0)

	// Simulate a crash between idempotency insert and reservation commit:
	// a stale 'evaluating' row created two minutes ago.
	stale := models.Decision{
		RequestID:   "rec-stale",
		PayloadHash: "x",
		CampaignID:  campaign.ID,
		UserID:      "user-stale",
		Cost:        100,
		OccurredAt:  time.Now().UTC(),
		Day:         time.Now().UTC().Format("2006-01-02"),
		Status:      models.DecisionStatusEvaluating,
	}
	if err := testDB.Create(&stale).Error; err != nil {
		t.Fatal(err)
	}
	testDB.Exec("UPDATE decisions SET created_at = ? WHERE id = ?", time.Now().UTC().Add(-2*time.Minute), stale.ID)

	svc := newService(5 * time.Minute)
	if _, err := svc.SweepExpired(context.Background()); err != nil {
		t.Fatal(err)
	}
	var d models.Decision
	if err := testDB.First(&d, stale.ID).Error; err != nil {
		t.Fatal(err)
	}
	if d.Status != models.DecisionStatusRejected || d.RejectReason != ReasonInterrupted {
		t.Fatalf("stale row not cleaned: status=%s reason=%s", d.Status, d.RejectReason)
	}
}

// ---- campaign pause ----

func TestPauseBlocksNewDecisionsButReservationsStayConfirmable(t *testing.T) {
	campaign, _ := newFixture(t, 1_000_000, 0)
	svc := newService(5 * time.Minute)

	mustApprove(t, svc, req("pause-1", "user-pause", campaign.ID, 100))

	if err := svc.SetCampaignStatus(context.Background(), campaign.ID, models.CampaignStatusPaused); err != nil {
		t.Fatal(err)
	}

	// New decisions rejected immediately.
	res, err := svc.Decide(context.Background(), req("pause-2", "user-pause2", campaign.ID, 100))
	if err != nil {
		t.Fatal(err)
	}
	if res.Approved || res.Reason != ReasonCampaignNotActive {
		t.Fatalf("paused campaign: approved=%v reason=%s", res.Approved, res.Reason)
	}

	// Existing reservation still confirmable within its TTL.
	if _, err := svc.Confirm(context.Background(), "pause-1"); err != nil {
		t.Fatalf("confirm after pause should succeed: %v", err)
	}
	if c := campaignState(t, campaign.ID); c.SpentTotal != 100 {
		t.Fatalf("spent=%d", c.SpentTotal)
	}
}

// ---- targeting rules ----

func TestTargetingRuleRejections(t *testing.T) {
	now := time.Now().UTC()
	campaign := models.Campaign{
		Name:        fmt.Sprintf("rules-%d", time.Now().UnixNano()),
		Status:      models.CampaignStatusActive,
		StartAt:     now.Add(-time.Hour),
		EndAt:       now.Add(24 * time.Hour),
		TotalBudget: 1_000_000,
		Rules: models.Rules{
			Regions: []string{"CN"},
			Devices: []string{"ios"},
			Hours:   []int{now.Hour()},
		}.Marshal(),
	}
	if err := testDB.Create(&campaign).Error; err != nil {
		t.Fatal(err)
	}
	creative := models.Creative{CampaignID: campaign.ID, Name: "c", Status: models.CreativeStatusActive}
	if err := testDB.Create(&creative).Error; err != nil {
		t.Fatal(err)
	}
	svc := newService(5 * time.Minute)

	cases := []struct {
		name   string
		mutate func(*DecideRequest)
		reason string
	}{
		{"region", func(r *DecideRequest) { r.Region = "US" }, ReasonRegionNotTargeted},
		{"device", func(r *DecideRequest) { r.Device = "android" }, ReasonDeviceNotTargeted},
		{"hour", func(r *DecideRequest) { r.OccurredAt = now.Add(3 * time.Hour) }, ReasonHourNotTargeted},
		{"schedule", func(r *DecideRequest) { r.OccurredAt = now.Add(48 * time.Hour) }, ReasonOutsideSchedule},
		{"no-creative", func(r *DecideRequest) { r.CreativeID = 999999 }, ReasonCreativeNotFound},
	}
	for i, tc := range cases {
		r := req(fmt.Sprintf("rules-%d", i), "user-rules", campaign.ID, 10)
		r.OccurredAt = now
		tc.mutate(&r)
		res, err := svc.Decide(context.Background(), r)
		if err != nil {
			t.Fatalf("%s: %v", tc.name, err)
		}
		if res.Approved || res.Reason != tc.reason {
			t.Fatalf("%s: approved=%v reason=%s want %s", tc.name, res.Approved, res.Reason, tc.reason)
		}
	}

	// Matching request passes.
	r := req("rules-ok", "user-rules", campaign.ID, 10)
	r.OccurredAt = now
	mustApprove(t, svc, r)
}

func TestPausedCreativeRejected(t *testing.T) {
	campaign, creative := newFixture(t, 1_000_000, 0)
	if err := testDB.Model(&creative).Update("status", models.CreativeStatusPaused).Error; err != nil {
		t.Fatal(err)
	}
	svc := newService(5 * time.Minute)

	// Explicit paused creative.
	r := req("cr-1", "user-cr", campaign.ID, 10)
	r.CreativeID = creative.ID
	res, err := svc.Decide(context.Background(), r)
	if err != nil {
		t.Fatal(err)
	}
	if res.Approved || res.Reason != ReasonCreativeNotActive {
		t.Fatalf("approved=%v reason=%s", res.Approved, res.Reason)
	}

	// Auto-pick with no active creative left.
	res, err = svc.Decide(context.Background(), req("cr-2", "user-cr", campaign.ID, 10))
	if err != nil {
		t.Fatal(err)
	}
	if res.Approved || res.Reason != ReasonNoActiveCreative {
		t.Fatalf("approved=%v reason=%s", res.Approved, res.Reason)
	}
}
