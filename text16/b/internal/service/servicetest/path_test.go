package servicetest

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/example/forensiccore/internal/safeopen"
	"github.com/example/forensiccore/internal/service"
)

// 各类路径穿越必须被拒绝，且不可能读到白名单根之外的文件。
func TestPathTraversal_Rejected(t *testing.T) {
	env := NewEnv(t)
	caseID := env.CreateCase(t, "SEC-PATH")

	// 在根目录之外放置一个“敏感”文件。
	outside := filepath.Join(t.TempDir(), "secret.raw")
	if err := os.WriteFile(outside, make([]byte, 64), 0o644); err != nil {
		t.Fatal(err)
	}

	attempts := []string{
		"../secret.raw",
		"../../etc/passwd",
		"./../secret.raw",
		"sub/../../secret.raw",
		"/etc/passwd",
		"",
		".",
		"a//../secret.raw",
	}
	for _, p := range attempts {
		_, err := env.Svc.RegisterEvidence(service.RegisterEvidenceInput{
			CaseID: caseID, RootName: env.RootName, RelPath: p,
			Name: "x", Actor: "investigator",
		})
		if err == nil {
			t.Fatalf("path %q should be rejected", p)
		}
		var se *service.Error
		if !errors.As(err, &se) || se.Kind != service.KindValidation {
			t.Fatalf("path %q: want validation error, got %T %v", p, err, err)
		}
	}
}

// 根内符号链接（包括指向根外文件的链接）必须被拒绝。
func TestSymlink_Rejected(t *testing.T) {
	env := NewEnv(t)
	caseID := env.CreateCase(t, "SEC-LINK")

	outside := filepath.Join(t.TempDir(), "outside.raw")
	if err := os.WriteFile(outside, make([]byte, 128), 0o644); err != nil {
		t.Fatal(err)
	}

	// 1) 链接指向根外常规文件。
	link := filepath.Join(env.Root, "link.raw")
	if err := os.Symlink(outside, link); err != nil {
		t.Fatal(err)
	}
	_, err := env.Svc.RegisterEvidence(service.RegisterEvidenceInput{
		CaseID: caseID, RootName: env.RootName, RelPath: "link.raw",
		Name: "link.raw", Actor: "investigator",
	})
	if err == nil {
		t.Fatal("symlink to outside must be rejected")
	}

	// 2) 中间目录是符号链接。
	inDir := filepath.Join(t.TempDir(), "realdir")
	if err := os.MkdirAll(inDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(inDir, "m.raw"), make([]byte, 32), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(inDir, filepath.Join(env.Root, "dirlink")); err != nil {
		t.Fatal(err)
	}
	_, err = env.Svc.RegisterEvidence(service.RegisterEvidenceInput{
		CaseID: caseID, RootName: env.RootName, RelPath: "dirlink/m.raw",
		Name: "m.raw", Actor: "investigator",
	})
	if err == nil {
		t.Fatal("symlinked intermediate directory must be rejected")
	}

	// 直接验证 safeopen 错误类别。
	root, err := safeopen.OpenRoot("evidence", env.Root)
	if err != nil {
		t.Fatal(err)
	}
	defer root.Close()
	if _, _, err := root.Open("../secret.raw", 0); err != safeopen.ErrPathTraversal {
		t.Fatalf("want ErrPathTraversal, got %v", err)
	}
	if _, _, err := root.Open("link.raw", 0); err != safeopen.ErrSymlink {
		t.Fatalf("want ErrSymlink, got %v", err)
	}
}

// 白名单外的逻辑根名必须被拒绝。
func TestUnknownRoot_Rejected(t *testing.T) {
	env := NewEnv(t)
	caseID := env.CreateCase(t, "SEC-ROOT")
	env.WriteImage(t, "a.raw", 16)
	_, err := env.Svc.RegisterEvidence(service.RegisterEvidenceInput{
		CaseID: caseID, RootName: "not-a-mounted-root", RelPath: "a.raw",
		Name: "a", Actor: "investigator",
	})
	var se *service.Error
	if !errors.As(err, &se) || se.Kind != service.KindValidation {
		t.Fatalf("want validation, got %v", err)
	}
}
