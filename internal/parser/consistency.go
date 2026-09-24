package parser

import (
	"fmt"
	"math/rand"
)

// SplitMismatch describes a difference between the one-shot parse and the
// streaming parse for some byte split of the same stream.
type SplitMismatch struct {
	// SplitAt is the number of bytes in the first piece of a two-way split.
	SplitAt int `json:"split_at"`
	// Where describes the particular comparison that failed.
	Where  string `json:"where"`
	Detail string `json:"detail"`
}

func (m *SplitMismatch) Error() string {
	return fmt.Sprintf("split at %d: %s: %s", m.SplitAt, m.Where, m.Detail)
}

type snapshot struct {
	reqs []*Request
	err  *Error
}

func runSplit(input []byte, cuts []int, opts []Option) snapshot {
	p := NewParser(opts...)
	var got []*Request
	prev := 0
	for _, c := range cuts {
		rs, e := p.Feed(input[prev:c])
		got = append(got, rs...)
		if e != nil {
			return snapshot{got, e}
		}
		prev = c
	}
	rs, e := p.Feed(input[prev:])
	got = append(got, rs...)
	if e == nil {
		e = p.Finish()
	}
	return snapshot{got, e}
}

func snapshotOpts(opts []Option) []Option { return opts }

func compareSnapshots(ref, got snapshot, splitAt int) *SplitMismatch {
	if (ref.err == nil) != (got.err == nil) {
		return &SplitMismatch{
			SplitAt: splitAt,
			Where:   "error presence",
			Detail:  fmt.Sprintf("one-shot err=%v, split err=%v", ref.err, got.err),
		}
	}
	if ref.err != nil && got.err != nil {
		if ref.err.Code != got.err.Code || ref.err.Offset != got.err.Offset {
			return &SplitMismatch{
				SplitAt: splitAt,
				Where:   "error code/offset",
				Detail:  fmt.Sprintf("one-shot %v, split %v", ref.err, got.err),
			}
		}
	}
	if len(ref.reqs) != len(got.reqs) {
		return &SplitMismatch{
			SplitAt: splitAt,
			Where:   "request count",
			Detail:  fmt.Sprintf("one-shot %d requests, split %d", len(ref.reqs), len(got.reqs)),
		}
	}
	for i := range ref.reqs {
		if diff := compareRequest(ref.reqs[i], got.reqs[i]); diff != "" {
			return &SplitMismatch{
				SplitAt: splitAt,
				Where:   fmt.Sprintf("request[%d] %s", i, diff),
				Detail: fmt.Sprintf("one-shot=%+v split=%+v",
					summarize(ref.reqs[i]), summarize(got.reqs[i])),
			}
		}
	}
	return nil
}

func summarize(r *Request) string {
	return fmt.Sprintf("{method=%s target=%s ver=%s frame=%s cl=%d bodyLen=%d rawLen=%d start=%d headers=%d}",
		r.Method, r.Target, r.Version, r.Frame, r.ContentLength, r.BodyLength, r.RawLength,
		r.StartOffset, len(r.Headers))
}

func compareRequest(a, b *Request) string {
	if a.Method != b.Method || a.Target != b.Target || a.Version != b.Version {
		return "request line"
	}
	if a.Frame != b.Frame || a.ContentLength != b.ContentLength {
		return "framing"
	}
	if string(a.Body) != string(b.Body) {
		return "body"
	}
	if a.BodyLength != b.BodyLength || a.RawLength != b.RawLength || a.StartOffset != b.StartOffset {
		return "offsets/lengths"
	}
	if len(a.Headers) != len(b.Headers) {
		return "header count"
	}
	for i := range a.Headers {
		if a.Headers[i] != b.Headers[i] {
			return "header field"
		}
	}
	if len(a.TrailingHeader) != len(b.TrailingHeader) {
		return "trailer count"
	}
	for i := range a.TrailingHeader {
		if a.TrailingHeader[i] != b.TrailingHeader[i] {
			return "trailer field"
		}
	}
	return ""
}

// VerifyAllSplits feeds the input to the streaming parser as every possible
// two-way split:
//
//	"" | input, input[0:1] | input[1:], input[0:2] | input[2:], ..., input | ""
//
// and compares each result with the one-shot Parse result. It returns the
// first mismatch found, or nil if framing is consistent at every boundary.
//
// It runs O(n) parses, so callers should bound the input length (the HTTP
// service caps it at 4 KiB).
func VerifyAllSplits(input []byte, opts ...Option) *SplitMismatch {
	opts = snapshotOpts(opts)
	ref := runSplit(input, nil, opts) // whole input in a single Feed
	for cut := 0; cut <= len(input); cut++ {
		got := runSplit(input, []int{cut}, opts)
		if m := compareSnapshots(ref, got, cut); m != nil {
			return m
		}
	}
	return nil
}

// VerifyRandomSplits performs `iterations` random multi-cut parses using a
// deterministic RNG seeded with seed. Useful for inputs too large to check
// exhaustively.
func VerifyRandomSplits(input []byte, seed int64, iterations int, opts ...Option) *SplitMismatch {
	rng := rand.New(rand.NewSource(seed))
	ref := runSplit(input, nil, opts)
	for it := 0; it < iterations; it++ {
		var cuts []int
		// Each interior byte boundary independently becomes a cut.
		for i := 1; i < len(input); i++ {
			if rng.Intn(2) == 0 {
				cuts = append(cuts, i)
			}
		}
		got := runSplit(input, cuts, opts)
		if m := compareSnapshots(ref, got, len(cuts)); m != nil {
			m.Where = fmt.Sprintf("random-split(iter=%d,cuts=%d) %s", it, len(cuts), m.Where)
			return m
		}
	}
	return nil
}
