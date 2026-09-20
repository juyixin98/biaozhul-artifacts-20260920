package servicetest

import (
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/example/forensiccore/internal/model"
	"github.com/example/forensiccore/internal/service"
)

// 登记成功：基线被保存，证据链首事件为 register，链校验健康。
func TestRegisterEvidence_Success(t *testing.T) {
	env := NewEnv(t)
	caseID := env.CreateCase(t, "C-OK")
	rel := env.WriteImage(t, "disk-a.raw", 4096*10) // 10 块

	ev, err := env.Svc.RegisterEvidence(service.RegisterEvidenceInput{
		CaseID: caseID, RootName: env.RootName, RelPath: rel,
		Name: "disk-a.raw", Actor: "investigator",
	})
	if err != nil {
		t.Fatalf("register: %v", err)
	}
	if ev.Size != 4096*10 {
		t.Fatalf("size = %d", ev.Size)
	}
	if len(ev.SHA256) != 64 {
		t.Fatalf("sha256 len = %d", len(ev.SHA256))
	}
	if ev.FileIno == 0 {
		t.Fatal("file ino should be recorded for resume identity")
	}

	events, err := env.Svc.ListChain(caseID)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != 1 || events[0].EventType != model.EventRegister ||
		events[0].Sequence != 1 || events[0].PrevDigest != zeroDigest {
		t.Fatalf("unexpected chain: %+v", events)
	}
	rep, err := env.Svc.VerifyChain(caseID)
	if err != nil {
		t.Fatal(err)
	}
	if !rep.Healthy {
		t.Fatalf("chain not healthy: %+v", rep.Issues)
	}
}

// 读取过程中文件被原地改写：登记必须失败（file_changed），
// 且数据库中不得留下任何证据基线。
func TestRegisterEvidence_ChangesDuringRead(t *testing.T) {
	env := NewEnv(t)
	caseID := env.CreateCase(t, "C-CHG")
	rel := env.WriteImage(t, "chg.raw", 4096*20)

	env.Svc.SetRegisterObserver(func(name string, offset int64, chunk []byte) error {
		if name == "chg.raw" && offset == 4096*5 {
			// 在第一遍哈希进行到第 6 块时，原地改写文件中段（保持大小不变）。
			p := filepath.Join(env.Root, rel)
			f, err := os.OpenFile(p, os.O_WRONLY, 0)
			if err != nil {
				return err
			}
			defer f.Close()
			if _, err := f.WriteAt([]byte("FORENSIC-TAMPER-0123456789"), 4096*3); err != nil {
				return err
			}
			return f.Sync()
		}
		return nil
	})

	_, err := env.Svc.RegisterEvidence(service.RegisterEvidenceInput{
		CaseID: caseID, RootName: env.RootName, RelPath: rel,
		Name: "chg.raw", Actor: "investigator",
	})
	if err == nil {
		t.Fatal("expected registration to fail because file changed during read")
	}
	var se *service.Error
	if !errors.As(err, &se) || se.Kind != service.KindFileChanged {
		t.Fatalf("want file_changed error, got %v", err)
	}

	// 错误基线绝不能落库。
	var count int64
	env.DB.Model(&model.Evidence{}).Where("case_id = ?", caseID).Count(&count)
	if count != 0 {
		t.Fatalf("evidence row saved despite change: %d", count)
	}
	events, _ := env.Svc.ListChain(caseID)
	if len(events) != 0 {
		t.Fatalf("no chain event may be appended on failed register, got %d", len(events))
	}
}

// 读取过程中文件被追加：大小变化，fstat 身份变化，登记失败。
func TestRegisterEvidence_AppendDuringRead(t *testing.T) {
	env := NewEnv(t)
	caseID := env.CreateCase(t, "C-APP")
	rel := env.WriteImage(t, "app.dd", 4096*4)

	env.Svc.SetRegisterObserver(func(name string, offset int64, chunk []byte) error {
		if name == "app.dd" && offset == 0 {
			f, err := os.OpenFile(filepath.Join(env.Root, rel), os.O_WRONLY|os.O_APPEND, 0)
			if err != nil {
				return err
			}
			defer f.Close()
			if _, err := f.Write(make([]byte, 123)); err != nil {
				return err
			}
			return f.Sync()
		}
		return nil
	})

	_, err := env.Svc.RegisterEvidence(service.RegisterEvidenceInput{
		CaseID: caseID, RootName: env.RootName, RelPath: rel,
		Name: "app.dd", Actor: "investigator",
	})
	if err == nil {
		t.Fatal("expected failure on append during read")
	}
	var se *service.Error
	if !errors.As(err, &se) || se.Kind != service.KindFileChanged {
		t.Fatalf("want file_changed, got %v", err)
	}
}

// 非 raw/dd 扩展名被拒绝。
func TestRegisterEvidence_RejectsNonRaw(t *testing.T) {
	env := NewEnv(t)
	caseID := env.CreateCase(t, "C-EXT")
	rel := env.WriteImage(t, "notes.txt", 16)

	_, err := env.Svc.RegisterEvidence(service.RegisterEvidenceInput{
		CaseID: caseID, RootName: env.RootName, RelPath: rel,
		Name: "notes.txt", Actor: "investigator",
	})
	var se *service.Error
	if !errors.As(err, &se) || se.Kind != service.KindValidation {
		t.Fatalf("want validation error, got %v", err)
	}
}
