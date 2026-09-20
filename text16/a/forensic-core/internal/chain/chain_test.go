package chain_test

import (
	"fmt"
	"sync"
	"testing"

	"forensiccore/internal/chain"
	"forensiccore/internal/models"
	"forensiccore/internal/testutil"
)

func newCase(t *testing.T, store *chain.Store) models.Case {
	t.Helper()
	cs := models.Case{Name: "case-" + t.Name()}
	if err := store.DB.Create(&cs).Error; err != nil {
		t.Fatal(err)
	}
	return cs
}

func mustAppend(t *testing.T, store *chain.Store, caseID uint, typ, actor string, payload map[string]any) {
	t.Helper()
	if _, err := store.Append(caseID, typ, actor, payload); err != nil {
		t.Fatalf("append: %v", err)
	}
}

func issueKinds(issues []chain.Issue) map[string]int {
	m := map[string]int{}
	for _, i := range issues {
		m[i.Kind]++
	}
	return m
}

func TestAppendAndVerifyClean(t *testing.T) {
	db := testutil.NewDB(t)
	store := chain.NewStore(db)
	cs := newCase(t, store)

	mustAppend(t, store, cs.ID, models.EventRegister, "inv1", map[string]any{"sha256": "abc", "size": 10})
	mustAppend(t, store, cs.ID, models.EventNote, "ana1", map[string]any{"text": "looks fine"})
	mustAppend(t, store, cs.ID, models.EventTransfer, "inv1", map[string]any{"to": "lab"})

	var events []models.ChainEvent
	if err := db.Where("case_id = ?", cs.ID).Order("seq ASC").Find(&events).Error; err != nil {
		t.Fatal(err)
	}
	if len(events) != 3 {
		t.Fatalf("got %d events, want 3", len(events))
	}
	for i, ev := range events {
		if ev.Seq != uint64(i+1) {
			t.Fatalf("event %d seq = %d", i, ev.Seq)
		}
		if i > 0 && ev.PrevHash != events[i-1].Hash {
			t.Fatalf("event %d prev_hash not linked", i)
		}
	}
	issues, err := store.Verify(cs.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(issues) != 0 {
		t.Fatalf("clean chain reported issues: %+v", issues)
	}
}

// 并发追加不能分叉：序号必须恰好为 1..N 且链校验通过。
func TestConcurrentAppendNoFork(t *testing.T) {
	db := testutil.NewDB(t)
	store := chain.NewStore(db)
	cs := newCase(t, store)

	const workers = 8
	const perWorker = 10
	var wg sync.WaitGroup
	errs := make(chan error, workers*perWorker)
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func(w int) {
			defer wg.Done()
			for i := 0; i < perWorker; i++ {
				_, err := store.Append(cs.ID, models.EventNote, fmt.Sprintf("user-%d", w),
					map[string]any{"text": fmt.Sprintf("note %d-%d", w, i)})
				if err != nil {
					errs <- err
				}
			}
		}(w)
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		t.Fatalf("concurrent append failed: %v", err)
	}

	var seqs []uint64
	if err := db.Model(&models.ChainEvent{}).Where("case_id = ?", cs.ID).
		Order("seq ASC").Pluck("seq", &seqs).Error; err != nil {
		t.Fatal(err)
	}
	if len(seqs) != workers*perWorker {
		t.Fatalf("got %d events, want %d", len(seqs), workers*perWorker)
	}
	for i, s := range seqs {
		if s != uint64(i+1) {
			t.Fatalf("fork or gap: seqs[%d] = %d", i, s)
		}
	}
	issues, err := store.Verify(cs.ID)
	if err != nil {
		t.Fatal(err)
	}
	if len(issues) != 0 {
		t.Fatalf("concurrent chain invalid: %+v", issues)
	}
}

// 篡改事件内容必须被检出。
func TestTamperDetected(t *testing.T) {
	db := testutil.NewDB(t)
	store := chain.NewStore(db)
	cs := newCase(t, store)
	mustAppend(t, store, cs.ID, models.EventRegister, "inv1", map[string]any{"sha256": "aaa"})
	mustAppend(t, store, cs.ID, models.EventTransfer, "inv1", map[string]any{"to": "lab-A"})
	mustAppend(t, store, cs.ID, models.EventNote, "ana1", map[string]any{"text": "ok"})

	// 直接改库：把移交对象从 lab-A 改成 lab-B（模拟恶意篡改）。
	if err := db.Model(&models.ChainEvent{}).Where("case_id = ? AND seq = 2", cs.ID).
		Update("payload", `{"to":"lab-B"}`).Error; err != nil {
		t.Fatal(err)
	}
	issues, err := store.Verify(cs.ID)
	if err != nil {
		t.Fatal(err)
	}
	kinds := issueKinds(issues)
	if kinds["tampered"] == 0 {
		t.Fatalf("tamper not detected: %+v", issues)
	}
}

// 删除中间事件必须检出缺失。
func TestMissingEventDetected(t *testing.T) {
	db := testutil.NewDB(t)
	store := chain.NewStore(db)
	cs := newCase(t, store)
	for i := 0; i < 4; i++ {
		mustAppend(t, store, cs.ID, models.EventNote, "ana1", map[string]any{"text": fmt.Sprintf("n%d", i)})
	}
	if err := db.Where("case_id = ? AND seq = 2", cs.ID).Delete(&models.ChainEvent{}).Error; err != nil {
		t.Fatal(err)
	}
	issues, err := store.Verify(cs.ID)
	if err != nil {
		t.Fatal(err)
	}
	kinds := issueKinds(issues)
	if kinds["missing"] == 0 {
		t.Fatalf("missing event not detected: %+v", issues)
	}
}

// 交换两条事件的序号（乱序）必须被检出。
func TestOutOfOrderDetected(t *testing.T) {
	db := testutil.NewDB(t)
	store := chain.NewStore(db)
	cs := newCase(t, store)
	for i := 0; i < 3; i++ {
		mustAppend(t, store, cs.ID, models.EventNote, "ana1", map[string]any{"text": fmt.Sprintf("n%d", i)})
	}
	// 交换 seq 2 与 3。
	if err := db.Model(&models.ChainEvent{}).Where("case_id = ? AND seq = 2", cs.ID).Update("seq", 99).Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Model(&models.ChainEvent{}).Where("case_id = ? AND seq = 3", cs.ID).Update("seq", 2).Error; err != nil {
		t.Fatal(err)
	}
	if err := db.Model(&models.ChainEvent{}).Where("case_id = ? AND seq = 99", cs.ID).Update("seq", 3).Error; err != nil {
		t.Fatal(err)
	}
	issues, err := store.Verify(cs.ID)
	if err != nil {
		t.Fatal(err)
	}
	kinds := issueKinds(issues)
	if kinds["out_of_order"] == 0 && kinds["tampered"] == 0 {
		t.Fatalf("reordering not detected: %+v", issues)
	}
}
