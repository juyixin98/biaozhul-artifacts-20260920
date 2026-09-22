package csvparse

import (
	"strings"
	"testing"
	"time"
)

func mustDate(s string) time.Time {
	d, err := time.Parse("2006-01-02", s)
	if err != nil {
		panic(err)
	}
	return d
}

func TestParseOK(t *testing.T) {
	in := "resource_id,service,date,currency,amount\n" +
		"r1,compute,2026-01-05,USD,12.34\n" +
		"r2,storage,2026-01-06,EUR,0\n"
	rows, err := Parse(strings.NewReader(in))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(rows) != 2 {
		t.Fatalf("want 2 rows, got %d", len(rows))
	}
	if rows[0].Line != 2 || rows[1].Line != 3 {
		t.Fatalf("line numbers wrong: %d %d", rows[0].Line, rows[1].Line)
	}
	if rows[0].Amount.String() != "12.34" {
		t.Fatalf("amount parse: %s", rows[0].Amount)
	}
	if !rows[0].Date.Equal(mustDate("2026-01-05")) {
		t.Fatalf("date parse: %v", rows[0].Date)
	}
}

func TestParseRejects(t *testing.T) {
	cases := map[string]string{
		"bad header":      "a,b,c,d,e\nr1,s,2026-01-01,USD,1\n",
		"bad date":        "resource_id,service,date,currency,amount\nr1,s,01/01/2026,USD,1\n",
		"lower currency":  "resource_id,service,date,currency,amount\nr1,s,2026-01-01,usd,1\n",
		"float exponent":  "resource_id,service,date,currency,amount\nr1,s,2026-01-01,USD,1e3\n",
		"too many digits": "resource_id,service,date,currency,amount\nr1,s,2026-01-01,USD,1.1234567\n",
		"empty resource":  "resource_id,service,date,currency,amount\n,s,2026-01-01,USD,1\n",
		"wrong columns":   "resource_id,service,date,currency,amount\nr1,s,2026-01-01,USD\n",
		"empty file":      "",
		"header only":     "resource_id,service,date,currency,amount\n",
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			if _, err := Parse(strings.NewReader(body)); err == nil {
				t.Fatalf("expected error for %q", name)
			}
		})
	}
}

func TestParseRowNumberReported(t *testing.T) {
	body := "resource_id,service,date,currency,amount\n" +
		strings.Repeat("r,s,2026-01-01,USD,1\n", 3) +
		"r,s,not-a-date,USD,1\n"
	_, err := Parse(strings.NewReader(body))
	var pe *ParseError
	if err == nil {
		t.Fatal("expected error")
	}
	if !asParseError(err, &pe) {
		t.Fatalf("want ParseError, got %T", err)
	}
	if pe.Errors[0].Line != 5 {
		t.Fatalf("want line 5, got %d", pe.Errors[0].Line)
	}
}

func TestParseOver5000Rows(t *testing.T) {
	var sb strings.Builder
	sb.WriteString("resource_id,service,date,currency,amount\n")
	for i := 0; i < MaxRows+1; i++ {
		sb.WriteString("r,s,2026-01-01,USD,1\n")
	}
	_, err := Parse(strings.NewReader(sb.String()))
	var pe *ParseError
	if !asParseError(err, &pe) {
		t.Fatalf("want ParseError, got %v", err)
	}
	if pe.Errors[0].Line != MaxRows+2 {
		t.Fatalf("want line %d, got %d", MaxRows+2, pe.Errors[0].Line)
	}
}

func TestParseExactly5000Rows(t *testing.T) {
	var sb strings.Builder
	sb.WriteString("resource_id,service,date,currency,amount\n")
	for i := 0; i < MaxRows; i++ {
		sb.WriteString("r,s,2026-01-01,USD,1\n")
	}
	// All same key -> parser itself does not dedupe (service does), so this
	// verifies only the row cap; use distinct resources to keep rows valid.
	sb.Reset()
	sb.WriteString("resource_id,service,date,currency,amount\n")
	for i := 0; i < MaxRows; i++ {
		sb.WriteString("r")
		sb.WriteString(itoaP(i))
		sb.WriteString(",s,2026-01-01,USD,1\n")
	}
	rows, err := Parse(strings.NewReader(sb.String()))
	if err != nil {
		t.Fatalf("unexpected: %v", err)
	}
	if len(rows) != MaxRows {
		t.Fatalf("want %d, got %d", MaxRows, len(rows))
	}
}

func asParseError(err error, pe **ParseError) bool {
	p, ok := err.(*ParseError)
	if ok {
		*pe = p
	}
	return ok
}

func itoaP(n int) string {
	if n == 0 {
		return "0"
	}
	var b []byte
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	return string(b)
}
