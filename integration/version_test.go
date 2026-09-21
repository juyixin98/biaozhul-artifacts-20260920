package integration

import (
	"context"
	"testing"

	"desklens/internal/admin"
	"desklens/internal/model"
	"desklens/internal/testsupport"
)

// TestClassificationVersioning verifies that publishing a new classification
// only affects new ingest, and that every raw row keeps the version and rule
// applied at ingest time. Existing history and its classification cannot be
// silently rewritten.
func TestClassificationVersioning(t *testing.T) {
	e := setup(t)
	e.publishWidePolicy("00:00", "24:00")

	before := utc(2026, 9, 15, 14, 0)
	out := e.mustIngest(p101, "cv-1",
		snapAt("WS-101", 101, before, "WeChat", 10))
	if out.Items[0].Category != model.CategoryUnproductive ||
		out.Items[0].RuleID != "social-01" || out.ClassVersion != 1 {
		t.Fatalf("v1 classification outcome = %+v", out.Items[0])
	}

	// Publish v2: WeChat becomes productive with a different rule id; add a new
	// high-priority game rule.
	v2, err := e.Admin.PublishClassification(context.Background(), admin.ClassificationInput{
		PublishedBy: "test",
		Rules: []admin.RuleInput{
			{RuleID: "social-new", Pattern: "wechat*", Category: model.CategoryProductive, Priority: 30},
			{RuleID: "game-new", Pattern: "*game*", Category: model.CategoryUnproductive, Priority: 5},
			{RuleID: "code-new", Pattern: "code", Category: model.CategoryProductive, Priority: 10},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if v2.Version != 2 {
		t.Fatalf("new classification version = %d, want 2", v2.Version)
	}

	// A new snapshot of the same app uses the new version/category.
	after := utc(2026, 9, 15, 14, 1)
	out = e.mustIngest(p101, "cv-2",
		snapAt("WS-101", 101, after, "WeChat", 20))
	if out.Items[0].Category != model.CategoryProductive ||
		out.Items[0].RuleID != "social-new" || out.ClassVersion != 2 {
		t.Fatalf("v2 classification outcome = %+v (class version %d)",
			out.Items[0], out.ClassVersion)
	}

	// Raw rows carry their own version stamps.
	var cvBefore, cvAfter int64
	var catBefore, catAfter, ruleBefore, ruleAfter string
	if err := e.DB.QueryRowx(`
		select classification_version, category, coalesce(matched_rule_id,'')
		from activity_snapshots where workstation_id='WS-101' and bucket_time=$1`,
		before).Scan(&cvBefore, &catBefore, &ruleBefore); err != nil {
		t.Fatal(err)
	}
	if err := e.DB.QueryRowx(`
		select classification_version, category, coalesce(matched_rule_id,'')
		from activity_snapshots where workstation_id='WS-101' and bucket_time=$1`,
		after).Scan(&cvAfter, &catAfter, &ruleAfter); err != nil {
		t.Fatal(err)
	}
	if cvBefore != 1 || catBefore != "unproductive" || ruleBefore != "social-01" {
		t.Fatalf("historical row rewritten: v=%d cat=%s rule=%s", cvBefore, catBefore, ruleBefore)
	}
	if cvAfter != 2 || catAfter != "productive" || ruleAfter != "social-new" {
		t.Fatalf("new row not stamped with v2: v=%d cat=%s rule=%s", cvAfter, catAfter, ruleAfter)
	}

	// The daily summary records the version span: min 1, max 2.
	d, _ := testsupport.GetDaily(t, e.DB, 101, "2026-09-15")
	if d.CvMin != 1 || d.CvMax != 2 {
		t.Fatalf("classification version span = [%d,%d], want [1,2]", d.CvMin, d.CvMax)
	}
	// Totals preserve both categories independently.
	if d.ProductiveCount != 20 || d.UnproductiveCount != 10 {
		t.Fatalf("daily counts = productive %d unproductive %d", d.ProductiveCount, d.UnproductiveCount)
	}

	// Re-inserting the same historical minute keeps the identical v1 content
	// (duplicate), it is NOT reclassified under v2.
	out = e.mustIngest(p101, "cv-3",
		snapAt("WS-101", 101, before, "WeChat", 10))
	if out.Duplicates != 1 || out.Accepted != 0 {
		t.Fatalf("historical resend outcome = %+v", out)
	}
}

// TestPolicyVersioning verifies that a published policy only affects new
// ingest and that raw rows stamp the policy version actually applied.
func TestPolicyVersioning(t *testing.T) {
	e := setup(t)
	// v1: window 08:00-18:00 NY local.
	inV1 := utc(2026, 9, 15, 14, 0) // 10:00 EDT, accepted under v1
	out := e.mustIngest(p101, "pv-1",
		snapAt("WS-101", 101, inV1, "Chrome", 5))
	if out.PolicyVersion != 1 {
		t.Fatalf("policy version = %d, want 1", out.PolicyVersion)
	}

	// Publish v2: evening window 19:00-23:00 local. The old in-window minute is
	// now filtered, and a minute outside v1 but inside v2 is accepted.
	v2, err := e.Admin.PublishPolicy(context.Background(), admin.PolicyInput{
		MonitoringStart:   "19:00",
		MonitoringEnd:     "23:00",
		ExcludedApps:      []string{"1password*", "*vault*"},
		ExemptDepartments: []int64{4},
		PublishedBy:       "test",
	})
	if err != nil {
		t.Fatal(err)
	}
	if v2.Version != 2 {
		t.Fatalf("policy version = %d, want 2", v2.Version)
	}

	nowOutV2 := utc(2026, 9, 15, 23, 30) // 19:30 EDT -> in v2 window
	out = e.mustIngest(p101, "pv-2",
		snapAt("WS-101", 101, nowOutV2, "Chrome", 8),
		snapAt("WS-101", 101, inV1, "Chrome", 5)) // duplicate anyway
	if out.Accepted != 1 || out.Duplicates != 1 {
		t.Fatalf("v2 outcome = %+v", out)
	}

	d, _ := testsupport.GetDaily(t, e.DB, 101, "2026-09-15")
	if d.PvMin != 1 || d.PvMax != 2 {
		t.Fatalf("policy version span = [%d,%d], want [1,2]", d.PvMin, d.PvMax)
	}
	if d.TotalCount != 13 {
		t.Fatalf("total = %d, want 13", d.TotalCount)
	}
}
