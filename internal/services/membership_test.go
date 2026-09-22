package services_test

import (
	"fmt"
	"sync"
	"testing"
	"time"

	"communitygov/internal/services"
)

// TestConcurrentRenewalNoLostTime: two payments recorded concurrently must
// BOTH add their full duration. This is the "并发续期不能丢失时长" guarantee —
// the membership row lock plus GREATEST(expires_at, now()) must serialize the
// extensions.
func TestConcurrentRenewalNoLostTime(t *testing.T) {
	f := newFixture(t)
	admin := f.mustRegister("admin", "admin@x.io")
	member := f.mustRegister("member", "m@x.io")
	c := f.mustCommunity("C", admin.ID)
	tier := f.mustTier(c.ID, 1, 30)

	const N = 8
	var wg sync.WaitGroup
	errs := make([]error, N)
	start := make(chan struct{})
	wg.Add(N)
	for i := 0; i < N; i++ {
		i := i
		go func() {
			defer wg.Done()
			<-start
			_, _, errs[i] = f.member.RecordPayment(f.ctx, services.RecordPaymentParams{
				CommunityID: c.ID, RequestID: fmt.Sprintf("req-%d", i),
				UserID: member.ID, TierID: tier.ID,
				AmountCents: 500, ExtendDays: 30, RecordedBy: admin.ID,
			})
		}()
	}
	close(start)
	wg.Wait()
	for i, err := range errs {
		if err != nil {
			t.Fatalf("renewal %d failed: %v", i, err)
		}
	}

	m, err := f.member.Get(f.ctx, c.ID, member.ID)
	if err != nil {
		t.Fatal(err)
	}
	wantMin := time.Now().Add(time.Duration(N*30-1) * 24 * time.Hour)
	if !m.ExpiresAt.After(wantMin) {
		t.Fatalf("lost renewal time: expiry %s, want >= ~%d days out (%s)",
			m.ExpiresAt, N*30, wantMin)
	}
}

// TestPaymentIdempotentReplayAndConflict: same request id replays identically;
// same request id with a different body is 409.
func TestPaymentIdempotentReplayAndConflict(t *testing.T) {
	f := newFixture(t)
	admin := f.mustRegister("admin", "p-admin@x.io")
	member := f.mustRegister("member", "p-m@x.io")
	c := f.mustCommunity("C", admin.ID)
	tier := f.mustTier(c.ID, 1, 10)

	params := services.RecordPaymentParams{
		CommunityID: c.ID, RequestID: "idem-1", UserID: member.ID, TierID: tier.ID,
		AmountCents: 1000, ExtendDays: 10, RecordedBy: admin.ID,
	}
	p1, m1, err := f.member.RecordPayment(f.ctx, params)
	if err != nil {
		t.Fatal(err)
	}
	// Identical replay.
	p2, m2, err := f.member.RecordPayment(f.ctx, params)
	if err != nil {
		t.Fatalf("idempotent replay failed: %v", err)
	}
	if p1.ID != p2.ID {
		t.Fatalf("replay created a new payment row: %d vs %d", p1.ID, p2.ID)
	}
	if !m1.ExpiresAt.Equal(m2.ExpiresAt) {
		t.Fatalf("replay changed expiry: %s vs %s", m1.ExpiresAt, m2.ExpiresAt)
	}

	// Same request id, different payload -> conflict.
	bad := params
	bad.AmountCents = 9999
	if _, _, err := f.member.RecordPayment(f.ctx, bad); err == nil {
		t.Fatal("expected conflict for same request id with different payload")
	} else if services.HTTPStatus(err) != 409 {
		t.Fatalf("want 409, got %d (%v)", services.HTTPStatus(err), err)
	}
}

// TestExpiryBoundary: the instant expires_at passes, access is denied; at
// exactly the boundary it is allowed. Because CheckVersionAccess uses
// expires_at > now(), a row set exactly to now() must be inaccessible, and a
// row one second in the future accessible.
func TestExpiryBoundary(t *testing.T) {
	f := newFixture(t)
	admin := f.mustRegister("admin", "e-admin@x.io")
	author := f.mustRegister("member", "e-author@x.io")
	viewer := f.mustRegister("member", "e-viewer@x.io")
	c := f.mustCommunity("C", admin.ID)
	tier := f.mustTier(c.ID, 2, 30)

	// Publish a level-1 post.
	_, v := publishPost(t, f, c.ID, author.ID, 1)

	// Active unexpired membership at level 2 -> allowed.
	f.pay(c.ID, viewer.ID, tier.ID, "p-ok", 30)
	if _, err := f.posts.AuthorizeVersion(f.ctx, v.ID, readCtx(c.ID, viewer)); err != nil {
		t.Fatalf("active member should access: %v", err)
	}

	// Set expiry exactly to now(): expires_at > now() is false -> denied.
	f.exec(`UPDATE memberships SET expires_at = now() WHERE community_id=$1 AND user_id=$2`,
		c.ID, viewer.ID)
	if _, err := f.posts.AuthorizeVersion(f.ctx, v.ID, readCtx(c.ID, viewer)); err == nil {
		t.Fatal("membership expiring exactly at now() must be denied (strict >)")
	}

	// One second in the future -> allowed again.
	f.exec(`UPDATE memberships SET expires_at = now() + interval '1 second'
		WHERE community_id=$1 AND user_id=$2`, c.ID, viewer.ID)
	if _, err := f.posts.AuthorizeVersion(f.ctx, v.ID, readCtx(c.ID, viewer)); err != nil {
		t.Fatalf("membership valid for 1s should access: %v", err)
	}

	// Expired in the past -> denied.
	f.exec(`UPDATE memberships SET expires_at = now() - interval '1 second'
		WHERE community_id=$1 AND user_id=$2`, c.ID, viewer.ID)
	if _, err := f.posts.AuthorizeVersion(f.ctx, v.ID, readCtx(c.ID, viewer)); err == nil {
		t.Fatal("expired member must be denied")
	}
}

// TestCancellationImmediateRevocation: cancelling a membership removes access
// on the very next request even though expires_at is far in the future.
func TestCancellationImmediateRevocation(t *testing.T) {
	f := newFixture(t)
	admin := f.mustRegister("admin", "c-admin@x.io")
	author := f.mustRegister("member", "c-author@x.io")
	viewer := f.mustRegister("member", "c-viewer@x.io")
	c := f.mustCommunity("C", admin.ID)
	tier := f.mustTier(c.ID, 1, 365)
	_, v := publishPost(t, f, c.ID, author.ID, 1)
	f.pay(c.ID, viewer.ID, tier.ID, "p", 365)

	if _, err := f.posts.AuthorizeVersion(f.ctx, v.ID, readCtx(c.ID, viewer)); err != nil {
		t.Fatalf("active: %v", err)
	}
	if _, err := f.member.Cancel(f.ctx, c.ID, viewer.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := f.posts.AuthorizeVersion(f.ctx, v.ID, readCtx(c.ID, viewer)); err == nil {
		t.Fatal("cancelled member must immediately lose access")
	}
}

// TestTierLevelIsolation: a level-1 membership cannot read a level-3 post.
func TestTierLevelIsolation(t *testing.T) {
	f := newFixture(t)
	admin := f.mustRegister("admin", "t-admin@x.io")
	author := f.mustRegister("member", "t-author@x.io")
	viewer := f.mustRegister("member", "t-viewer@x.io")
	c := f.mustCommunity("C", admin.ID)
	l1 := f.mustTier(c.ID, 1, 30)
	f.mustTier(c.ID, 3, 30)
	_, v := publishPost(t, f, c.ID, author.ID, 3)
	f.pay(c.ID, viewer.ID, l1.ID, "p", 30)
	if _, err := f.posts.AuthorizeVersion(f.ctx, v.ID, readCtx(c.ID, viewer)); err == nil {
		t.Fatal("level-1 member must not read level-3 content")
	}
}

// TestTenTierCap: the 11th tier is rejected.
func TestTenTierCap(t *testing.T) {
	f := newFixture(t)
	admin := f.mustRegister("admin", "cap-admin@x.io")
	c := f.mustCommunity("C", admin.ID)
	for level := int32(1); level <= 10; level++ {
		f.mustTier(c.ID, level, 30)
	}
	_, err := f.member.CreateTier(f.ctx, c.ID, 11, "x", 100, 30)
	if err == nil || services.HTTPStatus(err) != 400 {
		t.Fatalf("level 11 must be rejected at validation, got %v", err)
	}
	// Duplicate an existing level -> conflict.
	if _, err := f.member.CreateTier(f.ctx, c.ID, 5, "dup", 100, 30); err == nil {
		t.Fatal("duplicate tier level must conflict")
	}
}
