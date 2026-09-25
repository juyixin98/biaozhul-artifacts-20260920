package trmerge

import (
	"path/filepath"
	"testing"
	"time"
)

func TestRunnerHappyPath(t *testing.T) {
	dir := t.TempDir()
	st, err := NewStore(filepath.Join(dir, "cache"))
	if err != nil {
		t.Fatal(err)
	}
	seed := []ShardSeed{{ShardID: "s1", TestIDs: []string{"t1"}}}
	meta, err := st.CreateRun("execute", seed, "", true)
	if err != nil {
		t.Fatal(err)
	}
	work := filepath.Join(dir, "work")
	rnr, err := NewRunner(st, meta.ID, work, []CommandSpec{{
		ShardID: "s1",
		Command: `echo '{"type":"attempt_started","test_id":"t1","attempt_id":"a1","attempt_no":1}'
echo '{"type":"attempt_result","test_id":"t1","attempt_id":"a1","attempt_no":1,"result":"passed"}'`,
	}})
	if err != nil {
		t.Fatal(err)
	}
	rnr.Start()
	rnr.Wait(true) // auto-finalizes

	sum, err := st.Summary(meta.ID)
	if err != nil {
		t.Fatal(err)
	}
	if testStatusOf(*sum, "t1") != StatusPassed {
		t.Fatalf("t1 = %s, full: %+v", testStatusOf(*sum, "t1"), sum)
	}
	if shardStatusOf(*sum, "s1") != StatusCompleted {
		t.Fatalf("shard = %s", shardStatusOf(*sum, "s1"))
	}
	if sum.Status != StatusCompleted {
		t.Fatalf("run = %s", sum.Status)
	}
}

func TestRunnerCrashExitCode(t *testing.T) {
	dir := t.TempDir()
	st, _ := NewStore(filepath.Join(dir, "cache"))
	seed := []ShardSeed{{ShardID: "s1", TestIDs: []string{"t1"}}}
	meta, _ := st.CreateRun("execute", seed, "", true)
	rnr, err := NewRunner(st, meta.ID, filepath.Join(dir, "work"), []CommandSpec{{
		ShardID: "s1",
		// started but no result, then process dies nonzero
		Command: `echo '{"type":"attempt_started","test_id":"t1","attempt_id":"a1","attempt_no":1}'
echo boom >&2
exit 42`,
	}})
	if err != nil {
		t.Fatal(err)
	}
	rnr.Start()
	rnr.Wait(true)

	sum, _ := st.Summary(meta.ID)
	if shardStatusOf(*sum, "s1") != StatusCrashed {
		t.Fatalf("shard = %s want crashed", shardStatusOf(*sum, "s1"))
	}
	if testStatusOf(*sum, "t1") != StatusIncomplete {
		t.Fatalf("t1 = %s want incomplete after crash", testStatusOf(*sum, "t1"))
	}
	if sum.Status != StatusFailed {
		t.Fatalf("run = %s want failed", sum.Status)
	}
	// stderr captured
	var sh ShardSnapshot
	for _, x := range sum.Shards {
		if x.ShardID == "s1" {
			sh = x
		}
	}
	if sh.ExitCode == nil || *sh.ExitCode != 42 {
		t.Fatalf("exit code = %v want 42", sh.ExitCode)
	}
}

func TestRunnerCrashThenRetryAcrossShards(t *testing.T) {
	// Executor s1 crashes mid t1; the retry is modeled as a second attempt
	// (could be a new shard or the same recovered one) and passes.
	dir := t.TempDir()
	st, _ := NewStore(filepath.Join(dir, "cache"))
	seed := []ShardSeed{{ShardID: "s1", TestIDs: []string{"t1"}}}
	meta, _ := st.CreateRun("execute", seed, "", true)
	rnr, err := NewRunner(st, meta.ID, filepath.Join(dir, "work"), []CommandSpec{{
		ShardID: "s1",
		Command: `echo '{"type":"attempt_started","test_id":"t1","attempt_id":"a1","attempt_no":1}'
echo '{"type":"attempt_result","test_id":"t1","attempt_id":"a1","attempt_no":1,"result":"failed"}'
echo '{"type":"attempt_started","test_id":"t1","attempt_id":"a2","attempt_no":2}'
echo '{"type":"attempt_result","test_id":"t1","attempt_id":"a2","attempt_no":2,"result":"passed"}'`,
	}})
	if err != nil {
		t.Fatal(err)
	}
	rnr.Start()
	rnr.Wait(true)
	sum, _ := st.Summary(meta.ID)
	if testStatusOf(*sum, "t1") != StatusPassed {
		t.Fatalf("t1 = %s, retry passing should win", testStatusOf(*sum, "t1"))
	}
}

func TestRunnerTimeoutCanceled(t *testing.T) {
	dir := t.TempDir()
	st, _ := NewStore(filepath.Join(dir, "cache"))
	seed := []ShardSeed{{ShardID: "s1", TestIDs: []string{"t1"}}}
	meta, _ := st.CreateRun("execute", seed, "", true)
	rnr, err := NewRunner(st, meta.ID, filepath.Join(dir, "work"), []CommandSpec{{
		ShardID: "s1",
		Command: `sleep 30`,
		Timeout: "200ms",
	}})
	if err != nil {
		t.Fatal(err)
	}
	rnr.Start()
	done := make(chan struct{})
	go func() { rnr.Wait(true); close(done) }()
	select {
	case <-done:
	case <-time.After(10 * time.Second):
		t.Fatal("runner did not return after timeout")
	}
	sum, _ := st.Summary(meta.ID)
	if shardStatusOf(*sum, "s1") != StatusCanceled {
		t.Fatalf("shard = %s want canceled on timeout", shardStatusOf(*sum, "s1"))
	}
}

func TestRunnerWorkDirIsSeparateFromCache(t *testing.T) {
	dir := t.TempDir()
	st, _ := NewStore(filepath.Join(dir, "cache"))
	meta, _ := st.CreateRun("execute", seed1(), "", true)
	_, err := NewRunner(st, meta.ID, filepath.Join(dir, "cache", "inside"), []CommandSpec{{
		ShardID: "s1", Command: "true",
	}})
	if err == nil {
		t.Fatal("runner with work dir inside cache must be refused")
	}
}

func TestRunnerExplicitCancelSIGTERM(t *testing.T) {
	dir := t.TempDir()
	st, _ := NewStore(filepath.Join(dir, "cache"))
	seed := []ShardSeed{{ShardID: "s1", TestIDs: []string{"t1"}}}
	meta, _ := st.CreateRun("execute", seed, "", false)
	rnr, err := NewRunner(st, meta.ID, filepath.Join(dir, "work"), []CommandSpec{{
		ShardID: "s1",
		// A fixture that traps TERM and exits 143, like a well-behaved executor.
		Command: `echo '{"type":"attempt_started","test_id":"t1","attempt_id":"a1","attempt_no":1}'
sleep 30 &
child=$!
trap 'kill $child 2>/dev/null; exit 143' TERM
wait $child`,
	}})
	if err != nil {
		t.Fatal(err)
	}
	rnr.Start()
	time.Sleep(300 * time.Millisecond) // let attempt_started land
	rnr.Cancel()
	rnr.Wait(false)
	if _, err := st.Finalize(meta.ID); err != nil {
		t.Fatal(err)
	}
	sum, _ := st.Summary(meta.ID)
	if shardStatusOf(*sum, "s1") != StatusCanceled {
		t.Fatalf("shard = %s want canceled after SIGTERM", shardStatusOf(*sum, "s1"))
	}
	if testStatusOf(*sum, "t1") != StatusCanceled {
		t.Fatalf("t1 = %s want canceled (no result before cancel)", testStatusOf(*sum, "t1"))
	}
}
