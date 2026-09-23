package twap

import (
	"crypto/sha256"
	"encoding/hex"
	"sort"
	"strconv"
	"strings"

	"twap-service/internal/domain"
)

// CanonicalInputs is the deterministically-serialized content that pins a
// window version. Time-dependent fields (asOf, computed-at) are deliberately
// excluded: once a window is complete its content never changes unless a late
// sample arrives, and that is exactly when a new version is required.
type CanonicalInputs struct {
	Symbol    string
	WindowSec int64
	StartUnix int64
	EndUnix   int64
	// Effective events that influenced or conflicted in the window, in ts
	// order, as they resolve at computation time.
	Events []domain.EffectiveSample
	// Result fields.
	Integral        string // base10 price*usec
	CoveredUsec     int64
	TWAP            string // exact "num/den", or "0/0"
	TWAPRounded     string
	Coverage        string
	Stale           bool
	StaleHorizonSec int64
}

// CanonicalHash returns the hex SHA-256 over a canonical line-oriented
// encoding. The encoding is versioned ("v1|") so a future semantics change
// cannot silently collide with old versions.
func CanonicalHash(in CanonicalInputs) string {
	evs := make([]domain.EffectiveSample, len(in.Events))
	copy(evs, in.Events)
	sort.SliceStable(evs, func(i, j int) bool { return evs[i].TS < evs[j].TS })

	var b strings.Builder
	b.WriteString("v1|")
	b.WriteString("symbol=")
	b.WriteString(in.Symbol)
	b.WriteString("|window_sec=")
	b.WriteString(strconv.FormatInt(in.WindowSec, 10))
	b.WriteString("|start=")
	b.WriteString(strconv.FormatInt(in.StartUnix, 10))
	b.WriteString("|end=")
	b.WriteString(strconv.FormatInt(in.EndUnix, 10))
	b.WriteString("|horizon=")
	b.WriteString(strconv.FormatInt(in.StaleHorizonSec, 10))
	b.WriteString("|events=")
	for i, e := range evs {
		if i > 0 {
			b.WriteString(";")
		}
		b.WriteString(strconv.FormatInt(e.TS, 10))
		b.WriteString(",")
		b.WriteString(strconv.FormatInt(e.Price, 10))
		b.WriteString(",")
		b.WriteString(e.Source)
		b.WriteString(",")
		b.WriteString(strconv.Itoa(e.Priority))
		if len(e.Conflicting) > 0 {
			cf := append([]string(nil), e.Conflicting...)
			sort.Strings(cf)
			b.WriteString(",cf=")
			b.WriteString(strings.Join(cf, "+"))
		}
	}
	b.WriteString("|integral=")
	b.WriteString(in.Integral)
	b.WriteString("|covered_usec=")
	b.WriteString(strconv.FormatInt(in.CoveredUsec, 10))
	b.WriteString("|twap=")
	b.WriteString(in.TWAP)
	b.WriteString("|twap6=")
	b.WriteString(in.TWAPRounded)
	b.WriteString("|coverage6=")
	b.WriteString(in.Coverage)
	b.WriteString("|stale=")
	b.WriteString(strconv.FormatBool(in.Stale))

	sum := sha256.Sum256([]byte(b.String()))
	return hex.EncodeToString(sum[:])
}

// ResultHash builds the canonical hash of a computed window together with the
// effective events used to produce it.
func ResultHash(res domain.WindowResult, events []domain.EffectiveSample, staleHorizonSec int64) string {
	twap := "0/0"
	if res.TWAPDen.Sign() != 0 {
		twap = res.TWAPNum.String() + "/" + res.TWAPDen.String()
	}
	return CanonicalHash(CanonicalInputs{
		Symbol:          res.Symbol,
		WindowSec:       res.WindowSec,
		StartUnix:       res.StartUnix,
		EndUnix:         res.EndUnix,
		Events:          events,
		Integral:        res.Integral.String(),
		CoveredUsec:     res.CoveredUsec,
		TWAP:            twap,
		TWAPRounded:     res.TWAPString,
		Coverage:        res.CoverageString,
		Stale:           res.Stale,
		StaleHorizonSec: staleHorizonSec,
	})
}
