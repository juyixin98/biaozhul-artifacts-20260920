package cluster

import (
	"testing"
	"time"

	"logcluster/internal/tokenize"
)

func mk(line string) []tokenize.Slot { return tokenize.Line(line).Slots }

var epoch = time.Date(2026, 9, 24, 10, 0, 0, 0, time.UTC)

func TestExactMatchAndVersion(t *testing.T) {
	c := New(1, mk("user 5 logged in"), epoch)
	if !c.Matches(mk("user 9 logged in")) {
		t.Fatal("number variants should match")
	}
	if c.Version != 1 {
		t.Fatalf("version = %d, want 1", c.Version)
	}
}

func TestKeywordMismatchDoesNotMatch(t *testing.T) {
	c := New(1, mk("connection refused to host"), epoch)
	if c.Matches(mk("connection timeout to host")) {
		t.Fatal("different keywords must not match the same template")
	}
}

func TestTypeMismatchUUIDVsNumber(t *testing.T) {
	c := New(1, mk("id 5 done"), epoch)
	if c.Matches(mk("id 550e8400-e29b-41d4-a716-446655440000 done")) {
		t.Fatal("UUID at a <NUM> position must not match")
	}
	c2 := New(2, mk("id 550e8400-e29b-41d4-a716-446655440000 done"), epoch)
	if !c2.Matches(mk("id 660e8400-e29b-41d4-a716-446655440000 done")) {
		t.Fatal("UUID at a <UUID> position must match")
	}
}

func TestLengthMismatch(t *testing.T) {
	c := New(1, mk("a b c"), epoch)
	if c.Matches(mk("a b")) {
		t.Fatal("different slot counts must not match")
	}
}

func TestNearMissPromotionVersions(t *testing.T) {
	c := New(1, mk("task task101 ok"), epoch)
	toks2 := mk("task task102 ok")
	mm, sameLen := c.MatchMismatches(toks2)
	if !sameLen || len(mm) != 1 {
		t.Fatalf("want exactly one mismatching position, got %v (sameLen=%v)", mm, sameLen)
	}
	pos := mm[0]
	if !c.PromotableAt(pos, toks2) {
		t.Fatal("task101/task102 mismatch should be healable")
	}
	c.Absorb("task task102 ok", toks2, epoch.Add(time.Second), pos)
	if c.Version != 2 {
		t.Fatalf("version = %d, want 2", c.Version)
	}
	if len(c.Versions) != 2 {
		t.Fatalf("versions history len = %d, want 2", len(c.Versions))
	}
	if c.Versions[1].Number != 2 || c.Versions[1].Reason == "" {
		t.Fatalf("bad version entry: %+v", c.Versions[1])
	}
	// After promotion, a third distinct word matches via <*>.
	if !c.Matches(mk("task task999 ok")) {
		t.Fatal("promoted <*> slot should accept any word")
	}
	// A pure keyword difference at a different slot still does not match.
	if c.Matches(mk("task task103 fail")) {
		t.Fatal("keyword ok/fail mismatch must not match")
	}
}

func TestNearMissNotPromotableForKeywords(t *testing.T) {
	c := New(1, mk("connection refused now"), epoch)
	toks := mk("connection timeout now")
	mm, _ := c.MatchMismatches(toks)
	if len(mm) != 1 {
		t.Fatalf("want 1 mismatch, got %v", mm)
	}
	if c.PromotableAt(mm[0], toks) {
		t.Fatal("pure keywords must not be promotable")
	}
}

func TestSamplesBounded(t *testing.T) {
	c := New(1, mk("line 1"), epoch)
	for i := 0; i < 10; i++ {
		c.Absorb("line "+itoa(i), mk("line "+itoa(i)), epoch.Add(time.Duration(i)*time.Second), -1)
	}
	if len(c.Samples) != MaxSamples {
		t.Fatalf("samples len = %d, want cap %d", len(c.Samples), MaxSamples)
	}
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var b []byte
	for i > 0 {
		b = append([]byte{byte('0' + i%10)}, b...)
		i /= 10
	}
	return string(b)
}
