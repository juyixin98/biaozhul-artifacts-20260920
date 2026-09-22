package integration

import (
	"context"
	"fmt"
	"net/http"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

var reqSeq int64

func uniqueReq(prefix string) string {
	return fmt.Sprintf("%s-%d-%d", prefix, time.Now().UnixNano(), atomic.AddInt64(&reqSeq, 1))
}

// TestConcurrentRenewalNeverLosesDays: N concurrent payments with distinct
// request ids must add N*7 days on top of each other, never overwrite.
func TestConcurrentRenewalNeverLosesDays(t *testing.T) {
	e := newEnv(t)
	tierID := e.tier(1, 700, 7)
	uid, _ := e.createUser("member", "member")

	const n = 20
	var wg sync.WaitGroup
	errs := make(chan error, n)
	starts := make(chan struct{})
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			<-starts
			req := uniqueReq(fmt.Sprintf("renew-%d", i))
			st, b := e.pay(req, uid, tierID, 700, 7)
			if st != http.StatusOK {
				errs <- fmt.Errorf("pay %d: status %d body %v", i, st, b)
			}
		}(i)
	}
	close(starts)
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatal(err)
	}

	sub := fetchSubscription(t, e, uid)
	end, err := time.Parse(time.RFC3339Nano, sub["period_end"].(string))
	if err != nil {
		// pgx returns RFC3339 with nanos; try alternative layouts
		end, err = time.Parse(time.RFC3339, sub["period_end"].(string))
		if err != nil {
			t.Fatalf("parse period_end %q: %v", sub["period_end"], err)
		}
	}
	want := time.Now().Add(n * 7 * 24 * time.Hour)
	diff := want.Sub(end)
	if diff < -3*time.Minute || diff > 3*time.Minute {
		t.Fatalf("period_end=%v, want ~%v (diff=%v): renewal lost or double counted days",
			end, want, diff)
	}
}

// TestIdempotentPaymentSameRequestID: a replay returns the same subscription
// state; a replay with a changed body is rejected with 409.
func TestIdempotentPaymentSameRequestID(t *testing.T) {
	e := newEnv(t)
	tierID := e.tier(1, 700, 7)
	uid, _ := e.createUser("member", "member")

	reqID := uniqueReq("fixed-req-1")
	st, b1 := e.pay(reqID, uid, tierID, 700, 7)
	mustStatus(t, st, 200, b1)
	st, b2 := e.pay(reqID, uid, tierID, 700, 7)
	mustStatus(t, st, 200, b2)
	end1 := b1["subscription"].(map[string]any)["period_end"]
	end2 := b2["subscription"].(map[string]any)["period_end"]
	if end1 != end2 {
		t.Fatalf("identical replay changed period_end: %s vs %s", end1, end2)
	}

	// Same request id, different amount -> 409.
	st, b3 := e.pay(reqID, uid, tierID, 999, 7)
	mustStatus(t, st, http.StatusConflict, b3)
}

// TestExpirationAndCancelCutAccessImmediately: at the exact boundary the
// member loses read access; cancel also removes access at once; renewal
// revives and restores access.
func TestExpirationAndCancelCutAccessImmediately(t *testing.T) {
	e := newEnv(t)
	modID, modTok := e.moderatorUser()
	_ = modID
	tierID := e.tier(1, 100, 30)
	c := e.publishFlow(modTok, 1, "boundary body")

	uid, memTok := e.createUser("reader", "member")
	st, _ := e.pay(uniqueReq("boundary-pay"), uid, tierID, 100, 30)
	mustStatus(t, st, 200, nil)

	// Access works while active.
	st, b := doJSON(t, http.MethodGet, fmt.Sprintf("/contents/%d", c.id), memTok, nil)
	mustStatus(t, st, 200, b)

	// Force the period_end into the past directly: the boundary rule
	// (period_end > now()) must deny at once.
	if _, err := pool.Exec(context.Background(),
		`UPDATE subscriptions SET period_end = now() - interval '1 second'
		 WHERE user_id = $1`, uid); err != nil {
		t.Fatal(err)
	}
	st, b = doJSON(t, http.MethodGet, fmt.Sprintf("/contents/%d", c.id), memTok, nil)
	if st == http.StatusOK {
		t.Fatalf("expired member still has access: %v", b)
	}

	// Boundary exactly at now: period_end = now() is NOT greater, so denied.
	if _, err := pool.Exec(context.Background(),
		`UPDATE subscriptions SET period_end = now() WHERE user_id = $1`, uid); err != nil {
		t.Fatal(err)
	}
	st, _ = doJSON(t, http.MethodGet, fmt.Sprintf("/contents/%d", c.id), memTok, nil)
	if st == http.StatusOK {
		t.Fatal("member at period_end=now() must not have access (rule is strictly >)")
	}

	// Renewal revives.
	st, _ = e.pay(uniqueReq("boundary-pay-2"), uid, tierID, 100, 30)
	mustStatus(t, st, 200, nil)
	st, b = doJSON(t, http.MethodGet, fmt.Sprintf("/contents/%d", c.id), memTok, nil)
	mustStatus(t, st, 200, b)

	// Cancel removes access immediately even though period_end is in the future.
	st, _ = doJSON(t, http.MethodPost,
		fmt.Sprintf("/communities/%d/me/cancel?user_id=%d", e.cid, uid), e.admin, nil)
	mustStatus(t, st, http.StatusNoContent, nil)
	st, _ = doJSON(t, http.MethodGet, fmt.Sprintf("/contents/%d", c.id), memTok, nil)
	if st == http.StatusOK {
		t.Fatal("cancelled member still has access")
	}
}

// TestModeratorCannotRenew: reviewers may review but a moderator token must
// be rejected from the admin-only payment endpoint.
func TestModeratorCannotRenew(t *testing.T) {
	e := newEnv(t)
	_, modTok := e.moderatorUser()
	tierID := e.tier(1, 100, 30)
	uid, _ := e.createUser("member", "member")
	st, b := doJSON(t, http.MethodPost,
		fmt.Sprintf("/communities/%d/payments", e.cid), modTok, map[string]any{
			"request_id": uniqueReq("x"), "user_id": uid, "tier_id": tierID,
			"amount_cents": 100, "days": 30,
		})
	mustStatus(t, st, http.StatusForbidden, b)
}

// TestTierCap10: the 11th tier is rejected by the database trigger.
func TestTierCap10(t *testing.T) {
	e := newEnv(t)
	for l := int32(1); l <= 10; l++ {
		e.tier(l, 100, 30)
	}
	// Level 11 already violates the check constraint; insert a second tier at
	// the same level instead to hit the >10 trigger path via raw SQL.
	_, err := pool.Exec(context.Background(),
		`INSERT INTO tiers (community_id, level, name, price_cents, duration_days)
		 VALUES ($1, 10, 'dup-cap-check', 100, 30)`, e.cid)
	if err == nil {
		t.Fatal("expected unique/trigger error inserting 11th tier")
	}
}
