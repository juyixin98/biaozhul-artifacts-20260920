package servicetest

import (
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/example/forensiccore/internal/model"
)

// waitForStatus 轮询作业直到进入任一终结/指定状态。
func waitForStatus(t *testing.T, env *Env, jobID uint, want ...string) model.VerifyJob {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		var job model.VerifyJob
		if err := env.DB.First(&job, jobID).Error; err != nil {
			t.Fatal(err)
		}
		for _, w := range want {
			if job.Status == w {
				return job
			}
		}
		time.Sleep(5 * time.Millisecond)
	}
	var job model.VerifyJob
	_ = env.DB.First(&job, jobID).Error
	t.Fatalf("job %d never reached %v, last status=%s err=%s",
		jobID, want, job.Status, job.LastError)
	return model.VerifyJob{}
}

// 正常复核：镜像未变，作业 verified，链上追加 verify(verified)。
func TestVerifyJob_Verified(t *testing.T) {
	env := NewEnv(t)
	caseID := env.CreateCase(t, "J-OK")
	rel := env.WriteImage(t, "ok.raw", 4096*7)
	evID := env.Register(t, caseID, rel)

	job, err := env.Svc.StartVerify(caseID, evID, "investigator")
	if err != nil {
		t.Fatal(err)
	}
	fin := waitForStatus(t, env, job.ID, model.VerifyStatusVerified)
	if fin.FinalSHA256 == "" {
		t.Fatal("final digest empty")
	}
	events, _ := env.Svc.ListChain(caseID)
	var found bool
	for _, e := range events {
		if e.EventType == model.EventVerify {
			found = true
		}
	}
	if !found {
		t.Fatal("verify event missing from chain")
	}
}

// 中断后恢复：在第 3 块后钩子停止作业，随后恢复并完成。
func TestVerifyJob_ResumeAfterInterrupt(t *testing.T) {
	env := NewEnv(t)
	caseID := env.CreateCase(t, "J-RES")
	rel := env.WriteImage(t, "res.raw", 4096*10)
	evID := env.Register(t, caseID, rel)

	var stops int32
	env.Svc.SetJobHook(func(jobID uint, processed, total int64) error {
		// 仅对第一个作业、在处理到 3 块时返回一次停止。
		if atomic.LoadInt32(&stops) == 0 && processed == 4096*3 {
			atomic.StoreInt32(&stops, 1)
			return errHookStop
		}
		return nil
	})

	job, err := env.Svc.StartVerify(caseID, evID, "investigator")
	if err != nil {
		t.Fatal(err)
	}
	interrupted := waitForStatus(t, env, job.ID, model.VerifyStatusInterrupted)
	if interrupted.ProcessedSize != 4096*3 {
		t.Fatalf("processed = %d, want %d", interrupted.ProcessedSize, 4096*3)
	}

	// 恢复：不修改文件，应验证已处理块后继续。
	if _, err := env.Svc.ResumeJob(caseID, job.ID); err != nil {
		t.Fatal(err)
	}
	fin := waitForStatus(t, env, job.ID, model.VerifyStatusVerified)
	if fin.ProcessedSize != 4096*10 {
		t.Fatalf("final processed = %d", fin.ProcessedSize)
	}
}

// 恢复前“已处理部分”被修改：恢复必须检测到块摘要变化并判失败，
// 不能因为文件名/大小相同就复用进度。
func TestVerifyJob_ModifiedBeforeResume(t *testing.T) {
	env := NewEnv(t)
	caseID := env.CreateCase(t, "J-MOD")
	rel := env.WriteImage(t, "mod.raw", 4096*10)
	evID := env.Register(t, caseID, rel)

	var stops int32
	env.Svc.SetJobHook(func(jobID uint, processed, total int64) error {
		if atomic.LoadInt32(&stops) == 0 && processed == 4096*4 {
			atomic.StoreInt32(&stops, 1)
			return errHookStop
		}
		return nil
	})
	job, err := env.Svc.StartVerify(caseID, evID, "investigator")
	if err != nil {
		t.Fatal(err)
	}
	waitForStatus(t, env, job.ID, model.VerifyStatusInterrupted)

	// 在已处理区间（第 2 块）原地改写，文件大小、路径、文件名均不变。
	p := filepath.Join(env.Root, rel)
	f, err := os.OpenFile(p, os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteAt([]byte("PREFIX-WAS-MODIFIED-BEFORE-RESUME-XX"), 4096*2); err != nil {
		t.Fatal(err)
	}
	_ = f.Sync()
	_ = f.Close()

	if _, err := env.Svc.ResumeJob(caseID, job.ID); err != nil {
		t.Fatal(err)
	}
	fin := waitForStatus(t, env, job.ID, model.VerifyStatusFailed)
	if fin.LastError == "" {
		t.Fatal("expected failure reason")
	}
	events, _ := env.Svc.ListChain(caseID)
	var failedEv *model.ChainEvent
	for i := range events {
		if events[i].EventType == model.EventVerify {
			failedEv = &events[i]
		}
	}
	if failedEv == nil {
		t.Fatal("expected verify failed event")
	}
}

// 恢复前路径被另一个文件替换（同文件名、同大小、不同 inode）：身份校验失败。
func TestVerifyJob_ReplacedByIdentityBeforeResume(t *testing.T) {
	env := NewEnv(t)
	caseID := env.CreateCase(t, "J-SWP")
	rel := env.WriteImage(t, "swp.raw", 4096*8)
	evID := env.Register(t, caseID, rel)

	var stops int32
	env.Svc.SetJobHook(func(jobID uint, processed, total int64) error {
		if atomic.LoadInt32(&stops) == 0 && processed == 4096*2 {
			atomic.StoreInt32(&stops, 1)
			return errHookStop
		}
		return nil
	})
	job, err := env.Svc.StartVerify(caseID, evID, "investigator")
	if err != nil {
		t.Fatal(err)
	}
	waitForStatus(t, env, job.ID, model.VerifyStatusInterrupted)

	// 用全新文件替换同一路径：保持相同大小，但 dev/ino 必然不同。
	p := filepath.Join(env.Root, rel)
	tmp := p + ".new"
	tf, err := os.Create(tmp)
	if err != nil {
		t.Fatal(err)
	}
	if err := WriteDeterministic(tf, 4096*8); err != nil {
		t.Fatal(err)
	}
	// 写成不同内容以模拟“另一个镜像”，再覆盖原路径（rename 改变 inode）。
	if _, err := tf.WriteAt([]byte("DIFFERENT-IMAGE-CONTENT"), 0); err != nil {
		t.Fatal(err)
	}
	_ = tf.Close()
	if err := os.Rename(tmp, p); err != nil {
		t.Fatal(err)
	}

	if _, err := env.Svc.ResumeJob(caseID, job.ID); err != nil {
		t.Fatal(err)
	}
	fin := waitForStatus(t, env, job.ID, model.VerifyStatusFailed)
	if !strings.Contains(fin.LastError, "identity") {
		t.Fatalf("expected identity mismatch, got: %s", fin.LastError)
	}
}

// 复核完成后再篡改镜像：新作业必须 digest mismatch 失败，链校验仍健康（失败也被记录）。
func TestVerifyJob_TamperedAfterBaseline(t *testing.T) {
	env := NewEnv(t)
	caseID := env.CreateCase(t, "J-TAMP")
	rel := env.WriteImage(t, "tamp.raw", 4096*6)
	evID := env.Register(t, caseID, rel)

	job, err := env.Svc.StartVerify(caseID, evID, "investigator")
	if err != nil {
		t.Fatal(err)
	}
	waitForStatus(t, env, job.ID, model.VerifyStatusVerified)

	// 篡改原始镜像倒数第二块（保持大小不变）。
	p := filepath.Join(env.Root, rel)
	f, err := os.OpenFile(p, os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.WriteAt([]byte("TAMPERED-AFTER-BASELINE-0000"), int64(4096*5)); err != nil {
		t.Fatal(err)
	}
	_ = f.Close()

	// 作业表需要新作业；旧的 verified 不阻碍，但 active 计数只看活动状态。
	job2, err := env.Svc.StartVerify(caseID, evID, "investigator")
	if err != nil {
		t.Fatal(err)
	}
	fin := waitForStatus(t, env, job2.ID, model.VerifyStatusFailed)
	if !strings.Contains(fin.LastError, "digest mismatch") {
		t.Fatalf("expected digest mismatch, got: %s", fin.LastError)
	}
}
