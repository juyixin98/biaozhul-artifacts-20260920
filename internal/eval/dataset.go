// Package eval builds a deterministic labeled synthetic log dataset and
// computes clustering quality metrics (pairwise precision/recall/F1 and
// cluster purity) against the labels.
package eval

import (
	"fmt"
	"math/rand"
	"strings"
)

// LabeledLine is one synthetic log line with its ground-truth template id.
type LabeledLine struct {
	Label string
	Line  string
}

// generator produces one concrete line for a true template.
type generator struct {
	label string
	gen   func(r *rand.Rand) string
}

// baseGenerators are the recurring "normal" templates. Variables cover
// numbers, UUIDs, IPs, key=value pairs and word-suffix numbers; the
// conn.timeout/conn.refused and disk.read/disk.write pairs share length and
// prefix and differ only in keywords, exercising the no-over-generalization
// guarantee.
func baseGenerators() []generator {
	num := func(r *rand.Rand, n int) int { return r.Intn(n) }
	uuid := func(r *rand.Rand) string {
		return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x",
			r.Uint32(), r.Uint32()&0xffff, r.Uint32()&0xffff,
			r.Uint32()&0xffff, r.Uint64()&0xffffffffffff)
	}
	ip := func(r *rand.Rand) string {
		return fmt.Sprintf("10.%d.%d.%d", num(r, 256), num(r, 256), num(r, 254)+1)
	}
	user := func(r *rand.Rand) string {
		names := []string{"alice", "bob", "carol", "dave", "erin"}
		return names[num(r, len(names))]
	}
	return []generator{
		{"http.get", func(r *rand.Rand) string {
			return fmt.Sprintf("GET /api/v1/orders/%d completed in %dms status=%d",
				num(r, 9000)+1000, num(r, 800)+3, 200)
		}},
		{"http.post", func(r *rand.Rand) string {
			return fmt.Sprintf("POST /api/v1/orders created id=%s latency=%dms", uuid(r), num(r, 900)+10)
		}},
		{"db.slow", func(r *rand.Rand) string {
			return fmt.Sprintf("slow query took %dms rows=%d shard=shard-%d", num(r, 5000)+100, num(r, 10000), num(r, 8))
		}},
		{"auth.fail", func(r *rand.Rand) string {
			return fmt.Sprintf("authentication failed for user=%s from %s", user(r), ip(r))
		}},
		{"conn.timeout", func(r *rand.Rand) string {
			return fmt.Sprintf("connection error: timed out after %dms", num(r, 30000)+1000)
		}},
		{"conn.refused", func(r *rand.Rand) string {
			return "connection error: refused by remote host"
		}},
		{"disk.read", func(r *rand.Rand) string {
			return fmt.Sprintf("disk sda read error at sector %d", num(r, 1<<30))
		}},
		{"disk.write", func(r *rand.Rand) string {
			return fmt.Sprintf("disk sda write error at sector %d", num(r, 1<<30))
		}},
		{"uuid.event", func(r *rand.Rand) string {
			return fmt.Sprintf("event %s processed by worker-%d", uuid(r), num(r, 16))
		}},
		{"cache.miss", func(r *rand.Rand) string {
			return fmt.Sprintf("cache miss key user_%d ttl=%ds", num(r, 100000), num(r, 3600)+60)
		}},
		{"gc.pause", func(r *rand.Rand) string {
			return fmt.Sprintf("gc pause %dms heap=%dmb", num(r, 200)+1, num(r, 4096)+512)
		}},
	}
}

// rareGenerators fire only a handful of times each.
func rareGenerators() []generator {
	return []generator{
		{"kernel.panic", func(r *rand.Rand) string {
			return fmt.Sprintf("kernel panic: unable to handle paging request at %08x", r.Uint32())
		}},
		{"cert.expiry", func(r *rand.Rand) string {
			return fmt.Sprintf("tls certificate for api.internal expires in %d days", r.Intn(30)+1)
		}},
	}
}

// longLine builds a deterministic stack-trace-like line of at least
// minBytes bytes. It does not consume randomness, so repeated calls produce
// the identical line (the two super-long samples must cluster together).
func longLine(minBytes int) string {
	var b strings.Builder
	b.WriteString("trace")
	for i := 0; b.Len() < minBytes; i++ {
		fmt.Fprintf(&b, " frame#%d:0x%08x", i, uint32(i)*2654435761)
	}
	return b.String()
}

// Dataset is the full deterministic evaluation corpus.
type Dataset struct {
	// Mixed is the baseline stream: base templates shuffled with a fixed
	// seed, plus rare patterns and two super-long lines.
	Mixed []LabeledLine
	// Bulk is the capacity-eviction stream: many distinct templates in
	// round-robin order.
	Bulk []LabeledLine
}

// Build generates the dataset. The seed is fixed, so output is identical on
// every run. bulkRounds controls the length of the eviction stream; the hot
// and cold template counts are fixed constants.
func Build(seed int64, perTemplate, bulkRounds int) *Dataset {
	r := rand.New(rand.NewSource(seed))
	d := &Dataset{}

	base := baseGenerators()
	for i := 0; i < perTemplate; i++ {
		for _, g := range base {
			d.Mixed = append(d.Mixed, LabeledLine{Label: g.label, Line: g.gen(r)})
		}
	}
	// Rare patterns: 2 occurrences each, appended before shuffling so they
	// land at deterministic positions.
	for i := 0; i < 2; i++ {
		for _, g := range rareGenerators() {
			d.Mixed = append(d.Mixed, LabeledLine{Label: g.label, Line: g.gen(r)})
		}
	}
	// Super-long lines (well above the default 64KiB line cap).
	d.Mixed = append(d.Mixed,
		LabeledLine{Label: "trace.long", Line: longLine(200 * 1024)},
		LabeledLine{Label: "trace.long", Line: longLine(200 * 1024)},
	)

	// Deterministic shuffle of the mixed stream.
	r.Shuffle(len(d.Mixed), func(i, j int) { d.Mixed[i], d.Mixed[j] = d.Mixed[j], d.Mixed[i] })

	// Eviction stream: bulkHotTemplates frequent templates repeated for
	// bulkRounds rounds (the hot set fits a small capacity), interleaved with
	// bulkColdTemplates one-off templates (each one must evict an LRU
	// template, but never a hot one seen more recently).
	for round := 0; round < bulkRounds; round++ {
		for i := 0; i < bulkHot; i++ {
			d.Bulk = append(d.Bulk, LabeledLine{
				Label: fmt.Sprintf("hot.%03d", i),
				Line:  fmt.Sprintf("bulk message hot %s payload=%d", base26Name(i), r.Intn(1000)),
			})
		}
		if round < bulkCold {
			d.Bulk = append(d.Bulk, LabeledLine{
				Label: fmt.Sprintf("cold.%03d", round),
				Line:  fmt.Sprintf("bulk message cold %s payload=%d", base26Name(round), r.Intn(1000)),
			})
		}
	}
	return d
}

const (
	bulkHot  = 10 // number of distinct hot templates
	bulkCold = 20 // number of one-off cold templates
)

// base26Name maps 0 -> a, 25 -> z, 26 -> aa ...
func base26Name(i int) string {
	var b []byte
	for {
		b = append([]byte{byte('a' + i%26)}, b...)
		i = i/26 - 1
		if i < 0 {
			break
		}
	}
	return string(b)
}
