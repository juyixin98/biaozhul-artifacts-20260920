package evidence_test

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"forensiccore/internal/chain"
	"forensiccore/internal/evidence"
	"forensiccore/internal/hashutil"
	"forensiccore/internal/models"
	"forensiccore/internal/safeopen"
	"forensiccore/internal/testutil"
)

const chunkSize = 1024 // 测试用小分块，小文件也能产生多个块

type fixture struct {
	svc  *evidence.Service
	root string
	cs   models.Case
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	db := testutil.NewDB(t)
	rootDir := t.TempDir()
	root, err := safeopen.NewRoot(rootDir)
	if err != nil {
		t.Fatal(err)
	}
	svc := evidence.NewService(db, root, chain.NewStore(db), chunkSize)
	cs := models.Case{Name: "case-" + t.Name()}
	if err := db.Create(&cs).Error; err != nil {
		t.Fatal(err)
	}
	return &fixture{svc: svc, root: rootDir, cs: cs}
}

// writeImage 生成确定内容的小镜像。
func (f *fixture) writeImage(t *testing.T, name string, size int, seed byte) string {
	t.Helper()
	data := make([]byte, size)
	for i := range data {
		data[i] = seed + byte(i%251)
	}
	p := filepath.Join(f.root, name)
	if err := os.WriteFile(p, data, 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

func sha256File(t *testing.T, p string) string {
	t.Helper()
	raw, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

func TestRegisterSuccess(t *testing.T) {
	fx := newFixture(t)
	p := fx.writeImage(t, "disk.dd", 3*chunkSize+17, 5)

	ev, err := fx.svc.Register(fx.cs.ID, "disk.dd", "inv1")
	if err != nil {
		t.Fatalf("register: %v", err)
	}
	if ev.SHA256 != sha256File(t, p) {
		t.Fatal("baseline sha256 mismatch")
	}
	if ev.Size != 3*chunkSize+17 {
		t.Fatalf("size = %d", ev.Size)
	}
	// 登记事件已进入证据链且链完整。
	var n int64
	fx.svc.DB.Model(&models.ChainEvent{}).
		Where("case_id = ? AND type = ?", fx.cs.ID, models.EventRegister).Count(&n)
	if n != 1 {
		t.Fatalf("register events = %d", n)
	}
	issues, err := fx.svc.Chain.Verify(fx.cs.ID)
	if err != nil || len(issues) != 0 {
		t.Fatalf("chain verify: %v %+v", err, issues)
	}
}

// 登记计算期间文件被修改：必须失败且不留任何基线。
func TestRegisterFailsWhenFileChangesDuringRead(t *testing.T) {
	fx := newFixture(t)
	fx.writeImage(t, "disk.dd", 4*chunkSize, 9)

	mutated := false
	fx.svc.AfterChunkHook = func(done int64) {
		if done == chunkSize && !mutated {
			mutated = true
			mf, err := os.OpenFile(filepath.Join(fx.root, "disk.dd"), os.O_WRONLY|os.O_APPEND, 0)
			if err != nil {
				t.Errorf("mutate: %v", err)
				return
			}
			mf.Write([]byte("changed-while-hashing"))
			mf.Close()
		}
	}
	_, err := fx.svc.Register(fx.cs.ID, "disk.dd", "inv1")
	if !errors.Is(err, hashutil.ErrChangedDuringRead) {
		t.Fatalf("err = %v, want ErrChangedDuringRead", err)
	}
	var count int64
	fx.svc.DB.Model(&models.Evidence{}).Count(&count)
	if count != 0 {
		t.Fatalf("bad baseline was saved: %d evidence rows", count)
	}
	fx.svc.DB.Model(&models.ChainEvent{}).Count(&count)
	if count != 0 {
		t.Fatalf("chain event saved for failed registration: %d", count)
	}
}

func TestVerifyJobMatch(t *testing.T) {
	fx := newFixture(t)
	fx.writeImage(t, "disk.dd", 2500, 11)
	ev, err := fx.svc.Register(fx.cs.ID, "disk.dd", "inv1")
	if err != nil {
		t.Fatal(err)
	}
	job, err := fx.svc.CreateVerifyJob(ev.ID)
	if err != nil {
		t.Fatal(err)
	}
	job, err = fx.svc.RunJob(context.Background(), job.ID, "inv1")
	if err != nil {
		t.Fatal(err)
	}
	if job.Status != models.JobCompleted || job.Result != models.ResultMatch {
		t.Fatalf("job = %+v", job)
	}
	if job.ProcessedBytes != job.TotalSize {
		t.Fatalf("processed = %d, total = %d", job.ProcessedBytes, job.TotalSize)
	}
}

// 登记后文件内容被改动（同长度）：复核结论必须为 mismatch。
func TestVerifyJobMismatch(t *testing.T) {
	fx := newFixture(t)
	p := fx.writeImage(t, "disk.dd", 2500, 13)
	ev, err := fx.svc.Register(fx.cs.ID, "disk.dd", "inv1")
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(p)
	raw[100] ^= 0xFF // 原地翻转一个字节
	if err := os.WriteFile(p, raw, 0o644); err != nil {
		t.Fatal(err)
	}
	job, _ := fx.svc.CreateVerifyJob(ev.ID)
	job, err = fx.svc.RunJob(context.Background(), job.ID, "inv1")
	if err != nil {
		t.Fatal(err)
	}
	if job.Status != models.JobCompleted || job.Result != models.ResultMismatch {
		t.Fatalf("job = %+v", job)
	}
}

// 中断后恢复：取消 → paused → 恢复 → completed match。
func TestJobInterruptAndResume(t *testing.T) {
	fx := newFixture(t)
	fx.writeImage(t, "disk.dd", 4*chunkSize, 21)
	ev, err := fx.svc.Register(fx.cs.ID, "disk.dd", "inv1")
	if err != nil {
		t.Fatal(err)
	}
	job, err := fx.svc.CreateVerifyJob(ev.ID)
	if err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	fx.svc.AfterChunkHook = func(done int64) {
		if done == 2*chunkSize {
			cancel() // 模拟处理到一半被中断
		}
	}
	job, err = fx.svc.RunJob(ctx, job.ID, "inv1")
	if err != nil {
		t.Fatal(err)
	}
	if job.Status != models.JobPaused {
		t.Fatalf("status = %s, want paused", job.Status)
	}
	if job.ProcessedBytes != 2*chunkSize {
		t.Fatalf("processed = %d, want %d", job.ProcessedBytes, 2*chunkSize)
	}

	// 恢复运行（不再有取消钩子）。
	fx.svc.AfterChunkHook = nil
	job, err = fx.svc.RunJob(context.Background(), job.ID, "inv1")
	if err != nil {
		t.Fatal(err)
	}
	if job.Status != models.JobCompleted || job.Result != models.ResultMatch {
		t.Fatalf("resumed job = %+v", job)
	}
	// 证据链包含 register 与 verify 事件且完整。
	issues, err := fx.svc.Chain.Verify(fx.cs.ID)
	if err != nil || len(issues) != 0 {
		t.Fatalf("chain verify: %v %+v", err, issues)
	}
}

// 恢复前修改已处理部分：续算必须失败（不能只靠文件名/大小判断）。
func TestResumeFailsWhenProcessedPartModified(t *testing.T) {
	fx := newFixture(t)
	p := fx.writeImage(t, "disk.dd", 4*chunkSize, 33)
	ev, err := fx.svc.Register(fx.cs.ID, "disk.dd", "inv1")
	if err != nil {
		t.Fatal(err)
	}
	job, _ := fx.svc.CreateVerifyJob(ev.ID)

	ctx, cancel := context.WithCancel(context.Background())
	fx.svc.AfterChunkHook = func(done int64) {
		if done == 2*chunkSize {
			cancel()
		}
	}
	job, err = fx.svc.RunJob(ctx, job.ID, "inv1")
	if err != nil {
		t.Fatal(err)
	}
	if job.Status != models.JobPaused {
		t.Fatalf("status = %s", job.Status)
	}
	fx.svc.AfterChunkHook = nil

	// 中断期间篡改已处理的前 1KiB（保持文件总大小与 inode 不变）。
	fh, err := os.OpenFile(p, os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := fh.WriteAt([]byte("evil"), 0); err != nil {
		t.Fatal(err)
	}
	fh.Close()

	fx.svc.AfterChunkHook = nil
	job, err = fx.svc.RunJob(context.Background(), job.ID, "inv1")
	if err != nil {
		t.Fatal(err)
	}
	if job.Status != models.JobFailed {
		t.Fatalf("status = %s, want failed", job.Status)
	}
	if job.Error == "" {
		t.Fatal("expected error message on failed resume")
	}
	t.Logf("resume correctly refused: %s", job.Error)
}

// 进度记录（分块摘要）被篡改：恢复时的前缀重算比对必须拒绝。
func TestResumeFailsWhenChunkRecordTampered(t *testing.T) {
	fx := newFixture(t)
	fx.writeImage(t, "disk.dd", 4*chunkSize, 47)
	ev, err := fx.svc.Register(fx.cs.ID, "disk.dd", "inv1")
	if err != nil {
		t.Fatal(err)
	}
	job, _ := fx.svc.CreateVerifyJob(ev.ID)

	ctx, cancel := context.WithCancel(context.Background())
	fx.svc.AfterChunkHook = func(done int64) {
		if done == 2*chunkSize {
			cancel()
		}
	}
	job, err = fx.svc.RunJob(ctx, job.ID, "inv1")
	if err != nil {
		t.Fatal(err)
	}
	if job.Status != models.JobPaused {
		t.Fatalf("status = %s", job.Status)
	}
	fx.svc.AfterChunkHook = nil

	// 篡改已持久化的第 0 块摘要（文件本身未动，时间戳与基线一致）。
	if err := fx.svc.DB.Model(&models.ChunkHash{}).
		Where("owner_type = ? AND owner_id = ? AND seq = 0", "job", job.ID).
		Update("sha256", "0000000000000000000000000000000000000000000000000000000000000000").Error; err != nil {
		t.Fatal(err)
	}
	job, err = fx.svc.RunJob(context.Background(), job.ID, "inv1")
	if err != nil {
		t.Fatal(err)
	}
	if job.Status != models.JobFailed {
		t.Fatalf("status = %s, want failed", job.Status)
	}
	t.Logf("resume correctly refused: %s", job.Error)
}

// 恢复前整个文件被替换（inode 变化）：文件身份校验必须拒绝。
func TestResumeFailsWhenFileReplaced(t *testing.T) {
	fx := newFixture(t)
	p := fx.writeImage(t, "disk.dd", 4*chunkSize, 41)
	ev, err := fx.svc.Register(fx.cs.ID, "disk.dd", "inv1")
	if err != nil {
		t.Fatal(err)
	}
	job, _ := fx.svc.CreateVerifyJob(ev.ID)

	ctx, cancel := context.WithCancel(context.Background())
	fx.svc.AfterChunkHook = func(done int64) { cancel() }
	job, err = fx.svc.RunJob(ctx, job.ID, "inv1")
	if err != nil {
		t.Fatal(err)
	}
	if job.Status != models.JobPaused {
		t.Fatalf("status = %s", job.Status)
	}
	fx.svc.AfterChunkHook = nil

	// 删除并重建同名同大小文件 → 新 inode。
	if err := os.Remove(p); err != nil {
		t.Fatal(err)
	}
	fx.writeImage(t, "disk.dd", 4*chunkSize, 41)

	job, err = fx.svc.RunJob(context.Background(), job.ID, "inv1")
	if err != nil {
		t.Fatal(err)
	}
	if job.Status != models.JobFailed {
		t.Fatalf("status = %s, want failed", job.Status)
	}
	t.Logf("resume correctly refused: %s", job.Error)
}

// 复核过程中文件被修改：结果作废，作业失败。
func TestJobFailsWhenFileChangesDuringRun(t *testing.T) {
	fx := newFixture(t)
	p := fx.writeImage(t, "disk.dd", 4*chunkSize, 55)
	ev, err := fx.svc.Register(fx.cs.ID, "disk.dd", "inv1")
	if err != nil {
		t.Fatal(err)
	}
	job, _ := fx.svc.CreateVerifyJob(ev.ID)

	mutated := false
	fx.svc.AfterChunkHook = func(done int64) {
		if done == chunkSize && !mutated {
			mutated = true
			mf, _ := os.OpenFile(p, os.O_WRONLY|os.O_APPEND, 0)
			mf.Write([]byte("x"))
			mf.Close()
		}
	}
	job, err = fx.svc.RunJob(context.Background(), job.ID, "inv1")
	if err != nil {
		t.Fatal(err)
	}
	if job.Status != models.JobFailed {
		t.Fatalf("status = %s, want failed (err=%q)", job.Status, job.Error)
	}
}

// 崩溃恢复：running 作业被标记为 paused。
func TestRecoverInterruptedJobs(t *testing.T) {
	fx := newFixture(t)
	fx.writeImage(t, "disk.dd", 100, 7)
	ev, err := fx.svc.Register(fx.cs.ID, "disk.dd", "inv1")
	if err != nil {
		t.Fatal(err)
	}
	job, _ := fx.svc.CreateVerifyJob(ev.ID)
	fx.svc.DB.Model(&models.VerifyJob{}).Where("id = ?", job.ID).Update("status", models.JobRunning)

	if err := fx.svc.RecoverInterruptedJobs(); err != nil {
		t.Fatal(err)
	}
	got, _ := fx.svc.GetJob(job.ID)
	if got.Status != models.JobPaused {
		t.Fatalf("status = %s, want paused", got.Status)
	}
	// 恢复后可以正常跑完。
	got, err = fx.svc.RunJob(context.Background(), got.ID, "inv1")
	if err != nil {
		t.Fatal(err)
	}
	if got.Status != models.JobCompleted || got.Result != models.ResultMatch {
		t.Fatalf("job = %+v", got)
	}
}

// 路径越界与扩展名限制在服务层同样生效。
func TestRegisterRejectsUnsafePaths(t *testing.T) {
	fx := newFixture(t)
	outside := t.TempDir()
	os.WriteFile(filepath.Join(outside, "out.dd"), []byte("x"), 0o644)
	os.Symlink(filepath.Join(outside, "out.dd"), filepath.Join(fx.root, "link.dd"))
	os.WriteFile(filepath.Join(fx.root, "notes.txt"), []byte("x"), 0o644)

	for _, rel := range []string{
		"../" + filepath.Base(outside) + "/out.dd",
		"link.dd",
		"notes.txt",
		"/etc/hostname",
	} {
		if _, err := fx.svc.Register(fx.cs.ID, rel, "inv1"); err == nil {
			t.Fatalf("%q: expected error", rel)
		} else {
			t.Logf("%q rejected: %v", rel, err)
		}
	}
	var count int64
	fx.svc.DB.Model(&models.Evidence{}).Count(&count)
	if count != 0 {
		t.Fatalf("unsafe registrations saved: %d", count)
	}
}

// 多次复核与备注后链仍完整，事件类型齐全。
func TestChainCoversAllEventTypes(t *testing.T) {
	fx := newFixture(t)
	fx.writeImage(t, "disk.dd", 1500, 77)
	ev, err := fx.svc.Register(fx.cs.ID, "disk.dd", "inv1")
	if err != nil {
		t.Fatal(err)
	}
	job, _ := fx.svc.CreateVerifyJob(ev.ID)
	if _, err := fx.svc.RunJob(context.Background(), job.ID, "inv1"); err != nil {
		t.Fatal(err)
	}
	if _, err := fx.svc.Chain.Append(fx.cs.ID, models.EventTransfer, "inv1", map[string]any{"to": "lab"}); err != nil {
		t.Fatal(err)
	}
	if _, err := fx.svc.Chain.Append(fx.cs.ID, models.EventNote, "ana1", map[string]any{"text": "checked"}); err != nil {
		t.Fatal(err)
	}
	var types []string
	fx.svc.DB.Model(&models.ChainEvent{}).Where("case_id = ?", fx.cs.ID).Order("seq").Pluck("type", &types)
	want := []string{models.EventRegister, models.EventVerify, models.EventTransfer, models.EventNote}
	if fmt.Sprint(types) != fmt.Sprint(want) {
		t.Fatalf("types = %v, want %v", types, want)
	}
	issues, _ := fx.svc.Chain.Verify(fx.cs.ID)
	if len(issues) != 0 {
		t.Fatalf("chain invalid: %+v", issues)
	}
}
