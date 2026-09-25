package tokenize

import (
	"strings"
	"testing"
)

func kinds(rs []Slot) []string {
	out := make([]string, 0, len(rs))
	for _, s := range rs {
		switch s.Kind {
		case Word:
			out = append(out, "W:"+s.Literal)
		case Punct:
			out = append(out, "P:"+s.Literal)
		case Var:
			out = append(out, "V:"+s.Vk.String())
		}
	}
	return out
}

func TestUUID(t *testing.T) {
	cases := []string{
		"550e8400-e29b-41d4-a716-446655440000",
		"550E8400-E29B-41D4-A716-446655440000",
		"{550e8400-e29b-41d4-a716-446655440000}",
		"urn:uuid:550e8400-e29b-41d4-a716-446655440000",
	}
	for _, in := range cases {
		r := Line("user " + in + " done")
		// expect: word "user", var uuid, word "done"
		if len(r.Slots) != 3 {
			t.Fatalf("%s: got %d slots (%v)", in, len(r.Slots), kinds(r.Slots))
		}
		if r.Slots[0].Kind != Word || r.Slots[0].Literal != "user" {
			t.Errorf("%s: slot0 = %v", in, r.Slots[0])
		}
		if r.Slots[1].Kind != Var || r.Slots[1].Vk != UUID {
			t.Errorf("%s: slot1 not uuid: %v", in, r.Slots[1])
		}
		if r.Slots[2].Kind != Word || r.Slots[2].Literal != "done" {
			t.Errorf("%s: slot2 = %v", in, r.Slots[2])
		}
	}
}

func TestNumbersAndUnits(t *testing.T) {
	r := Line("pid -7 size 1.5 bytes 8080ms hex 0xDEADBEEF pct 99% exp 2e3")
	got := kinds(r.Slots)
	// pid, -7, size, 1.5, bytes, 8080ms, hex, 0xDEADBEEF, pct, 99%, exp, 2e3
	want := []string{
		"W:pid", "V:num",
		"W:size", "V:num",
		"W:bytes", "V:num",
		"W:hex", "V:num",
		"W:pct", "V:num",
		"W:exp", "V:num",
	}
	if len(got) != len(want) {
		t.Fatalf("got %d slots %v, want %d %v", len(got), got, len(want), want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("slot %d: got %s want %s", i, got[i], want[i])
		}
	}
}

func TestQuotedString(t *testing.T) {
	r := Line(`user "alice bob" and 'x y' end`)
	got := kinds(r.Slots)
	want := []string{"W:user", "V:str", "W:and", "V:str", "W:end"}
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("got %v want %v", got, want)
	}
}

func TestDateSeparatorsSurvive(t *testing.T) {
	// The '-' inside a date must remain punctuation, not be eaten as the sign
	// of the following number.
	r := Line("2026-09-24 10:00:00.123 INFO ok")
	out := Render(r.Slots)
	for _, want := range []string{"<NUM>-<NUM>-<NUM>", "<NUM>:<NUM>"} {
		if !strings.Contains(out, want) {
			t.Errorf("render %q missing %q", out, want)
		}
	}
}

func TestSignedNumbersOnlyAtBoundary(t *testing.T) {
	// After whitespace, "-7" is one signed number.
	r := Line("delta -7 done")
	if len(r.Slots) != 3 {
		t.Fatalf("slots=%v", kinds(r.Slots))
	}
	if r.Slots[1].Kind != Var || r.Slots[1].Vk != Num || r.Slots[1].Sep != true {
		t.Fatalf("signed slot = %+v", r.Slots[1])
	}
	// Embedded "-7" splits into punct '-' + number, preserving the separator.
	r2 := Line("v-7 end")
	if len(r2.Slots) != 4 { // word v, punct -, num, word end
		t.Fatalf("slots=%v", kinds(r2.Slots))
	}
	if r2.Slots[1].Kind != Punct || r2.Slots[1].Literal != "-" {
		t.Fatalf("separator slot = %+v", r2.Slots[1])
	}
}

func TestKeywordsStayLiteral(t *testing.T) {
	// Pure alphabetic error words must never be treated as variables.
	r := Line("Connection refused vs timeout")
	for _, s := range r.Slots {
		if s.Kind == Var {
			t.Fatalf("unexpected variable slot: %v in %v", s, kinds(r.Slots))
		}
	}
}

func TestHexLikeUUIDNotConfused(t *testing.T) {
	// A plain hex number without dashes is a number, not a UUID.
	r := Line("0x550e8400")
	if len(r.Slots) != 1 || r.Slots[0].Vk != Num {
		t.Fatalf("got %v", kinds(r.Slots))
	}
}

func TestLongLineTruncated(t *testing.T) {
	var b strings.Builder
	for i := 0; i < MaxTokens+500; i++ {
		if i > 0 {
			b.WriteByte(' ')
		}
		b.WriteString("token")
	}
	r := Line(b.String())
	if !r.Truncated {
		t.Fatal("expected Truncated")
	}
	if len(r.Slots) > MaxTokens {
		t.Fatalf("slots %d exceed cap %d", len(r.Slots), MaxTokens)
	}
}

func TestPromotable(t *testing.T) {
	if Promotable(Slot{Kind: Word, Literal: "task101"}) != true {
		t.Error("task101 should be promotable")
	}
	if Promotable(Slot{Kind: Word, Literal: "refused"}) != false {
		t.Error("pure-alpha keyword must not be promotable")
	}
	if Promotable(Slot{Kind: Var, Vk: Num}) != false {
		t.Error("variable slots are not literal/promotable")
	}
	if Promotable(Slot{Kind: Word, Literal: strings.Repeat("a1", 20)}) != false {
		t.Error("over-long word should not be promotable")
	}
}

func TestRenderGluesPunctuation(t *testing.T) {
	r := Line("GET /api/users/5 200 128 bytes in 5ms")
	out := Render(r.Slots)
	if !strings.Contains(out, "GET /api/users/<NUM>") {
		t.Errorf("render: %q", out)
	}
	if strings.Contains(out, "ms ") {
		t.Errorf("unit suffix separated: %q", out)
	}
	t.Log(out)
}

// BenchmarkLongLine guards against regex catastrophic backtracking on
// pathological long lines. It must stay well under a millisecond.
func BenchmarkLongLine(b *testing.B) {
	var sb strings.Builder
	for k := 0; k < 2400; k++ {
		if k > 0 {
			sb.WriteByte(' ')
		}
		sb.WriteString("field")
		sb.WriteString(itoa(k))
		sb.WriteString("=12")
	}
	line := sb.String()
	b.SetBytes(int64(len(line)))
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		Line(line)
	}
}

func itoa(n int) string {
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
