package schema

import "testing"

func TestParseSupportedSubset(t *testing.T) {
	s, err := Parse([]byte(`{
		"type": "object",
		"required": ["amount"],
		"properties": {
			"amount": {"type": "number", "minimum": 0, "exclusiveMaximum": 100},
			"tags": {"type": "array", "items": {"type": "string"}},
			"status": {"enum": ["ok", "failed"]}
		}
	}`))
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if len(s.Unsupported) != 0 {
		t.Fatalf("expected no unsupported keywords, got %v", s.Unsupported)
	}
	if len(s.Required) != 1 || s.Required[0] != "amount" {
		t.Fatalf("required = %v", s.Required)
	}
	amount := s.Properties["amount"]
	if amount.Minimum == nil || *amount.Minimum != 0 {
		t.Fatalf("minimum = %v", amount.Minimum)
	}
	if amount.ExclusiveMaximum == nil || *amount.ExclusiveMaximum != 100 {
		t.Fatalf("exclusiveMaximum = %v", amount.ExclusiveMaximum)
	}
	if s.Properties["tags"].Items == nil {
		t.Fatal("items schema missing")
	}
	if len(s.Properties["status"].Enum) != 2 {
		t.Fatalf("enum = %v", s.Properties["status"].Enum)
	}
}

func TestParseRecordsUnsupportedKeywords(t *testing.T) {
	s, err := Parse([]byte(`{
		"type": "string",
		"pattern": "^[a-z]+$",
		"format": "email",
		"title": "ignored annotation"
	}`))
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	want := []string{"format", "pattern"}
	if len(s.Unsupported) != len(want) {
		t.Fatalf("unsupported = %v, want %v", s.Unsupported, want)
	}
	for i, k := range want {
		if s.Unsupported[i] != k {
			t.Fatalf("unsupported = %v, want %v", s.Unsupported, want)
		}
	}
}

func TestParseRejectsInvalidDocuments(t *testing.T) {
	for _, doc := range []string{
		`{not json`,
		`["array", "root"]`,
		`{"type": 42}`,
		`{"type": [1, 2]}`,
		`{"required": "not-an-array"}`,
		`{"required": [1]}`,
		`{"enum": "not-an-array"}`,
		`{"properties": "not-an-object"}`,
		`{"properties": {"a": 42}}`,
		`{"items": 42}`,
		`{"minimum": "zero"}`,
		`{"maximum": true}`,
		`{"exclusiveMinimum": null}`,
		`{"exclusiveMaximum": {}}`,
	} {
		if _, err := Parse([]byte(doc)); err == nil {
			t.Fatalf("expected error for %s", doc)
		}
	}
}

func TestParseExclusiveBounds(t *testing.T) {
	s, err := Parse([]byte(`{"type":"number","exclusiveMinimum":1.5,"exclusiveMaximum":9.5}`))
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if s.ExclusiveMinimum == nil || *s.ExclusiveMinimum != 1.5 {
		t.Fatalf("exclusiveMinimum = %v", s.ExclusiveMinimum)
	}
	if s.ExclusiveMaximum == nil || *s.ExclusiveMaximum != 9.5 {
		t.Fatalf("exclusiveMaximum = %v", s.ExclusiveMaximum)
	}
}
