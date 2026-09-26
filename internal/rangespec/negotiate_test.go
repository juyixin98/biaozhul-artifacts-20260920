package rangespec

import "testing"

func TestSelectEncoding(t *testing.T) {
	tests := []struct {
		name       string
		header     string
		offersGzip bool
		want       string
		wantOK     bool
	}{
		{"no header defaults identity", "", true, RepIdentity, true},
		{"identity explicitly", "identity", true, RepIdentity, true},
		{"gzip requested", "gzip", true, RepGzip, true},
		{"gzip wins over identity q tie", "gzip, identity", true, RepGzip, true},
		{"higher identity q wins", "gzip;q=0.5, identity;q=1", true, RepIdentity, true},
		{"identity refused, gzip accepted", "identity;q=0, gzip", true, RepGzip, true},
		{"both refused", "identity;q=0, gzip;q=0", true, "", false},
		{"star accepts identity on tie", "*;q=1", true, RepIdentity, true},
		{"star refused", "*;q=0", true, "", false},
		{"gzip requested but not offered", "gzip", false, RepIdentity, true},
		{"identity refused and gzip not offered", "identity;q=0", false, "", false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := SelectEncoding(tc.header, tc.offersGzip)
			if ok != tc.wantOK || got != tc.want {
				t.Errorf("SelectEncoding(%q, %v) = %q,%v want %q,%v",
					tc.header, tc.offersGzip, got, ok, tc.want, tc.wantOK)
			}
		})
	}
}

func TestBoundaryDeterministic(t *testing.T) {
	if Boundary("x") != Boundary("x") {
		t.Error("boundary not deterministic for same nonce")
	}
	if Boundary("x") == Boundary("y") {
		t.Error("boundary collides for different nonces")
	}
}
