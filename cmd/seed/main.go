// Command seed loads a small, deterministic demo dataset via the service
// layer (so every business rule is exercised rather than bypassed with raw
// inserts). It is safe to run repeatedly: it skips work if demo data exists.
//
//	go run ./cmd/seed
package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"communitygov/internal/database"
	"communitygov/internal/database/sqlcgen"
	"communitygov/internal/services"
)

func main() {
	url := os.Getenv("DATABASE_URL")
	if url == "" {
		url = "postgres://gov:gov@localhost:5432/gov?sslmode=disable"
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	pool, err := pgxpool.New(ctx, url)
	ck(err)
	defer pool.Close()
	ck(database.Migrate(ctx, pool))

	store := database.NewStore(pool)
	users := services.NewUserService(store)
	members := services.NewMembershipService(store)
	posts := services.NewPostService(store)
	courses := services.NewCourseService(store)

	if _, err := users.Login(ctx, "admin@demo.local", "demo1234"); err == nil {
		log.Println("demo data already present; skipping")
		return
	}

	admin := ckU(users.Register(ctx, "admin@demo.local", "Demo Admin", "demo1234", "admin"))
	reviewer := ckU(users.Register(ctx, "reviewer@demo.local", "Dana Reviewer", "demo1234", "reviewer"))
	author := ckU(users.Register(ctx, "author@demo.local", "Avery Author", "demo1234", "member"))
	member := ckU(users.Register(ctx, "member@demo.local", "Mia Member", "demo1234", "member"))

	community := ckC(users.CreateCommunity(ctx, "Go Enthusiasts", admin.ID))
	ck(users.AddReviewer(ctx, community.ID, reviewer.ID))

	tier1 := ckT(members.CreateTier(ctx, community.ID, 1, "Bronze", 900, 30))
	tier3 := ckT(members.CreateTier(ctx, community.ID, 3, "Gold", 2900, 90))

	_, _, err = members.RecordPayment(ctx, services.RecordPaymentParams{
		CommunityID: community.ID, RequestID: "seed-mia-gold", UserID: member.ID,
		TierID: tier3.ID, AmountCents: 2900, ExtendDays: 90, RecordedBy: admin.ID,
	})
	ck(err)

	p1, v1 := ckP(posts.CreatePost(ctx, community.ID, author.ID, 1,
		"Welcome to the community", "This is the bronze-level welcome article."))
	p1 = ckPv(posts.Submit(ctx, p1.ID, author.ID))
	p1 = ckPv(posts.Review(ctx, services.ReviewAction{
		PostID: p1.ID, CommunityID: community.ID, ReviewerID: reviewer.ID,
		VersionID: v1.ID, Approve: true, Reason: "looks great", ExpectedStatus: "pending",
	}))

	_, _, _ = posts.CreatePost(ctx, community.ID, author.ID, 3,
		"Advanced concurrency patterns", "Draft of the gold deep-dive (not yet submitted).")

	course := ckCo(courses.Create(ctx, community.ID, "Getting Started"))
	ck(courses.SetStructure(ctx, course.ID, community.ID, []services.ModuleInput{{
		Position: 1,
		Title:    "Orientation",
		Lessons: []services.LessonInput{{
			Position: 1, Title: "Welcome", ContentVersionID: p1.PublishedVersionID.Int64,
		}},
	}}))
	ckCo(courses.Publish(ctx, course.ID, community.ID, admin.ID))

	_ = tier1
	fmt.Println(`Demo data created. Log in as any of:
  admin@demo.local    / demo1234   (records payments, tiers, communities)
  reviewer@demo.local / demo1234   (moderates content; CANNOT record payments)
  author@demo.local   / demo1234   (writes posts)
  member@demo.local   / demo1234   (Gold member, 90 days)

Try:
  curl -s localhost:8080/api/login -d '{"email":"member@demo.local","password":"demo1234"}'`)
}

func ck(err error) {
	if err != nil {
		log.Fatal(err)
	}
}
func ckU(u sqlcgen.User, err error) sqlcgen.User           { ck(err); return u }
func ckC(c sqlcgen.Community, err error) sqlcgen.Community { ck(err); return c }
func ckT(t sqlcgen.Tier, err error) sqlcgen.Tier           { ck(err); return t }
func ckP(p sqlcgen.Post, v sqlcgen.PostVersion, err error) (sqlcgen.Post, sqlcgen.PostVersion) {
	ck(err)
	return p, v
}
func ckPv(p sqlcgen.Post, err error) sqlcgen.Post     { ck(err); return p }
func ckCo(c sqlcgen.Course, err error) sqlcgen.Course { ck(err); return c }
