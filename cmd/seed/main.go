// Command seed populates a small, self-consistent demo community:
// tiers, users, a paid member, a fully reviewed piece of content with an
// attachment, an upheld report on a second post, and a published course.
package main

import (
	"context"
	"fmt"
	"log"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgtype"
	"github.com/jackc/pgx/v5/pgxpool"

	"community-governance/internal/config"
	"community-governance/internal/database"
)

func main() {
	cfg := config.Load()
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, cfg.Database)
	if err != nil {
		log.Fatal(err)
	}
	defer pool.Close()
	q := database.New(pool)

	c, err := q.CreateCommunity(ctx, "示例社区 / Demo Community")
	must(err)

	tok := func() string { return "tok_" + uuid.NewString() }
	admin, err := q.CreateUser(ctx, database.CreateUserParams{
		CommunityID: c.ID, Username: "admin", Role: "admin", Token: tok()})
	must(err)
	mod, err := q.CreateUser(ctx, database.CreateUserParams{
		CommunityID: c.ID, Username: "reviewer", Role: "moderator", Token: tok()})
	must(err)
	alice, err := q.CreateUser(ctx, database.CreateUserParams{
		CommunityID: c.ID, Username: "alice", Role: "member", Token: tok()})
	must(err)
	bob, err := q.CreateUser(ctx, database.CreateUserParams{
		CommunityID: c.ID, Username: "bob", Role: "member", Token: tok()})
	must(err)
	member, err := q.CreateUser(ctx, database.CreateUserParams{
		CommunityID: c.ID, Username: "chris", Role: "member", Token: tok()})
	must(err)

	bronze, err := q.CreateTier(ctx, database.CreateTierParams{
		CommunityID: c.ID, Level: 1, Name: "Bronze",
		PriceCents: 990, DurationDays: 30})
	must(err)
	_, err = q.CreateTier(ctx, database.CreateTierParams{
		CommunityID: c.ID, Level: 5, Name: "Gold",
		PriceCents: 4990, DurationDays: 90})
	must(err)

	// Admin records an offline payment for chris (integer cents + request id).
	_, err = q.CreatePayment(ctx, database.CreatePaymentParams{
		CommunityID: c.ID, RequestID: "seed-req-0001", UserID: member.ID,
		TierID: bronze.ID, AmountCents: 990, Days: 30})
	must(err)
	_, err = q.UpsertSubscription(ctx, database.UpsertSubscriptionParams{
		CommunityID: c.ID, UserID: member.ID, TierID: bronze.ID, Days: 30})
	must(err)

	// Alice writes a level-1 post, submits, reviewer approves.
	post1, err := q.CreateContent(ctx, database.CreateContentParams{
		CommunityID: c.ID, AuthorID: alice.ID, Title: "入门指南", RequiredLevel: 1})
	must(err)
	v1, err := q.CreateVersion(ctx, database.CreateVersionParams{
		ContentID: post1.ID, CommunityID: c.ID, VersionNo: 1,
		Body: "这是入门指南正文 v1。", CreatedBy: alice.ID})
	must(err)
	must(q.BindInitialVersion(ctx, database.BindInitialVersionParams{
		ID: post1.ID, CommunityID: c.ID,
		CurrentVersionID: pgtype.Int8{Int64: v1.ID, Valid: true}}))
	_, err = q.SubmitContent(ctx, database.SubmitContentParams{
		ID: post1.ID, CommunityID: c.ID,
		CurrentVersionID: pgtype.Int8{Int64: v1.ID, Valid: true}})
	must(err)
	must(q.MarkVersionPending(ctx, database.MarkVersionPendingParams{ID: v1.ID, CommunityID: c.ID}))
	pub1, err := q.ApproveContent(ctx, database.ApproveContentParams{
		ID: post1.ID, CommunityID: c.ID,
		CurrentVersionID: pgtype.Int8{Int64: v1.ID, Valid: true}})
	must(err)
	must(q.ApproveVersion(ctx, database.ApproveVersionParams{ID: v1.ID, CommunityID: c.ID}))
	_, err = q.AddAttachment(ctx, database.AddAttachmentParams{
		CommunityID: c.ID, VersionID: v1.ID, Filename: "welcome.txt",
		ContentType: "text/plain", Data: []byte("hello from the seed")})
	must(err)
	must(q.InsertContentEvent(ctx, database.InsertContentEventParams{
		CommunityID: c.ID, ContentID: post1.ID,
		VersionID: pgtype.Int8{Int64: v1.ID, Valid: true},
		ActorID:   mod.ID, Action: "approve", Reason: "内容合格"}))

	// Bob writes a second post; it gets reported and upheld (delisted).
	post2, err := q.CreateContent(ctx, database.CreateContentParams{
		CommunityID: c.ID, AuthorID: bob.ID, Title: "争议文章", RequiredLevel: 1})
	must(err)
	v2, err := q.CreateVersion(ctx, database.CreateVersionParams{
		ContentID: post2.ID, CommunityID: c.ID, VersionNo: 1,
		Body: "争议内容 v1。", CreatedBy: bob.ID})
	must(err)
	must(q.BindInitialVersion(ctx, database.BindInitialVersionParams{
		ID: post2.ID, CommunityID: c.ID,
		CurrentVersionID: pgtype.Int8{Int64: v2.ID, Valid: true}}))
	_, err = q.SubmitContent(ctx, database.SubmitContentParams{
		ID: post2.ID, CommunityID: c.ID,
		CurrentVersionID: pgtype.Int8{Int64: v2.ID, Valid: true}})
	must(err)
	must(q.MarkVersionPending(ctx, database.MarkVersionPendingParams{ID: v2.ID, CommunityID: c.ID}))
	_, err = q.ApproveContent(ctx, database.ApproveContentParams{
		ID: post2.ID, CommunityID: c.ID,
		CurrentVersionID: pgtype.Int8{Int64: v2.ID, Valid: true}})
	must(err)
	must(q.ApproveVersion(ctx, database.ApproveVersionParams{ID: v2.ID, CommunityID: c.ID}))
	rep, err := q.CreateReport(ctx, database.CreateReportParams{
		CommunityID: c.ID, ContentID: post2.ID, VersionID: v2.ID,
		ReporterID: member.ID, Category: "spam", Reason: "示例举报：疑似垃圾内容"})
	must(err)
	_, err = q.UpholdReport(ctx, database.UpholdReportParams{ID: rep.ID, CommunityID: c.ID})
	must(err)
	_, err = q.DelistContent(ctx, database.DelistContentParams{ID: post2.ID, CommunityID: c.ID})
	must(err)
	must(q.InsertReportEvent(ctx, database.InsertReportEventParams{
		ReportID: rep.ID, CommunityID: c.ID, ActorID: mod.ID,
		Action: "uphold", Basis: "核实为违规示例"}))

	// A published course with one module / one lesson pointing at post1 v1.
	course, err := q.CreateCourse(ctx, database.CreateCourseParams{
		CommunityID: c.ID, AuthorID: alice.ID, Title: "新人课程", RequiredLevel: 1})
	must(err)
	modRow, err := q.CreateModule(ctx, database.CreateModuleParams{
		CourseID: course.ID, Position: 1, Title: "模块一"})
	must(err)
	_, err = q.CreateLesson(ctx, database.CreateLessonParams{
		ModuleID: modRow.ID, Position: 1, Title: "阅读入门指南",
		ContentVersionID: pgtype.Int8{Int64: v1.ID, Valid: true}})
	must(err)
	pub, err := q.CreatePublish(ctx, database.CreatePublishParams{
		CourseID: course.ID, CommunityID: c.ID, PublishedBy: admin.ID})
	must(err)
	pm, err := q.CreatePublishModule(ctx, database.CreatePublishModuleParams{
		PublishID: pub.ID, Position: 1, Title: "模块一"})
	must(err)
	_, err = q.CreatePublishLesson(ctx, database.CreatePublishLessonParams{
		PublishModuleID: pm.ID, Position: 1, Title: "阅读入门指南",
		ContentVersionID: v1.ID, ContentID: post1.ID, RequiredLevel: 1})
	must(err)
	must(q.MarkCoursePublished(ctx, database.MarkCoursePublishedParams{
		ID: course.ID, CommunityID: c.ID}))

	fmt.Println("seed complete at", time.Now().UTC().Format(time.RFC3339))
	fmt.Printf("community_id=%d\n", c.ID)
	fmt.Printf("published content id=%d (version %d)\n", pub1.ID, v1.ID)
	fmt.Printf("delisted  content id=%d (report %d)\n", post2.ID, rep.ID)
	fmt.Printf("published course id=%d\n", course.ID)
	fmt.Println("tokens (use as 'Authorization: Bearer <token>'):")
	fmt.Printf("  admin    %s\n", admin.Token)
	fmt.Printf("  reviewer %s\n", mod.Token)
	fmt.Printf("  alice    %s\n", alice.Token)
	fmt.Printf("  bob      %s\n", bob.Token)
	fmt.Printf("  chris    %s  (Bronze member, 30 days)\n", member.Token)
}

func must(err error) {
	if err != nil {
		log.Fatalf("seed: %v", err)
	}
}
