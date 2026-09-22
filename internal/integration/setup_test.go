package integration

import (
	"context"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"communityvault/internal/content"
	"communityvault/internal/db"
	"communityvault/internal/moderation"
	"communityvault/internal/rules"
	"communityvault/internal/testsupport"
	"communityvault/internal/view"
)

type env struct {
	pool *pgxpool.Pool
	ctx  context.Context
	rs   *rules.Service
	cts  *content.Service
	mod  *moderation.Service
	vs   *view.Service
}

func setup(t *testing.T, ttl ...time.Duration) *env {
	t.Helper()
	pool := testsupport.Pool(t)
	testsupport.Reset(t, pool)
	claimTTL := time.Hour
	if len(ttl) > 0 {
		claimTTL = ttl[0]
	}
	rs := rules.New(pool)
	return &env{
		pool: pool,
		ctx:  context.Background(),
		rs:   rs,
		cts:  content.New(pool, rs),
		mod:  moderation.New(pool, rs, claimTTL),
		vs:   view.New(pool),
	}
}

func (e *env) user(t *testing.T, token string) db.User {
	t.Helper()
	u, err := db.New(e.pool).GetUserByToken(e.ctx, token)
	if err != nil {
		t.Fatalf("user %s: %v", token, err)
	}
	return u
}

func (e *env) catIDs(t *testing.T, modID int64) []int64 {
	t.Helper()
	ids, err := db.New(e.pool).ModeratorCategoryIDs(e.ctx, modID)
	if err != nil {
		t.Fatalf("cats: %v", err)
	}
	return ids
}

// submitPublished is the happy-path helper: author creates clean content and
// submits; moderator claims and approves. Returns content + revisions.
func (e *env) submitPublished(t *testing.T, author db.User, cat int64, title, body string) (db.Content, []db.ContentRevision) {
	t.Helper()
	return e.submitPublishedBy(t, author, e.user(t, "token-mod-tech"), cat, title, body)
}

func (e *env) submitPublishedBy(t *testing.T, author, mod db.User, cat int64, title, body string) (db.Content, []db.ContentRevision) {
	t.Helper()
	c, rev1, err := e.cts.Create(e.ctx, author.ID, cat, title, body)
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	if c, _, _, err = e.cts.Submit(e.ctx, author.ID, c.ID); err != nil {
		t.Fatalf("submit: %v", err)
	}
	task, _, err := e.mod.Claim(e.ctx, mod, e.catIDs(t, mod.ID))
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if _, err := e.mod.Approve(e.ctx, mod, task.ID, e.catIDs(t, mod.ID)); err != nil {
		t.Fatalf("approve: %v", err)
	}
	revs, err := db.New(e.pool).ListRevisions(e.ctx, c.ID)
	if err != nil {
		t.Fatalf("list revs: %v", err)
	}
	got, err := db.New(e.pool).GetContent(e.ctx, c.ID)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	return got, append([]db.ContentRevision{rev1}, revs...)
}
