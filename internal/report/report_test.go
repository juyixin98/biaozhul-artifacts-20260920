package report

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func TestSummary(t *testing.T) {
	s := Suite{Cases: []Case{
		{Status: StatusPass}, {Status: StatusPass},
		{Status: StatusFail}, {Status: StatusSkip},
	}}
	pass, fail, skip := s.Summary()
	if pass != 2 || fail != 1 || skip != 1 {
		t.Fatalf("got %d/%d/%d", pass, fail, skip)
	}
}

func TestWriteJSON(t *testing.T) {
	s := Suite{Name: "x", Cases: []Case{{ID: "R1", Status: StatusPass}}}
	var buf bytes.Buffer
	if err := WriteJSON(&buf, s); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(buf.String(), `"id": "R1"`) {
		t.Fatalf("json missing case: %s", buf.String())
	}
	var decoded Suite
	if err := json.Unmarshal(buf.Bytes(), &decoded); err != nil {
		t.Fatalf("invalid json: %v", err)
	}
	if decoded.Cases[0].ElapsedMS != 0 || decoded.DurationMS != 0 {
		t.Error("ms fields must serialize as integers")
	}
}
