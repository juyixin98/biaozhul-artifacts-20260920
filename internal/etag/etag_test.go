package etag

import "testing"

func TestParseStrongAndWeak(t *testing.T) {
	strong, err := Parse(`"res-1-v3"`)
	if err != nil {
		t.Fatalf("parse strong: %v", err)
	}
	if strong.Weak || strong.Value != "res-1-v3" {
		t.Fatalf("unexpected strong tag: %+v", strong)
	}

	weak, err := Parse(`W/"res-1-v3"`)
	if err != nil {
		t.Fatalf("parse weak: %v", err)
	}
	if !weak.Weak || weak.Value != "res-1-v3" {
		t.Fatalf("unexpected weak tag: %+v", weak)
	}
	if weak.String() != `W/"res-1-v3"` {
		t.Fatalf("weak render: %s", weak.String())
	}
}

func TestParseRejectsMalformed(t *testing.T) {
	for _, bad := range []string{"", `abc`, `"`, `""`, `W/"`} {
		if _, err := Parse(bad); err == nil {
			t.Fatalf("expected error for %q", bad)
		}
	}
}

func TestStrongMatchRejectsWeakTags(t *testing.T) {
	strong := ETag{Value: "x"}
	weak := ETag{Value: "x", Weak: true}

	if !StrongMatch(strong, strong) {
		t.Fatal("identical strong tags must match")
	}
	if StrongMatch(weak, strong) {
		t.Fatal("weak tag must not satisfy strong comparison (left)")
	}
	if StrongMatch(strong, weak) {
		t.Fatal("weak tag must not satisfy strong comparison (right)")
	}
	if StrongMatch(weak, weak) {
		t.Fatal("two weak tags must not satisfy strong comparison")
	}
}

func TestConditionStar(t *testing.T) {
	c, err := ParseCondition("*")
	if err != nil {
		t.Fatalf("parse star: %v", err)
	}
	if !c.Star {
		t.Fatal("expected star condition")
	}
	if !c.StronglyMatchesAny(ETag{Value: "anything"}, true) {
		t.Fatal("star must match an existing resource")
	}
	if c.StronglyMatchesAny(ETag{}, false) {
		t.Fatal("star must not match a missing resource")
	}
}

func TestConditionListWithWeakEntry(t *testing.T) {
	c, err := ParseCondition(`"other", W/"res-1-v3"`)
	if err != nil {
		t.Fatalf("parse list: %v", err)
	}
	current := ETag{Value: "res-1-v3"}
	if c.StronglyMatchesAny(current, true) {
		t.Fatal("weak entry in If-Match list must not match strongly")
	}
	if !c.WeaklyMatchesAny(current, true) {
		t.Fatal("weak entry must match under weak comparison (If-None-Match)")
	}
	if c.WeaklyMatchesAny(current, false) {
		t.Fatal("weak comparison must fail for a missing resource")
	}
}
