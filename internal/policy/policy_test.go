package policy

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestValidateRejectsUnlistedAllow(t *testing.T) {
	p := &Policy{
		Name:     "bad",
		Unlisted: DecisionAllow,
		Licenses: map[string]LicenseRule{"MIT": {Decision: DecisionAllow}},
	}
	err := p.Validate()
	if err == nil || !strings.Contains(err.Error(), "unknown") {
		t.Fatalf("期望拒绝 unlisted=allow，实际: %v", err)
	}
}

func TestValidateRejectsUnlistedDeny(t *testing.T) {
	p := &Policy{
		Name:     "bad",
		Unlisted: DecisionDeny,
		Licenses: map[string]LicenseRule{},
	}
	if err := p.Validate(); err == nil {
		t.Fatal("期望拒绝 unlisted=deny")
	}
}

func TestUnlistedDefaultsUnknown(t *testing.T) {
	p := &Policy{Name: "p", Licenses: map[string]LicenseRule{}}
	if err := p.Validate(); err != nil {
		t.Fatal(err)
	}
	if got := p.LicenseDecision("Never-Heard-Of-It"); got != DecisionUnknown {
		t.Fatalf("未知许可证裁决 = %v, 期望 unknown", got)
	}
}

func TestLicenseDecision(t *testing.T) {
	p := &Policy{
		Name: "p",
		Licenses: map[string]LicenseRule{
			"MIT":          {Decision: DecisionAllow},
			"GPL-3.0-only": {Decision: DecisionDeny},
			"LicenseRef-X": {Decision: DecisionUnknown},
		},
	}
	if err := p.Validate(); err != nil {
		t.Fatal(err)
	}
	if p.LicenseDecision("MIT") != DecisionAllow {
		t.Error("MIT 应为 allow")
	}
	if p.LicenseDecision("GPL-3.0-only") != DecisionDeny {
		t.Error("GPL 应为 deny")
	}
	if p.LicenseDecision("LicenseRef-X") != DecisionUnknown {
		t.Error("LicenseRef-X 应为 unknown")
	}
}

func TestBadDecisionValue(t *testing.T) {
	p := &Policy{
		Name:     "p",
		Unlisted: DecisionUnknown,
		Licenses: map[string]LicenseRule{"MIT": {Decision: "maybe"}},
	}
	if err := p.Validate(); err == nil {
		t.Fatal("非法 decision 值应被拒绝")
	}
}

func TestDenyExceptionCannotHaveAppliesTo(t *testing.T) {
	p := &Policy{
		Name: "p",
		Licenses: map[string]LicenseRule{
			"MIT":                 {Decision: DecisionAllow},
			"Bison-exception-2.2": {Decision: DecisionAllow},
		},
		Exceptions: map[string]ExceptionRule{
			"Bison-exception-2.2": {Allow: false, AppliesTo: []string{"MIT"}},
		},
	}
	if err := p.Validate(); err == nil {
		t.Fatal("拒绝型例外不应允许声明 applies_to")
	}
}

func TestLoadFileUnknownFieldRejected(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "p.json")
	content := `{
	  "name": "p",
	  "licenses": {"MIT": {"decision": "allow"}},
	  "bogus": 1
	}`
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadFile(path); err == nil {
		t.Fatal("包含未知字段的 JSON 应被拒绝")
	}
}

func TestLoadPathDirectory(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "only.json")
	content := `{
	  "name": "p",
	  "licenses": {"MIT": {"decision": "allow"}}
	}`
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	p, err := LoadPath(dir)
	if err != nil {
		t.Fatalf("从目录加载单一策略失败: %v", err)
	}
	if p.Name != "p" {
		t.Fatalf("策略名 = %q", p.Name)
	}
}

func TestLoadPathDirectoryMultipleJSON(t *testing.T) {
	dir := t.TempDir()
	for _, name := range []string{"a.json", "b.json"} {
		if err := os.WriteFile(filepath.Join(dir, name),
			[]byte(`{"name":"`+strings.TrimSuffix(name, ".json")+`","licenses":{}}`), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := LoadPath(dir); err == nil {
		t.Fatal("目录含多个 JSON 时应报错")
	}
}
