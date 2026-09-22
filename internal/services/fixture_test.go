package services_test

import (
	"context"
	"testing"

	"github.com/jackc/pgx/v5/pgxpool"

	"communitygov/internal/database"
	"communitygov/internal/database/dbtest"
	"communitygov/internal/database/sqlcgen"
	"communitygov/internal/services"
)

// fixture bundles a fresh migrated store and the services under test.
type fixture struct {
	t       *testing.T
	ctx     context.Context
	store   *database.Store
	users   *services.UserService
	posts   *services.PostService
	member  *services.MembershipService
	report  *services.ReportService
	course  *services.CourseService
	admin   sqlcgen.User // one global admin used to record payments
	rev     sqlcgen.User // lazily-created community reviewer
	revMade bool
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	store := dbtest.NewDB(t)
	posts := services.NewPostService(store)
	f := &fixture{
		t:      t,
		ctx:    context.Background(),
		store:  store,
		users:  services.NewUserService(store),
		posts:  posts,
		member: services.NewMembershipService(store),
		report: services.NewReportService(store, posts),
		course: services.NewCourseService(store),
	}
	f.admin = f.mustRegister("admin", "root-admin@x.io")
	return f
}

func (f *fixture) mustRegister(role, email string) sqlcgen.User {
	f.t.Helper()
	u, err := f.users.Register(f.ctx, email, email, "password123", role)
	if err != nil {
		f.t.Fatalf("register %s: %v", email, err)
	}
	return u
}

func (f *fixture) mustCommunity(name string, adminID int64) sqlcgen.Community {
	f.t.Helper()
	c, err := f.users.CreateCommunity(f.ctx, name, adminID)
	if err != nil {
		f.t.Fatalf("create community: %v", err)
	}
	return c
}

func (f *fixture) mustTier(communityID int64, level int32, days int32) sqlcgen.Tier {
	f.t.Helper()
	t, err := f.member.CreateTier(f.ctx, communityID, level, "L", int64(level)*1000, days)
	if err != nil {
		f.t.Fatalf("create tier: %v", err)
	}
	return t
}

func (f *fixture) addReviewer(communityID, userID int64) {
	f.t.Helper()
	if err := f.users.AddReviewer(f.ctx, communityID, userID); err != nil {
		f.t.Fatalf("add reviewer: %v", err)
	}
}

// exec runs raw SQL against the test schema (used for expiry manipulation).
func (f *fixture) exec(sql string, args ...any) {
	f.t.Helper()
	if _, err := f.store.Pool().Exec(f.ctx, sql, args...); err != nil {
		f.t.Fatalf("exec %q: %v", sql, err)
	}
}

var _ = (*pgxpool.Pool)(nil)

// pay records a payment as the fixture's admin and fails the test on error.
func (f *fixture) pay(communityID, userID, tierID int64, reqID string, days int32) sqlcgen.Membership {
	f.t.Helper()
	_, m, err := f.member.RecordPayment(f.ctx, services.RecordPaymentParams{
		CommunityID: communityID, RequestID: reqID, UserID: userID, TierID: tierID,
		AmountCents: int64(days) * 100, ExtendDays: days, RecordedBy: f.admin.ID,
	})
	if err != nil {
		f.t.Fatalf("record payment: %v", err)
	}
	return m
}
