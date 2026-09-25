package cluster

import (
	"strings"
	"testing"
)

func TestMaskToken(t *testing.T) {
	cases := map[string]string{
		"550e8400-e29b-41d4-a716-446655440000": VarUUID,
		"192.168.0.1":                          VarIP,
		"10.0.0.1:8080":                        VarIP,
		"0xdeadbeef":                           VarHex,
		"deadbeef12":                           VarHex,
		"abcdefgh":                             "abcdefgh", // no digit -> literal word
		"42":                                   VarNum,
		"-3.14":                                VarNum,
		"120ms":                                VarNum,
		"99%":                                  VarNum,
		"1,024":                                VarNum,
		"status=200":                           "status=<VAL>",
		"user=alice":                           "user=<VAL>",
		"worker-7":                             "worker-<NUM>",
		"node_12":                              "node_<NUM>",
		`"some quoted`:                         VarStr,
		"/api/v1/orders/1000":                  "/api/v1/orders/<NUM>",
		"/api/v1/orders":                       "/api/v1/orders",
		"timeout":                              "timeout",
	}
	for in, want := range cases {
		if got := MaskToken(in); got != want {
			t.Errorf("MaskToken(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestTokenizeTruncation(t *testing.T) {
	long := strings.Repeat("frame#1:0xabc ", 100) // 1400 bytes, 100 tokens

	// Byte limit: the surviving token count depends on the byte budget, but
	// the <TRUNC> marker must be present.
	toks := Tokenize(long, 64, DefaultMaxTokens)
	if toks[len(toks)-1] != VarTruncated {
		t.Fatalf("byte truncation missing marker: %v", toks)
	}
	if len(Join(toks)) > 64+len(VarTruncated)+1 {
		t.Fatal("byte-truncated tokens exceed budget")
	}

	// Token limit: generous byte budget, cap at 10 tokens.
	toks = Tokenize(long, DefaultMaxLineBytes, 10)
	if len(toks) != 11 {
		t.Fatalf("expected 11 tokens (10 + TRUNC), got %d", len(toks))
	}
	if toks[len(toks)-1] != VarTruncated {
		t.Fatalf("expected trailing %s, got %s", VarTruncated, toks[len(toks)-1])
	}
	// Deterministic: same input, same output.
	again := Tokenize(long, DefaultMaxLineBytes, 10)
	if Join(toks) != Join(again) {
		t.Fatal("tokenize not deterministic")
	}
}

func TestTokenizeVeryLongLine(t *testing.T) {
	line := "trace " + strings.Repeat("x", 5*1024*1024) // 5MB single token
	toks := Tokenize(line, DefaultMaxLineBytes, DefaultMaxTokens)
	if toks[len(toks)-1] != VarTruncated {
		t.Fatal("expected <TRUNC> marker")
	}
	if len(Join(toks)) > DefaultMaxLineBytes+len(VarTruncated)+8 {
		t.Fatal("tokenized output exceeds line budget")
	}
}

func TestSameTemplateDifferentNumbers(t *testing.T) {
	c := New(Config{})
	a1, _ := c.Ingest("request completed in 12ms status=200")
	a2, _ := c.Ingest("request completed in 900ms status=200")
	if a1.TemplateID != a2.TemplateID {
		t.Fatalf("number variation should share a template: %d vs %d", a1.TemplateID, a2.TemplateID)
	}
	tmpl, _ := c.Get(a1.TemplateID)
	if tmpl.Count != 2 {
		t.Fatalf("count = %d, want 2", tmpl.Count)
	}
	if tmpl.Version != 1 {
		t.Fatalf("pure variable reuse must not bump version, got v%d", tmpl.Version)
	}
}

func TestKeywordDifferencesNeverMerge(t *testing.T) {
	c := New(Config{})
	read, _ := c.Ingest("disk sda read error at sector 100")
	write, _ := c.Ingest("disk sda write error at sector 200")
	if read.TemplateID == write.TemplateID {
		t.Fatal("read/write keyword difference must not merge")
	}
	to, _ := c.Ingest("connection error: timed out after 1000ms")
	rf, _ := c.Ingest("connection error: refused by remote host")
	if to.TemplateID == rf.TemplateID {
		t.Fatal("timeout/refused keyword difference must not merge")
	}
	if got := c.Stats().Templates; got != 4 {
		t.Fatalf("expected 4 templates, got %d", got)
	}
}

func TestVersionBumpOnGeneralization(t *testing.T) {
	c := New(Config{})
	a1, _ := c.Ingest("lookup key 12345 done")
	if !a1.Created || a1.Version != 1 {
		t.Fatalf("first line should create v1, got %+v", a1)
	}
	// Same shape, but a UUID where a NUM was: different var types generalize
	// the position to a wildcard and bump the version.
	a2, _ := c.Ingest("lookup key 550e8400-e29b-41d4-a716-446655440000 done")
	if a2.TemplateID != a1.TemplateID {
		t.Fatal("var-type change should generalize, not split")
	}
	if a2.Version != 2 {
		t.Fatalf("expected v2 after generalization, got v%d", a2.Version)
	}
	tmpl, _ := c.Get(a1.TemplateID)
	if !strings.Contains(tmpl.Pattern, Wildcard) {
		t.Fatalf("expected wildcard in pattern, got %q", tmpl.Pattern)
	}
	if len(tmpl.History) != 2 {
		t.Fatalf("expected 2 history records, got %d", len(tmpl.History))
	}
}

func TestWildcardRatioGuard(t *testing.T) {
	c := New(Config{MaxWildRatio: 0.4})
	c.Ingest("alpha bravo charlie delta echo")
	// Four of five positions would need to wildcard: ratio 0.8 > 0.4.
	a, _ := c.Ingest("alpha 1 2 3 4")
	if !a.Created {
		t.Fatal("over-generalizing merge should have been rejected")
	}
	if c.Stats().Templates != 2 {
		t.Fatalf("expected 2 templates, got %d", c.Stats().Templates)
	}
}

func TestEvictionLRU(t *testing.T) {
	c := New(Config{MaxTemplates: 2})
	a1, _ := c.Ingest("first template here")
	c.Ingest("second template here")
	c.Ingest("third template here") // evicts #1 (oldest LastSeq)
	st := c.Stats()
	if st.Templates != 2 || st.Evictions != 1 {
		t.Fatalf("stats = %+v, want 2 templates 1 eviction", st)
	}
	if _, ok := c.Get(a1.TemplateID); ok {
		t.Fatal("template #1 should have been evicted")
	}
	// Re-ingesting the evicted pattern creates a fresh template id.
	a4, _ := c.Ingest("first template here")
	if !a4.Created || a4.TemplateID == a1.TemplateID {
		t.Fatalf("evicted pattern should re-create, got %+v", a4)
	}
}

func TestEvictionDeterministicTieBreak(t *testing.T) {
	build := func() []Eviction {
		c := New(Config{MaxTemplates: 3})
		lines := []string{
			"t one a", "t two b", "t three c", "t four d", "t five e",
		}
		for _, l := range lines {
			c.Ingest(l)
		}
		return c.Evictions()
	}
	e1, e2 := build(), build()
	if len(e1) != 2 || len(e2) != 2 {
		t.Fatalf("expected 2 evictions, got %d/%d", len(e1), len(e2))
	}
	for i := range e1 {
		if e1[i] != e2[i] {
			t.Fatal("eviction order not deterministic")
		}
	}
	if e1[0].TemplateID != 1 || e1[1].TemplateID != 2 {
		t.Fatalf("expected eviction of ids 1,2 got %+v", e1)
	}
}

func TestEmptyLine(t *testing.T) {
	c := New(Config{})
	if _, err := c.Ingest("   "); err != ErrEmptyLine {
		t.Fatalf("expected ErrEmptyLine, got %v", err)
	}
}

func TestSnapshotRoundTrip(t *testing.T) {
	c := New(Config{MaxTemplates: 4})
	lines := []string{
		"alpha value 1", "alpha value 2", "beta other thing",
		"gamma more stuff", "delta last one", "epsilon overflow",
	}
	for _, l := range lines {
		c.Ingest(l)
	}
	snap := c.Snapshot()
	restored := Restore(Config{MaxTemplates: 4}, snap)
	if restored.Stats() != c.Stats() {
		t.Fatalf("stats differ after restore: %+v vs %+v", restored.Stats(), c.Stats())
	}
	// Clustering continues consistently after restore.
	a1, _ := restored.Ingest("alpha value 99")
	a2, _ := c.Ingest("alpha value 99")
	if a1 != a2 {
		t.Fatalf("divergent assignment after restore: %+v vs %+v", a1, a2)
	}
}
