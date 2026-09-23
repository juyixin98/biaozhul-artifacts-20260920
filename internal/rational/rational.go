// Package rational provides exact rational arithmetic for rates and token
// amounts, so the token bucket never accumulates floating-point drift.
//
// Every public numeric type in the limiter reduces to a fraction of int64s:
// rates like "1 token / 3 seconds" or "0.5 tokens per second" are represented
// exactly instead of as a float64 approximation.
package rational

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"strconv"
	"strings"

	"tokenbudget/internal/u128"
)

// Rate is tokens per second expressed as the reduced fraction Num/Den
// (Den > 0). A zero Rate means no refill.
type Rate struct {
	Num int64 // tokens per Den seconds...
	Den int64 // ...see TokensPerSecond for the unit: tokens/sec = Num/Den
}

// PerSecond returns a rate of n tokens per second.
func PerSecond(n int64) Rate { return Rate{Num: n, Den: 1} }

// Over returns n tokens over d seconds. d <= 0 is invalid; callers should
// validate before constructing.
func Over(n, d int64) (Rate, error) {
	if d <= 0 {
		return Rate{}, errors.New("rational: rate interval must be positive")
	}
	return reduce(n, d), nil
}

// TokensPerSecond returns num/den tokens per second.
func TokensPerSecond(num, den int64) (Rate, error) {
	if den <= 0 {
		return Rate{}, errors.New("rational: denominator must be positive")
	}
	if num < 0 {
		return Rate{}, errors.New("rational: rate must be non-negative")
	}
	return reduce(num, den), nil
}

func reduce(num, den int64) Rate {
	if num == 0 {
		return Rate{Num: 0, Den: 1}
	}
	g := gcd(abs64(num), den)
	return Rate{Num: num / g, Den: den / g}
}

func gcd(a, b int64) int64 {
	for b != 0 {
		a, b = b, a%b
	}
	return a
}

func abs64(x int64) int64 {
	if x < 0 {
		return -x
	}
	return x
}

// IsZero reports whether no tokens are ever produced.
func (r Rate) IsZero() bool { return r.Num == 0 }

// MulNanos returns r * dt where dt is a duration in nanoseconds, expressed in
// microtokens (1e-6 token units). The result is truncated; the sub-microtoken
// remainder, in units of 1/(r.Den*1000) of a microtoken, is returned with
// the divisor it was computed against so callers can carry it across refills.
func (r Rate) MulNanos(dtNanos int64) (microtokens int64, remNum, remDen uint64, ok bool) {
	// tokens = (r.Num / r.Den) tokens/sec * dt_ns * 1e-9 s/ns
	// microtokens = tokens * 1e6
	//             = r.Num * dt_ns / (r.Den * 1_000)
	den := uint64(r.Den) * 1_000
	num128 := u128.Mul64(uint64(r.Num), uint64(dtNanos))
	q128, rem128 := u128.DivU64(num128, den)
	if !q128.FitsInt64() || !rem128.FitsInt64() {
		return 0, 0, 0, false
	}
	return int64(q128.Lo), rem128.Lo, den, true
}

// String renders the rate.
func (r Rate) String() string {
	if r.Den == 1 {
		return fmt.Sprintf("%d/s", r.Num)
	}
	return fmt.Sprintf("%d/%d per sec", r.Num, r.Den)
}

// MarshalJSON renders reduced rates as stable exact text.
func (r Rate) MarshalJSON() ([]byte, error) {
	if r.Den == 1 {
		return json.Marshal(fmt.Sprintf("%d/s", r.Num))
	}
	return json.Marshal(fmt.Sprintf("%d/%s", r.Num, rateDenText(r.Den)))
}

func rateDenText(den int64) string {
	// den is "seconds in denominator" encoded as per-second fraction; the
	// bucket stores everything normalized to per-second, so render "1/3"
	// meaning one third of a token per second.
	return fmt.Sprintf("%ds", den)
}

// UnmarshalJSON implements exact rate parsing. JSON numbers with fractional
// digits are rejected: "0.1" in a float is 0.1000000000000000055..., so callers
// must send "0.1" as a string (parched as an exact decimal fraction) or as an
// explicit numerator/denominator pair.
func (r *Rate) UnmarshalJSON(data []byte) error {
	// String form: "10", "0.5", "10/s", "1/3".
	if len(data) > 0 && data[0] == '"' {
		var s string
		if err := json.Unmarshal(data, &s); err != nil {
			return err
		}
		parsed, err := ParseRate(s)
		if err != nil {
			return err
		}
		*r = parsed
		return nil
	}

	// Object form.
	var obj struct {
		Tokens     *int64 `json:"tokens"`
		PerSeconds *int64 `json:"per_seconds"`
		Num        *int64 `json:"num"`
		Den        *int64 `json:"den"`
	}
	dec := json.NewDecoder(strings.NewReader(string(data)))
	if err := dec.Decode(&obj); err != nil {
		return fmt.Errorf("rate must be a string or object, not a fractional JSON number: %w", err)
	}
	switch {
	case obj.Tokens != nil && obj.PerSeconds != nil:
		rr, err := TokensPerSecond(*obj.Tokens, *obj.PerSeconds)
		if err != nil {
			return err
		}
		*r = rr
		return nil
	case obj.Num != nil && obj.Den != nil:
		rr, err := TokensPerSecond(*obj.Num, *obj.Den)
		if err != nil {
			return err
		}
		*r = rr
		return nil
	default:
		return errors.New(`rate object requires {"tokens": n, "per_seconds": d}`)
	}
}

// ParseRate parses exact rate text:
//
//	"10"     -> 10 tokens/sec
//	"10/s"   -> 10 tokens/sec
//	"0.5"    -> 1 token / 2 sec
//	"1/3"    -> 1 token / 3 sec
//	"1 token / 3 seconds" style is intentionally not supported; keep it tight.
func ParseRate(s string) (Rate, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		return Rate{}, errors.New("rational: empty rate")
	}
	s = strings.TrimSuffix(s, "/s")
	s = strings.TrimSuffix(s, "s")
	if i := strings.IndexByte(s, '/'); i >= 0 {
		num, err := strconv.ParseInt(strings.TrimSpace(s[:i]), 10, 64)
		if err != nil {
			return Rate{}, fmt.Errorf("rational: bad rate numerator %q", s)
		}
		den, err := strconv.ParseInt(strings.TrimSpace(s[i+1:]), 10, 64)
		if err != nil {
			return Rate{}, fmt.Errorf("rational: bad rate denominator %q", s)
		}
		return TokensPerSecond(num, den)
	}
	n, frac, err := parseDecimal(s)
	if err != nil {
		return Rate{}, err
	}
	if frac == "" {
		if n < 0 {
			return Rate{}, errors.New("rational: rate must be non-negative")
		}
		return reduce(n, 1), nil
	}
	den := int64(math.Pow10(len(frac)))
	num := n * den
	if fracVal, err := strconv.ParseInt(frac, 10, 64); err == nil {
		if n < 0 {
			num -= fracVal
		} else {
			num += fracVal
		}
	}
	return reduce(num, den), nil
}

// ParseDecimalTokens parses an exact decimal token amount ("3", "0.25") and
// returns it in microtoken units (1e6 per token). Fractional digits beyond
// microtoken precision are rejected rather than silently rounded.
func ParseDecimalTokens(s string) (int64, error) {
	s = strings.TrimSpace(s)
	n, frac, err := parseDecimal(s)
	if err != nil {
		return 0, err
	}
	if len(frac) > 6 {
		return 0, fmt.Errorf("rational: token amount %q has more than 6 fractional digits", s)
	}
	micro := n * 1_000_000
	if frac != "" {
		f, err := strconv.ParseInt(frac, 10, 64)
		if err != nil {
			return 0, fmt.Errorf("rational: bad decimal %q", s)
		}
		for i := len(frac); i < 6; i++ {
			f *= 10
		}
		if micro < 0 {
			micro -= f
		} else {
			micro += f
		}
	}
	return micro, nil
}

// FormatMicroTokens renders microtoken units as an exact decimal string with
// up to 6 fractional digits, trimming trailing zeros.
func FormatMicroTokens(micro int64) string {
	neg := micro < 0
	if neg {
		micro = -micro
	}
	whole := micro / 1_000_000
	frac := micro % 1_000_000
	s := strconv.FormatInt(whole, 10)
	if frac != 0 {
		f := fmt.Sprintf("%06d", frac)
		f = strings.TrimRight(f, "0")
		s += "." + f
	}
	if neg {
		s = "-" + s
	}
	return s
}

func parseDecimal(s string) (whole int64, frac string, err error) {
	if s == "" {
		return 0, "", errors.New("rational: empty decimal")
	}
	neg := false
	if s[0] == '-' {
		neg = true
		s = s[1:]
	}
	if s == "" {
		return 0, "", errors.New("rational: invalid number")
	}
	if i := strings.IndexByte(s, '.'); i >= 0 {
		whole, err = strconv.ParseInt(s[:i], 10, 64)
		if err != nil {
			return 0, "", fmt.Errorf("rational: bad number %q", s)
		}
		frac = s[i+1:]
		if frac == "" {
			return 0, "", fmt.Errorf("rational: bad number %q", s)
		}
		if _, err := strconv.ParseInt(frac, 10, 64); err != nil {
			return 0, "", fmt.Errorf("rational: bad number %q", s)
		}
	} else {
		whole, err = strconv.ParseInt(s, 10, 64)
		if err != nil {
			return 0, "", fmt.Errorf("rational: bad number %q", s)
		}
	}
	if neg {
		whole = -whole
	}
	return whole, frac, nil
}
