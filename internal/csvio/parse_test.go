package csvio

import (
	"strings"
	"testing"
)

const validHeader = "account_code,resource_code,service,cost_date,currency,amount\n"

func TestParseOK(t *testing.T) {
	in := validHeader +
		"acct-1,res-1,EC2,2026-01-02,USD,12.30\n" +
		"acct-1,res-2,S3,2026-01-02,EUR,0.000001\n"
	rows, err := Parse(strings.NewReader(in))
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 2 {
		t.Fatalf("rows=%d", len(rows))
	}
	if rows[0].Line != 2 || rows[1].Line != 3 {
		t.Fatalf("line numbers %d,%d", rows[0].Line, rows[1].Line)
	}
	if rows[0].Amount.String() != "12.3" {
		t.Fatalf("amount=%s", rows[0].Amount)
	}
}

func TestParseLineNumbers(t *testing.T) {
	cases := []struct {
		name     string
		file     string
		wantLine int
	}{
		{"bad header", "a,b,c,d,e,f\nx,x,x,x,x,x\n", 1},
		{"bad date", validHeader + "a,r,S,2026-13-99,USD,1\n", 2},
		{"bad currency", validHeader + "a,r,S,2026-01-01,us,1\n", 2},
		{"bad amount second row", validHeader +
			"a,r,S,2026-01-01,USD,1\na,r,S,2026-01-01,USD,nope\n", 3},
		{"negative amount", validHeader + "a,r,S,2026-01-01,USD,-1\n", 2},
		{"too precise", validHeader + "a,r,S,2026-01-01,USD,1.0000001\n", 2},
		{"empty field", validHeader + "a,r,S,2026-01-01,USD,\n", 2},
		{"empty file", "", 1},
		{"header only", validHeader, 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, err := Parse(strings.NewReader(tc.file))
			if err == nil {
				t.Fatal("expected error")
			}
			pe, ok := err.(*ParseError)
			if !ok {
				t.Fatalf("error type %T", err)
			}
			if pe.Line != tc.wantLine {
				t.Fatalf("line=%d want %d (%v)", pe.Line, tc.wantLine, err)
			}
		})
	}
}

func TestParseBatchLimit(t *testing.T) {
	var sb strings.Builder
	sb.WriteString(validHeader)
	for i := 0; i < MaxRows+1; i++ {
		sb.WriteString("a,r,S,2026-01-01,USD,1\n")
	}
	_, err := Parse(strings.NewReader(sb.String()))
	if err == nil {
		t.Fatal("expected batch size error")
	}
	pe := err.(*ParseError)
	if pe.Line != MaxRows+2 {
		t.Fatalf("line=%d want %d", pe.Line, MaxRows+2)
	}
}
