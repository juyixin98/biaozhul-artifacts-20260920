package etag

import "testing"

func TestNewIsStrongAndBindsVersionAndContent(t *testing.T) {
	t1 := New(3, []byte("hello"))
	if t1.Weak {
		t.Fatal("New must produce a strong tag")
	}
	if t1.Opaque == "" {
		t.Fatal("opaque value empty")
	}
	// Same version + content -> identical tag; changed content -> different tag.
	if New(3, []byte("hello")) != t1 {
		t.Fatal("deterministic tag mismatch for identical input")
	}
	if New(3, []byte("hellp")) == t1 {
		t.Fatal("content change must change the tag")
	}
	if New(4, []byte("hello")) == t1 {
		t.Fatal("version change must change the tag")
	}
}

func TestParse(t *testing.T) {
	cases := []struct {
		name    string
		in      string
		wantErr bool
		weak    bool
		opaque  string
	}{
		{name: "strong", in: `"v1-abcd"`, opaque: "v1-abcd"},
		{name: "weak upper prefix", in: `W/"v1-abcd"`, weak: true, opaque: "v1-abcd"},
		{name: "weak lower prefix", in: `w/"v1-abcd"`, weak: true, opaque: "v1-abcd"},
		{name: "missing quotes", in: `v1-abcd`, wantErr: true},
		{name: "only prefix", in: `W/`, wantErr: true},
		{name: "empty", in: ``, wantErr: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := Parse(tc.in)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("Parse(%q) expected error, got %+v", tc.in, got)
				}
				return
			}
			if err != nil {
				t.Fatalf("Parse(%q) unexpected error: %v", tc.in, err)
			}
			if got.Weak != tc.weak || got.Opaque != tc.opaque {
				t.Fatalf("Parse(%q) = %+v, want weak=%v opaque=%q", tc.in, got, tc.weak, tc.opaque)
			}
			if got.String() != tc.in && !(tc.name == "weak lower prefix") {
				t.Fatalf("round trip: %q != %q", got.String(), tc.in)
			}
		})
	}
}

func TestStrongComparison(t *testing.T) {
	strong := ETag{Opaque: "x"}
	strong2 := ETag{Opaque: "x"}
	weak := ETag{Weak: true, Opaque: "x"}
	other := ETag{Opaque: "y"}

	if !StrongEqual(strong, strong2) {
		t.Error("two strong equal tags must compare equal")
	}
	if StrongEqual(strong, weak) || StrongEqual(weak, strong) || StrongEqual(weak, weak) {
		t.Error("any weak tag must fail strong comparison")
	}
	if StrongEqual(strong, other) {
		t.Error("different opaque values must not compare equal")
	}
	if !WeakEqual(strong, weak) {
		t.Error("weak comparison ignores the weak marking")
	}
}

func TestParseList(t *testing.T) {
	tags, anyTag, err := ParseList("*")
	if err != nil || anyTag != true || len(tags) != 0 {
		t.Fatalf("wildcard: tags=%v any=%v err=%v", tags, anyTag, err)
	}
	tags, anyTag, err = ParseList(`"v1-a", "v2-b", W/"v3-c"`)
	if err != nil || anyTag {
		t.Fatalf("list: any=%v err=%v", anyTag, err)
	}
	if len(tags) != 3 || !tags[2].Weak {
		t.Fatalf("expected 3 tags with third weak, got %+v", tags)
	}
	if _, _, err := ParseList(`not-a-tag`); err == nil {
		t.Fatal("malformed list entry must error")
	}
	if tags, _, err := ParseList("   "); err != nil || len(tags) != 0 {
		t.Fatalf("blank header must be empty list, got %v %v", tags, err)
	}
}
