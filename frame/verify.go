package frame

import "reflect"

// SplitFailure records one inconsistency between feeding the whole stream at
// once and feeding it split at Cut.
type SplitFailure struct {
	Cut      int    `json:"cut"`
	Kind     string `json:"kind"`
	Expected string `json:"expected"`
	Got      string `json:"got"`
}

// SplitReport summarizes the byte-cut consistency sweep over one stream.
type SplitReport struct {
	TotalBytes  int            `json:"total_bytes"`
	CutsChecked int            `json:"cuts_checked"`
	OK          bool           `json:"ok"`
	Failures    []SplitFailure `json:"failures,omitempty"`
}

// VerifySplits feeds data to fresh decoders for every byte cut position c in
// 0..len(data) (prefix data[:c] then suffix data[c:]) and checks two
// invariants at each cut:
//
//  1. Prefix invariance: feeding data[:c] incrementally and Closing gives the
//     same messages/error as one-shot parsing data[:c].
//  2. Reassembly invariance: feeding data[:c], then data[c:], then Close
//     gives exactly the same messages and error (code AND absolute offset) as
//     one-shot parsing the complete data.
//
// This is the "分帧一致性" guarantee: callers receive identical framing
// results regardless of TCP-segment boundaries, and error offsets stay
// anchored at absolute stream positions.
func VerifySplits(data []byte, opts ...Option) SplitReport {
	rep := SplitReport{TotalBytes: len(data), OK: true}

	refMsgs, refErr := runOneShot(data, opts)

	for c := 0; c <= len(data); c++ {
		rep.CutsChecked++

		// (1) prefix alone
		pMsgs, pErr := runOneShot(data[:c], opts)
		d := NewDecoder(opts...)
		var gotErr *Error
		if c > 0 {
			if _, e := d.Write(data[:c]); e != nil {
				gotErr = e
			}
		}
		if gotErr == nil {
			if _, e := d.Close(); e != nil {
				gotErr = e
			}
		}
		if f := compareOutcome(c, "prefix", pMsgs, pErr, d.msgs, gotErr); f != nil {
			rep.OK = false
			rep.Failures = append(rep.Failures, *f)
			continue
		}

		// (2) prefix then suffix
		d2 := NewDecoder(opts...)
		var e2 *Error
		if c > 0 {
			if _, e := d2.Write(data[:c]); e != nil {
				e2 = e
			}
		}
		if e2 == nil && c < len(data) {
			if _, e := d2.Write(data[c:]); e != nil {
				e2 = e
			}
		}
		if e2 == nil {
			if _, e := d2.Close(); e != nil {
				e2 = e
			}
		}
		if f := compareOutcome(c, "reassembly", refMsgs, refErr, d2.msgs, e2); f != nil {
			rep.OK = false
			rep.Failures = append(rep.Failures, *f)
		}
	}
	return rep
}

func runOneShot(data []byte, opts []Option) ([]*Message, *Error) {
	d := NewDecoder(opts...)
	if _, e := d.Write(data); e != nil {
		return d.msgs, e
	}
	if _, e := d.Close(); e != nil {
		return d.msgs, e
	}
	return d.msgs, nil
}

func compareOutcome(cut int, kind string, wantMsgs []*Message, wantErr *Error, gotMsgs []*Message, gotErr *Error) *SplitFailure {
	if (wantErr == nil) != (gotErr == nil) {
		return &SplitFailure{Cut: cut, Kind: kind + "_error_mismatch",
			Expected: errOrNil(wantErr), Got: errOrNil(gotErr)}
	}
	if wantErr != nil && (wantErr.Code != gotErr.Code || wantErr.Offset != gotErr.Offset) {
		return &SplitFailure{Cut: cut, Kind: kind + "_error_mismatch",
			Expected: errOrNil(wantErr), Got: errOrNil(gotErr)}
	}
	if !reflect.DeepEqual(wantMsgs, gotMsgs) {
		return &SplitFailure{Cut: cut, Kind: kind + "_message_mismatch",
			Expected: describeMsgs(wantMsgs), Got: describeMsgs(gotMsgs)}
	}
	return nil
}

func errOrNil(e *Error) string {
	if e == nil {
		return "<nil>"
	}
	return e.Error()
}

func describeMsgs(ms []*Message) string {
	s := ""
	for i, m := range ms {
		if i > 0 {
			s += "; "
		}
		s += m.Method + " " + m.Target
	}
	return s
}
