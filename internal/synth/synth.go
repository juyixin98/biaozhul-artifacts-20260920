// Package synth produces a deterministic, labeled synthetic log corpus for
// evaluation. No randomness: every call generates byte-identical output, so
// clustering scores are reproducible across runs and machines.
package synth

import (
	"fmt"
	"strings"
)

// Sample is one labeled log line. Group is the ground-truth template ID.
type Sample struct {
	Line  string
	Group string
}

// Group describes a ground-truth template family.
type Group struct {
	ID          string
	Description string
	ExpectedTpl string // expected template after all lines have been ingested
	Count       int
}

// Dataset is the labeled corpus plus its ground truth.
type Dataset struct {
	Groups  []Group
	Samples []Sample
}

func fmtUUID(i int) string {
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x",
		i&0xffffffff, i&0xffff, (i>>16)&0xffff, (i>>32)&0xffff, i&0xffffffffffff)
}

func fmtIP(i int) string {
	return fmt.Sprintf("10.%d.%d.%d", (i*7)%256, (i*13)%256, 1+(i*17)%254)
}

func ts(i int) string {
	// 2026-09-24 with second-of-day varying over 18 minutes. A space separates
	// the date and clock so the template renders readably.
	sec := (i * 13) % 1100
	return fmt.Sprintf("2026-09-24 %02d:%02d:%02d.123", 10+sec/3600, (sec%3600)/60, sec%60)
}

// http request: UUID path segment and response bytes vary; status fixed at 200
// so the group stays a clean <NUM> exercise rather than a promotion one.
func g1Line(i int) string {
	return fmt.Sprintf("%s INFO http: GET /api/users/%s 200 %d bytes in %dms",
		ts(i), fmtUUID(i), 128+(i*37)%4000, 3+i%90)
}

// connection refused: IP and port (pure number) vary.
func g2Line(i int) string {
	return fmt.Sprintf("%s WARN db: Connection to %s:%d refused: dial tcp, retrying",
		ts(i), fmtIP(i), 5000+i%400)
}

// timeout vs refused: keyword differs, must NOT merge with G2.
func g3Line(i int) string {
	return fmt.Sprintf("%s WARN db: Connection to %s:%d timed out after %dms, giving up",
		ts(i), fmtIP(i), 5000+i%400, 500+i%2000)
}

func g4Line(i int) string {
	return fmt.Sprintf("%s INFO service %s started on port %d pid %d",
		ts(i), quoted("billing", i), 8080+i%16, 1000+i)
}

// quoted string variable: user names differ; a quoted token masks as <STR>.
func g5Line(i int) string {
	user := []string{"alice", "bob", "carol", "dave"}[i%4]
	ip := fmtIP(i + 3)
	status := []int{200, 401}[i%2]
	return fmt.Sprintf("%s INFO auth: user \"%s\" login from %s status %d", ts(i), user, ip, status)
}

func quotedName(i int) string {
	return []string{"billing", "payments", "search", "ingest"}[i%4]
}

func quoted(_ string, i int) string {
	// quoted string renders as one <STR> variable regardless of its contents.
	return `"` + quotedName(i) + `"`
}

// word parameter: task ids "task101/task102/task103" are digit-bearing words
// (not quotes, not plain numbers). The first id seeds the template as a
// literal; the second distinct id heals that slot to <*> (template v2); later
// ids match <*> directly. This exercises template versioning via promotion.
func g6Line(i int) string {
	id := []string{"task101", "task102", "task103"}[i%3]
	return fmt.Sprintf("%s INFO job: scheduled task %s heartbeat ok after %dms (worker %d)",
		ts(i), id, 5+i%20, 1+i%7)
}

// disk usage: pure number and percentage.
func g7Line(i int) string {
	return fmt.Sprintf("%s ERROR disk: /var partition usage at %d%%, %d MB free",
		ts(i), 71+i%20, 20000-(i*97)%9000)
}

// ultra-long line: a bounded number of repeating tokens; must be flagged
// truncated and still cluster into one template.
func g8Line(i int) string {
	var b strings.Builder
	fmt.Fprintf(&b, "%s ERROR batch: payload %d items ", ts(i), 100+i%50)
	for k := 0; k < 2400; k++ {
		if k > 0 {
			b.WriteByte(' ')
		}
		fmt.Fprintf(&b, "field%d=%d", k, (i+k)%97)
	}
	return b.String()
}

// panic: hex address varies, error type is a keyword that stays literal.
func g9Line(i int) string {
	return fmt.Sprintf("%s FATAL kernel: segmentation fault at address 0x%08x in module driver",
		ts(i), uint32(i*2654435761))
}

// keyword-different errors: NullPointerException vs IllegalArgumentException.
// Both are pure-alphabetic keywords, so by design they stay in SEPARATE
// templates and are labeled as separate ground-truth groups.
func g10aLine(i int) string {
	return fmt.Sprintf("%s ERROR app: uncaught NullPointerException while processing request %d",
		ts(i), i)
}

func g10bLine(i int) string {
	return fmt.Sprintf("%s ERROR app: uncaught IllegalArgumentException while processing request %d",
		ts(i), i)
}

// numeric-distinct error codes with the same words: the two codes must not
// collapse into one template (1001 vs 2002 both <NUM> — wait: both are numeric,
// so masking *does* merge them; that is the correct behavior for numbers).
// Instead use a digit-bearing code WORD that differs in prefix: E1001/E2002
// are one-letter + digits, hence tokenized as words. They appear in separate
// sentences ("payment failed" vs "validation failed") so the keyword keeps
// the templates apart regardless.
func g11Line(i int) string {
	return fmt.Sprintf("%s ERROR app: payment failed with code E1001 for transaction %d",
		ts(i), 1000+i)
}

func g12Line(i int) string {
	return fmt.Sprintf("%s ERROR app: validation failed with code E2002 for form %d",
		ts(i), 4000+i)
}

// rare, one-of-a-kind line: remains a singleton template.
func g13Line(i int) string {
	return fmt.Sprintf("%s FATAL app: unreachable state reached in coordinator node %d",
		ts(i), 7+i%3)
}

// Build returns the interleaved, labeled corpus.
func Build() Dataset {
	type spec struct {
		g Group
		f func(int) string
	}
	specs := []spec{
		{Group{"G1-http", "HTTP request logs with UUID path vars", "", 40}, g1Line},
		{Group{"G2-refused", "Connection refused errors", "", 30}, g2Line},
		{Group{"G3-timeout", "Connection timeout errors (keyword differs from G2)", "", 24}, g3Line},
		{Group{"G4-startup", "Service startup logs with quoted service name", "", 12}, g4Line},
		{Group{"G5-auth", "Auth login logs with quoted user variable", "", 20}, g5Line},
		{Group{"G6-wordparam", "Digit-bearing word parameter that promotes to <*>", "", 15}, g6Line},
		{Group{"G7-disk", "Disk usage alerts", "", 18}, g7Line},
		{Group{"G8-long", "Ultra-long lines beyond the token cap", "", 6}, g8Line},
		{Group{"G9-panic", "Kernel panics with hex address", "", 10}, g9Line},
		{Group{"G10a-npe", "NullPointerException errors (keyword-specific)", "", 8}, g10aLine},
		{Group{"G10b-iae", "IllegalArgumentException errors (keyword-specific, never merges with G10a)", "", 8}, g10bLine},
		{Group{"G11-paycode", "Payment error E1001 (keyword-specific)", "", 8}, g11Line},
		{Group{"G12-valcode", "Validation error E2002 (keyword-specific)", "", 8}, g12Line},
		{Group{"G13-rare", "Rare one-off fatal pattern", "", 1}, g13Line},
	}

	type work struct {
		id string
		f  func(int) string
		i  int
		n  int
	}
	queues := make([]work, len(specs))
	groups := make([]Group, len(specs))
	total := 0
	for qi, s := range specs {
		groups[qi] = s.g
		queues[qi] = work{id: s.g.ID, f: s.f, n: s.g.Count}
		total += s.g.Count
	}

	samples := make([]Sample, 0, total)
	for progressed := true; progressed; {
		progressed = false
		for qi := range queues {
			if queues[qi].i >= queues[qi].n {
				continue
			}
			samples = append(samples, Sample{
				Line:  queues[qi].f(queues[qi].i),
				Group: queues[qi].id,
			})
			queues[qi].i++
			progressed = true
		}
	}

	return Dataset{Groups: groups, Samples: samples}
}
