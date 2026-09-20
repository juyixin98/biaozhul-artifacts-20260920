package servicetest

import (
	"encoding/json"
	"fmt"
	"sync"
	"testing"

	"github.com/example/forensiccore/internal/chain"
	"github.com/example/forensiccore/internal/model"
	"github.com/example/forensiccore/internal/service"
)

// 并发追加不能分叉：N 个 goroutine 同时追加备注，必须得到 1..N 的连续序号、
// 每条 prev 指向前一条真实摘要，链校验健康。
func TestChain_ConcurrentAppendNoFork(t *testing.T) {
	env := NewEnv(t)
	caseID := env.CreateCase(t, "CH-CONC")

	const n = 30
	var wg sync.WaitGroup
	errs := make(chan error, n)
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			payload, _ := json.Marshal(map[string]any{"text": fmt.Sprintf("note-%d", i)})
			_, err := chain.Append(env.DB, chain.AppendInput{
				CaseID: caseID, Type: model.EventNote,
				Actor: "investigator", Payload: payload,
			})
			errs <- err
		}(i)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatalf("append: %v", err)
		}
	}

	events, err := env.Svc.ListChain(caseID)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) != n {
		t.Fatalf("event count = %d, want %d", len(events), n)
	}
	rep, err := env.Svc.VerifyChain(caseID)
	if err != nil {
		t.Fatal(err)
	}
	if !rep.Healthy {
		t.Fatalf("chain forked/corrupted under concurrency: %+v", rep.Issues)
	}
}

// 直接篡改数据库中某条事件的规范化内容：必须报 tampered。
func TestChain_DetectsContentTampering(t *testing.T) {
	env := NewEnv(t)
	caseID := env.CreateCase(t, "CH-TAMP")
	rel := env.WriteImage(t, "x.raw", 4096*2)
	env.Register(t, caseID, rel)

	// 追加两条备注后篡改中间内容。
	for i := 0; i < 2; i++ {
		if _, err := env.Svc.AddNote(service.NoteInput{
			CaseID: caseID, Text: fmt.Sprintf("note-%d", i), Actor: "analyst",
		}); err != nil {
			t.Fatal(err)
		}
	}
	if err := env.DB.Model(&model.ChainEvent{}).
		Where("case_id = ? AND sequence = ?", caseID, 2).
		Update("canonical", `{"tampered":true}`).Error; err != nil {
		t.Fatal(err)
	}
	rep, err := env.Svc.VerifyChain(caseID)
	if err != nil {
		t.Fatal(err)
	}
	if rep.Healthy {
		t.Fatal("expected unhealthy chain after tamper")
	}
	if !hasIssue(rep, chain.IssueTampered, 2) {
		t.Fatalf("expected tampered issue at seq 2, got %+v", rep.Issues)
	}
}

// 删除中间事件：必须检测到 missing，并因此断链。
func TestChain_DetectsGap(t *testing.T) {
	env := NewEnv(t)
	caseID := env.CreateCase(t, "CH-GAP")
	rel := env.WriteImage(t, "g.raw", 4096)
	env.Register(t, caseID, rel)
	for i := 0; i < 3; i++ {
		_, _ = env.Svc.AddNote(service.NoteInput{
			CaseID: caseID, Text: fmt.Sprintf("n%d", i), Actor: "analyst",
		})
	}
	// 删除序号 2（制造缺口）。
	if err := env.DB.Delete(&model.ChainEvent{},
		"case_id = ? AND sequence = ?", caseID, 2).Error; err != nil {
		t.Fatal(err)
	}
	rep, err := env.Svc.VerifyChain(caseID)
	if err != nil {
		t.Fatal(err)
	}
	if !hasIssue(rep, chain.IssueGap, 2) {
		t.Fatalf("expected missing at seq 2, got %+v", rep.Issues)
	}
	if !hasIssue(rep, chain.IssueBrokenLink, 3) {
		t.Fatalf("expected broken link at seq 3, got %+v", rep.Issues)
	}
}

// 篡改某事件的 prev_digest（模拟乱序/重排）：必须检测 broken_link。
func TestChain_DetectsReorder(t *testing.T) {
	env := NewEnv(t)
	caseID := env.CreateCase(t, "CH-ORD")
	rel := env.WriteImage(t, "o.raw", 4096)
	env.Register(t, caseID, rel)
	_, _ = env.Svc.AddNote(service.NoteInput{CaseID: caseID, Text: "a", Actor: "analyst"})
	_, _ = env.Svc.AddNote(service.NoteInput{CaseID: caseID, Text: "b", Actor: "analyst"})

	// 把序号 3 的 prev 改成任意值（摘要内容不变但链接断裂）。
	if err := env.DB.Model(&model.ChainEvent{}).
		Where("case_id = ? AND sequence = ?", caseID, 3).
		Update("prev_digest", chain.ZeroDigest).Error; err != nil {
		t.Fatal(err)
	}
	rep, err := env.Svc.VerifyChain(caseID)
	if err != nil {
		t.Fatal(err)
	}
	if !hasIssue(rep, chain.IssueBrokenLink, 3) {
		t.Fatalf("expected broken link at seq 3, got %+v", rep.Issues)
	}
}

func hasIssue(rep *chain.Report, code string, seq int64) bool {
	for _, is := range rep.Issues {
		if is.Code == code && is.Sequence == seq {
			return true
		}
	}
	return false
}
