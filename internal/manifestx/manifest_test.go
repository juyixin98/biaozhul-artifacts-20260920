package manifestx

import (
	"strings"
	"testing"
)

func validDigest(seed string) string {
	return "sha256:" + strings.Repeat(seed, 64/len(seed))[:64]
}

func TestParseImageManifest(t *testing.T) {
	cfg := validDigest("1")
	l1 := validDigest("2")
	l2 := validDigest("3")
	body := `{
	  "schemaVersion": 2,
	  "mediaType": "application/vnd.oci.image.manifest.v1+json",
	  "config": {"mediaType":"application/vnd.oci.image.config.v1+json","digest":"` + cfg + `","size":11},
	  "layers": [
	    {"mediaType":"application/vnd.oci.image.layer.v1.tar","digest":"` + l1 + `","size":22},
	    {"mediaType":"application/vnd.oci.image.layer.v1.tar","digest":"` + l2 + `","size":33}
	  ]
	}`
	refs, err := Parse([]byte(body), OCIManifestMediaType)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if len(refs) != 3 {
		t.Fatalf("got %d refs, want 3: %+v", len(refs), refs)
	}
	if refs[0].Kind != "blob" || refs[0].Digest != cfg {
		t.Fatalf("first ref should be config blob %s, got %+v", cfg, refs[0])
	}
	for i, want := range []string{cfg, l1, l2} {
		if refs[i].Digest != want {
			t.Fatalf("ref %d digest = %s, want %s", i, refs[i].Digest, want)
		}
	}
}

func TestParseIndexManifest(t *testing.T) {
	m1 := validDigest("a")
	m2 := validDigest("b")
	body := `{
	  "schemaVersion": 2,
	  "mediaType": "application/vnd.oci.image.index.v1+json",
	  "manifests": [
	    {"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":"` + m1 + `","size":1},
	    {"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":"` + m2 + `","size":1}
	  ]
	}`
	refs, err := Parse([]byte(body), OCIIndexMediaType)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	if len(refs) != 2 {
		t.Fatalf("got %d refs, want 2", len(refs))
	}
	for _, r := range refs {
		if r.Kind != "manifest" {
			t.Fatalf("index child must be manifest, got %+v", r)
		}
	}
}

func TestParseSubjectEdge(t *testing.T) {
	cfg := validDigest("1")
	subj := validDigest("9")
	body := `{
	  "schemaVersion": 2,
	  "mediaType": "application/vnd.oci.image.manifest.v1+json",
	  "config": {"mediaType":"x","digest":"` + cfg + `","size":1},
	  "layers": [],
	  "subject": {"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":"` + subj + `","size":1}
	}`
	refs, err := Parse([]byte(body), OCIManifestMediaType)
	if err != nil {
		t.Fatalf("Parse: %v", err)
	}
	found := false
	for _, r := range refs {
		if r.Kind == "manifest" && r.Digest == subj {
			found = true
		}
	}
	if !found {
		t.Fatalf("subject manifest edge missing: %+v", refs)
	}
}

func TestParseRejectsBadInputs(t *testing.T) {
	cfg := validDigest("1")
	cases := []struct {
		name, mt, body string
	}{
		{"unsupported media type", "application/json", `{}`},
		{"schemaVersion 1", OCIManifestMediaType,
			`{"schemaVersion":1,"config":{"digest":"` + cfg + `"},"layers":[]}`},
		{"missing config", OCIManifestMediaType,
			`{"schemaVersion":2,"layers":[]}`},
		{"bad layer digest", OCIManifestMediaType,
			`{"schemaVersion":2,"config":{"digest":"` + cfg + `"},"layers":[{"digest":"nope"}]}`},
		{"malformed json", OCIIndexMediaType, `{not json`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := Parse([]byte(tc.body), tc.mt); err == nil {
				t.Fatalf("expected error for %s", tc.name)
			}
		})
	}
}
