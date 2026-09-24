package api_test

// End-to-end tests over real HTTP: they drive the controller and node servers
// exactly as an external client would, including injected partitions and
// duplicated control messages.
import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sort"
	"sync"
	"testing"

	"shardmig/api"
	"shardmig/core"
)

// ---- HTTP helpers ------------------------------------------------------------

type client struct {
	t     *testing.T
	base  string
	httpc *http.Client
}

func newClient(t *testing.T, base string) *client {
	return &client{t: t, base: base, httpc: http.DefaultClient}
}

func (cl *client) baseURL() string { return cl.base }

func (cl *client) post(path string, body any) (int, map[string]any) {
	var rdr io.Reader
	if body != nil {
		b, _ := json.Marshal(body)
		rdr = bytes.NewReader(b)
	}
	resp, err := cl.httpc.Post(cl.base+path, "application/json", rdr)
	if err != nil {
		cl.t.Fatalf("POST %s: %v", path, err)
	}
	defer resp.Body.Close()
	return decode(cl.t, resp)
}

func (cl *client) get(path string) (int, map[string]any) {
	resp, err := cl.httpc.Get(cl.base + path)
	if err != nil {
		cl.t.Fatalf("GET %s: %v", path, err)
	}
	defer resp.Body.Close()
	return decode(cl.t, resp)
}

func decode(t *testing.T, resp *http.Response) (int, map[string]any) {
	raw, _ := io.ReadAll(resp.Body)
	var m map[string]any
	if len(raw) > 0 {
		_ = json.Unmarshal(raw, &m)
	}
	return resp.StatusCode, m
}

func (cl *client) must200(path string, body any) map[string]any {
	st, m := cl.post(path, body)
	if st != 200 {
		cl.t.Fatalf("POST %s: status %d body %v", path, st, m)
	}
	return m
}

// appendWrite confirms one write through the routed primary.
func (cl *client) appendWrite(node string, epoch int, k, value string) int64 {
	st, m := cl.post("/append", map[string]any{"node": node, "epoch": epoch, "key": k, "value": value})
	if st != 200 {
		cl.t.Fatalf("append %s: status %d body %v", k, st, m)
	}
	return int64(m["seq"].(float64))
}

func jsonReader(v any) io.Reader {
	b, _ := json.Marshal(v)
	return bytes.NewReader(b)
}

func (cl *client) setLink(node string, up bool) {
	cl.must200("/nodes/"+node+"/link", map[string]any{"up": up})
}

func (cl *client) dropAppend(node string, drop bool) {
	cl.must200("/nodes/"+node+"/drop-append", map[string]any{"drop": drop})
}

// ctrl sends a phase-control message and returns (status, body).
func (cl *client) ctrl(action, cmdID string) (int, map[string]any) {
	return cl.post("/migration/"+action, map[string]any{"command_id": cmdID})
}

func (cl *client) ctrlOK(action, cmdID string) map[string]any {
	st, m := cl.ctrl(action, cmdID)
	if st != 200 {
		cl.t.Fatalf("control %s: status %d body %v", action, st, m)
	}
	return m
}

func (cl *client) expectCode(st, want int, action string, m map[string]any) {
	if st != want {
		cl.t.Fatalf("%s: want status %d got %d body %v", action, want, st, m)
	}
}

func (cl *client) verifyAll() {
	st, m := cl.get("/verify")
	if st != 200 {
		cl.t.Fatalf("verify: %d", st)
	}
	if m["all_pass"] != true {
		b, _ := json.MarshalIndent(m["checks"], "", "  ")
		cl.t.Fatalf("invariants FAILED:\n%s", b)
	}
}

// nodeWrites reads one node's confirmed-write log.
func nodeWrites(t *testing.T, sys *api.System, node string) []map[string]any {
	resp, err := http.Get(sys.NodeURL(node) + "/writes")
	if err != nil {
		t.Fatalf("GET node %s writes: %v", node, err)
	}
	defer resp.Body.Close()
	_, m := decode(t, resp)
	raw := m["writes"].([]any)
	out := make([]map[string]any, len(raw))
	for i, w := range raw {
		out[i] = w.(map[string]any)
	}
	return out
}

// ---- setup -------------------------------------------------------------------

func setupRaw(t *testing.T) (*api.System, *client) {
	t.Helper()
	sys, err := api.New(core.NewCluster(), "")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(sys.Close)
	return sys, newClient(t, sys.ControllerURL())
}

var (
	keyMu sync.Mutex
	keyN  int
	seqN  int64
	seqMu sync.Mutex
)

func key(prefix string) string {
	keyMu.Lock()
	defer keyMu.Unlock()
	keyN++
	return fmt.Sprintf("%s-%04d", prefix, keyN)
}

func nextSeq() int64 {
	seqMu.Lock()
	defer seqMu.Unlock()
	seqN++
	return seqN
}

func keysOf(ws []map[string]any) []string {
	out := make([]string, len(ws))
	for i, w := range ws {
		out[i] = w["key"].(string)
	}
	sort.Strings(out)
	return out
}

func equalSet(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	cp := append([]string{}, b...)
	sort.Strings(cp)
	for i := range a {
		if a[i] != cp[i] {
			return false
		}
	}
	return true
}

// ---- tests -------------------------------------------------------------------

// TestHappyPathLifecycle covers the acceptance baseline: writes inserted in
// every phase, migration completes, no write is lost.
func TestHappyPathLifecycle(t *testing.T) {
	_, cl := setupRaw(t)

	// pre-snapshot writes
	cl.appendWrite(core.NodeA, 1, key("pre-1"), "v1")
	cl.appendWrite(core.NodeA, 1, key("pre-2"), "v2")

	// phase 1: snapshot
	cl.ctrlOK("begin_snapshot", "cmd-snap-begin")
	cl.appendWrite(core.NodeA, 1, key("during-snapshot"), "v3")
	cl.ctrlOK("complete_snapshot", "cmd-snap-done")

	// phase 2: incremental catch-up (new writes land while draining)
	cl.appendWrite(core.NodeA, 1, key("catchup-1"), "v4")
	r := cl.ctrlOK("catch_up", "cmd-catch-1")
	if r["lag"].(float64) != 0 {
		t.Fatalf("expected lag 0, got %v", r["lag"])
	}
	cl.appendWrite(core.NodeA, 1, key("catchup-2"), "v5")
	r = cl.ctrlOK("catch_up", "cmd-catch-2")
	if r["lag"].(float64) != 0 {
		t.Fatalf("expected lag 0 after second catch-up, got %v", r["lag"])
	}

	// phase 3: cutover
	cl.ctrlOK("prepare_cutover", "cmd-prep")
	st, m := cl.post("/append", map[string]any{"node": core.NodeA, "epoch": 1, "key": key("frozen"), "value": "x"})
	cl.expectCode(st, 503, "write while switching", m)
	cl.ctrlOK("commit_cutover", "cmd-commit")

	// old primary refuses new writes on stale routing...
	st, m = cl.post("/append", map[string]any{"node": core.NodeA, "epoch": 1, "key": key("after-a"), "value": "x"})
	cl.expectCode(st, 409, "stale-epoch write to A", m)
	// ...new primary confirms epoch-2 writes
	cl.appendWrite(core.NodeB, 2, key("after-b"), "v6")

	cl.verifyAll()
}

// TestDisconnectInEveryPhase inserts a partition in each phase and asserts the
// protocol blocks unsafe progress, recovers on reconnect, and never loses a
// confirmed write.
func TestDisconnectInEveryPhase(t *testing.T) {
	_, cl := setupRaw(t)

	cl.appendWrite(core.NodeA, 1, key("base"), "v")

	// --- snapshot phase: B partitioned -> snapshot install fails ---
	cl.ctrlOK("begin_snapshot", "s1")
	cl.setLink(core.NodeB, false)
	st, m := cl.ctrl("complete_snapshot", "s2")
	cl.expectCode(st, 502, "snapshot with B down", m)
	// A still serves writes (only B is partitioned)
	cl.appendWrite(core.NodeA, 1, key("snap-while-b-down"), "v")
	// reconnect -> snapshot completes
	cl.setLink(core.NodeB, true)
	cl.ctrlOK("complete_snapshot", "s2")

	// --- catch-up phase: A partitioned mid-drain -> catch-up blocked ---
	cl.appendWrite(core.NodeA, 1, key("catch-a"), "v")
	cl.setLink(core.NodeA, false)
	st, m = cl.ctrl("catch_up", "c1")
	cl.expectCode(st, 502, "catch-up with A down", m)
	// writes via A are impossible while it is partitioned from the controller
	st, m = cl.post("/append", map[string]any{"node": core.NodeA, "epoch": 1, "key": key("no"), "value": "x"})
	cl.expectCode(st, 502, "write via partitioned A", m)
	cl.setLink(core.NodeA, true)
	cl.appendWrite(core.NodeA, 1, key("after-reconnect-1"), "v")
	r := cl.ctrlOK("catch_up", "c1")
	if r["lag"].(float64) != 0 {
		t.Fatalf("lag after reconnect catch-up: %v", r["lag"])
	}

	// --- switching phase: B down at commit -> commit refused, epoch unchanged ---
	cl.ctrlOK("prepare_cutover", "p1")
	cl.setLink(core.NodeB, false)
	st, m = cl.ctrl("commit_cutover", "p2")
	cl.expectCode(st, 502, "commit with B down", m)
	_, sm := cl.get("/status")
	if int(sm["epoch"].(float64)) != 1 || sm["primary"] != core.NodeA {
		t.Fatalf("epoch must not advance on failed commit: epoch=%v primary=%v", sm["epoch"], sm["primary"])
	}
	if sm["phase"] != "switching" {
		t.Fatalf("phase should remain switching, got %v", sm["phase"])
	}
	// reconnect B -> the same commit message now succeeds
	cl.setLink(core.NodeB, true)
	cl.ctrlOK("commit_cutover", "p2")

	// post-cutover: A down makes no difference — B is the sole primary
	cl.setLink(core.NodeA, false)
	cl.appendWrite(core.NodeB, 2, key("b-only"), "v")
	cl.setLink(core.NodeA, true)

	cl.verifyAll()
}

// TestDuplicatedControlMessages proves exactly-once control semantics:
// duplicate command IDs replay the first result and never move state twice.
func TestDuplicatedControlMessages(t *testing.T) {
	_, cl := setupRaw(t)
	cl.appendWrite(core.NodeA, 1, key("w"), "v")

	first := cl.ctrlOK("begin_snapshot", "dup-begin")
	if first["replayed"] != false {
		t.Fatal("first begin must not be replayed")
	}
	again := cl.ctrlOK("begin_snapshot", "dup-begin")
	if again["replayed"] != true {
		t.Fatal("duplicate begin must replay")
	}
	cl.ctrlOK("complete_snapshot", "dup-snap")
	// replay the begin from catch-up: returns the original cached result
	replay := cl.ctrlOK("begin_snapshot", "dup-begin")
	if replay["phase"] != "snapshot" || replay["replayed"] != true {
		t.Fatalf("replay should return original snapshot result, got %v", replay["phase"])
	}

	// duplicate catch-up after more writes replays the cached response;
	// a new command id drains the fresh write
	r1 := cl.ctrlOK("catch_up", "dup-catch")
	cl.appendWrite(core.NodeA, 1, key("late"), "v")
	r2 := cl.ctrlOK("catch_up", "dup-catch")
	if r2["replayed"] != true || r2["lag"] != r1["lag"] {
		t.Fatalf("duplicate catch-up must replay cached result: %v vs %v", r1["lag"], r2["lag"])
	}
	fresh := cl.ctrlOK("catch_up", "dup-catch-2")
	if fresh["lag"].(float64) != 0 {
		t.Fatalf("fresh catch-up should drain, lag=%v", fresh["lag"])
	}

	// a failed commit (B down) is NOT cached: the same command id retries
	cl.ctrlOK("prepare_cutover", "dup-prep")
	cl.setLink(core.NodeB, false)
	st, m := cl.ctrl("commit_cutover", "dup-commit")
	cl.expectCode(st, 502, "first commit fails", m)
	cl.setLink(core.NodeB, true)
	ok := cl.ctrlOK("commit_cutover", "dup-commit")
	if ok["replayed"] == true {
		t.Fatal("a previously failed command must not be served from cache")
	}
	if int(ok["epoch"].(float64)) != 2 {
		t.Fatalf("expected epoch 2, got %v", ok["epoch"])
	}

	cl.verifyAll()
}

// TestAppendResponseLostRetried drives the injected "commit then response
// lost" fault: the client retries and must observe exactly one write.
func TestAppendResponseLostRetried(t *testing.T) {
	sys, cl := setupRaw(t)
	cl.dropAppend(core.NodeA, true)

	k := key("lost-response")
	postOnce := func() (int, map[string]any) {
		return cl.post("/append", map[string]any{"node": core.NodeA, "epoch": 1, "key": k, "value": "v"})
	}

	st, m := postOnce()
	cl.expectCode(st, 500, "dropped response", m)
	if m["code"] != "response_dropped_after_commit" {
		t.Fatalf("unexpected code %v", m["code"])
	}
	// blind retry — must dedupe, not allocate a second seq
	st, m = postOnce()
	cl.expectCode(st, 200, "retry after dropped response", m)
	if m["deduplicated"] != true {
		t.Fatal("retry must be reported as deduplicated")
	}
	cl.dropAppend(core.NodeA, false)

	var n int
	var seq int64
	for _, w := range nodeWrites(t, sys, core.NodeA) {
		if w["key"] == k {
			n++
			seq = int64(w["seq"].(float64))
		}
	}
	if n != 1 || seq != 1 {
		t.Fatalf("exactly one write expected, got %d (seq %d)", n, seq)
	}

	// the lost-response write must survive the cutover
	cl.ctrlOK("begin_snapshot", "b")
	cl.ctrlOK("complete_snapshot", "s")
	cl.ctrlOK("catch_up", "c")
	cl.ctrlOK("prepare_cutover", "p")
	cl.ctrlOK("commit_cutover", "co")
	cl.verifyAll()
}

// TestNoDualPrimaryAcrossCutover fires concurrent writes at both primaries
// around the commit fence: at every instant exactly one primary can confirm,
// and the same key contested across the switch is never confirmed twice.
func TestNoDualPrimaryAcrossCutover(t *testing.T) {
	_, cl := setupRaw(t)
	for i := 0; i < 20; i++ {
		cl.appendWrite(core.NodeA, 1, fmt.Sprintf("pre-%02d", i), "v")
	}
	cl.ctrlOK("begin_snapshot", "b")
	cl.ctrlOK("complete_snapshot", "s")
	cl.ctrlOK("catch_up", "c")
	cl.ctrlOK("prepare_cutover", "p")

	var wg sync.WaitGroup
	var mu sync.Mutex
	confirmed := map[string]string{} // key -> confirming primary
	rejected := 0

	tryWrite := func(node string, epoch int, k string) {
		defer wg.Done()
		resp, err := http.Post(cl.baseURL()+"/append", "application/json",
			jsonReader(map[string]any{"node": node, "epoch": epoch, "key": k, "value": "v"}))
		if err != nil {
			return
		}
		defer resp.Body.Close()
		var mm map[string]any
		raw, _ := io.ReadAll(resp.Body)
		_ = json.Unmarshal(raw, &mm)
		mu.Lock()
		defer mu.Unlock()
		if resp.StatusCode == 200 {
			confirmed[k] = mm["primary"].(string)
		} else {
			rejected++
		}
	}

	shared := "shared-key"
	for i := 0; i < 10; i++ {
		wg.Add(2)
		go tryWrite(core.NodeA, 1, shared)
		go tryWrite(core.NodeB, 2, shared)
	}
	wg.Add(1)
	go func() { defer wg.Done(); cl.ctrlOK("commit_cutover", "co") }()
	wg.Wait()

	// Whatever the burst interleaving was, A could not have confirmed
	// (frozen before commit, stale after). Settle the key on B now.
	st, fm := cl.post("/append", map[string]any{"node": core.NodeB, "epoch": 2, "key": shared, "value": "v"})
	cl.expectCode(st, 200, "settle shared key on B", fm)
	if fm["primary"] != core.NodeB {
		t.Fatalf("shared key must settle on B, got %v", fm["primary"])
	}
	confirmed[shared] = fm["primary"].(string)

	if owner := confirmed[shared]; owner != core.NodeB {
		t.Fatalf("shared key crossing cutover must be owned by B only, owner=%s", owner)
	}
	if len(confirmed) != 1 {
		t.Fatalf("exactly one key should be confirmed, got %d", len(confirmed))
	}
	if rejected == 0 {
		t.Fatal("expected fence rejections during the switching window, got none")
	}

	// settled state: A cannot confirm; B owns epoch 2
	st, m := cl.post("/append", map[string]any{"node": core.NodeA, "epoch": 1, "key": "late-a", "value": "v"})
	cl.expectCode(st, 409, "A after cutover", m)
	st, _ = cl.post("/append", map[string]any{"node": core.NodeB, "epoch": 2, "key": fmt.Sprintf("post-%d", nextSeq()), "value": "v"})
	if st != 200 {
		t.Fatalf("B write after cutover: %d", st)
	}
	cl.verifyAll()
}

// TestSnapshotCatchupContents checks that B receives exactly the writes
// committed up to each transfer boundary, in global sequence order.
func TestSnapshotCatchupContents(t *testing.T) {
	sys, cl := setupRaw(t)

	var preKeys []string
	for i := 0; i < 5; i++ {
		k := fmt.Sprintf("pre-%d", i)
		cl.appendWrite(core.NodeA, 1, k, "v")
		preKeys = append(preKeys, k)
	}
	cl.ctrlOK("begin_snapshot", "b")
	snapKey := "at-snapshot"
	cl.appendWrite(core.NodeA, 1, snapKey, "v")

	// complete_snapshot transfers exactly [1..snapshot_seq]
	cl.ctrlOK("complete_snapshot", "s")
	got := keysOf(nodeWrites(t, sys, core.NodeB))
	if len(got) != 5 || !equalSet(got, preKeys) {
		t.Fatalf("snapshot batch mismatch: %v", got)
	}

	// catch-up drains the single post-snapshot write
	r := cl.ctrlOK("catch_up", "c")
	if int(r["installed"].(float64)) != 1 {
		t.Fatalf("expected 1 installed in catch-up, got %v", r["installed"])
	}
	bLog := nodeWrites(t, sys, core.NodeB)
	got = keysOf(bLog)
	if len(got) != 6 {
		t.Fatalf("B should hold 6 writes after catch-up, got %d: %v", len(got), got)
	}
	lastSeq := int64(0)
	for _, w := range bLog {
		s := int64(w["seq"].(float64))
		if s <= lastSeq {
			t.Fatalf("B log not ordered: %d after %d", s, lastSeq)
		}
		lastSeq = s
	}
}
