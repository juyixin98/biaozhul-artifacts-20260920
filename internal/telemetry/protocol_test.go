package telemetry

import (
	"encoding/json"
	"errors"
	"testing"
)

func validRaw(t *testing.T) []byte {
	t.Helper()
	raw, err := json.Marshal(Message{
		DeviceID: "dev-001", BootGen: 2, Seq: 7, Value: 1.5, TSMillis: 1727100000000,
		Sig: "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
	})
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestParseAndValidateOK(t *testing.T) {
	m, err := ParseAndValidate("dev-001", validRaw(t))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if m.Seq != 7 || m.BootGen != 2 {
		t.Fatalf("parsed wrong message: %+v", m)
	}
}

func TestParseAndValidateRejections(t *testing.T) {
	sig := "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
	base := Message{DeviceID: "dev-001", BootGen: 1, Seq: 1, Value: 1, TSMillis: 1, Sig: sig}

	cases := []struct {
		name   string
		topic  string
		mutate func(*Message)
		raw    []byte
	}{
		{"empty", "dev-001", nil, []byte(``)},
		{"bad json", "dev-001", nil, []byte(`{broken`)},
		{"missing device", "dev-001", func(m *Message) { m.DeviceID = "" }, nil},
		{"topic mismatch", "dev-002", nil, nil},
		{"boot zero", "dev-001", func(m *Message) { m.BootGen = 0 }, nil},
		{"seq zero", "dev-001", func(m *Message) { m.Seq = 0 }, nil},
		{"ts zero", "dev-001", func(m *Message) { m.TSMillis = 0 }, nil},
		{"sig short", "dev-001", func(m *Message) { m.Sig = "abc" }, nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			raw := tc.raw
			if raw == nil {
				m := base
				if tc.mutate != nil {
					tc.mutate(&m)
				}
				raw, _ = json.Marshal(m)
			}
			_, err := ParseAndValidate(tc.topic, raw)
			if err == nil {
				t.Fatal("expected rejection, got nil")
			}
			var rj *RejectError
			if !errors.As(err, &rj) {
				t.Fatalf("error %v is not RejectError", err)
			}
		})
	}
}

func TestRetryErrorClassification(t *testing.T) {
	if AsRetry(nil) {
		t.Fatal("nil is not a retry error")
	}
	if AsRetry(&RejectError{Reason: "x"}) {
		t.Fatal("RejectError must not be classified as retry")
	}
	if !AsRetry(&RetryError{Reason: "x"}) {
		t.Fatal("RetryError must be classified as retry")
	}
}
