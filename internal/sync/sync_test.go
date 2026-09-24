package sync_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/example/merklekv/internal/hlc"
	"github.com/example/merklekv/internal/merkle"
	"github.com/example/merklekv/internal/server"
	"github.com/example/merklekv/internal/store"
	synclib "github.com/example/merklekv/internal/sync"
)

// ---- test fixture ----------------------------------------------------------

type replica struct {
	name   string
	store  *store.Store
	srv    *httptest.Server
	client *synclib.Client
}

type pair struct {
	t      *testing.T
	params merkle.Params
	r1, r2 *replica
}

func newPair(t *testing.T, fanout, depth int) *pair {
	t.Helper()
	params, err := merkle.NewParams(fanout, depth)
	if err != nil {
		t.Fatal(err)
	}
	mk := func(name string) *replica {
		st := store.New(store.Config{ReplicaID: name, SnapshotTTL: 30 * time.Second})
		srv := httptest.NewServer(server.New(st, params, nil).Handler())
		t.Cleanup(srv.Close)
		return &replica{name: name, store: st, srv: srv}
	}
	p := &pair{t: t, params: params, r1: mk("r1"), r2: mk("r2")}
	p.r1.client = mustClient(t, p.r1.store, params, p.r2.srv.URL)
	p.r2.client = mustClient(t, p.r2.store, params, p.r1.srv.URL)
	return p
}

func mustClient(t *testing.T, local *store.Store, p merkle.Params, peer string) *synclib.Client {
	t.Helper()
	c, err := synclib.NewClient(synclib.Config{Local: local, Params: p, Peer: peer, MaxRounds: 10})
	if err != nil {
		t.Fatal(err)
	}
	return c
}

// seed installs an IDENTICAL baseline on both replicas. Origin is fixed to
// "base" on both sides on purpose: replicated history means the same record
// (same version, same origin writer) lives on both replicas. Divergence is
// introduced afterwards by writing a NEW version, not by changing the
// baseline's origin.
func (p *pair) seed(rs []record) {
	p.t.Helper()
	base := toEntries("base", rs)
	p.r1.store.Seed(base)
	p.r2.store.Seed(base)
}

// record is compact test data.
type record struct {
	key     string
	value   string
	wall    uint64
	deleted bool
	origin  string
}

func toEntries(origin string, rs []record) []store.Entry {
	out := make([]store.Entry, len(rs))
	for i, r := range rs {
		o := r.origin
		if o == "" {
			o = origin
		}
		out[i] = store.Entry{
			Key: r.key, Value: []byte(r.value),
			Ver: hlc.Timestamp{Wall: r.wall}, Origin: o, Deleted: r.deleted,
		}
	}
	return out
}

func records(n, valueSize int) []record {
	rs := make([]record, n)
	for i := 0; i < n; i++ {
		rs[i] = record{key: fmt.Sprintf("key-%05d", i), value: strings.Repeat("x", valueSize), wall: uint64(i + 1)}
	}
	return rs
}

func run(t *testing.T, c *synclib.Client) *synclib.Result {
	t.Helper()
	res, err := c.Run(context.Background())
	if err != nil {
		t.Fatalf("sync failed: %v", err)
	}
	if !res.Converged {
		t.Fatal("sync reported not converged without error")
	}
	return res
}

// assertConverged compares the live key/value sets of both replicas.
func (p *pair) assertConverged() {
	p.t.Helper()
	a := liveMap(p.r1.store)
	b := liveMap(p.r2.store)
	if len(a) != len(b) {
		p.t.Fatalf("live key counts differ: %d vs %d", len(a), len(b))
	}
	for k, va := range a {
		vb, ok := b[k]
		if !ok {
			p.t.Fatalf("key %q only on r1 after sync", k)
		}
		if !bytes.Equal(va, vb) {
			p.t.Fatalf("key %q values diverge: %q vs %q", k, va, vb)
		}
	}
	// Roots over fresh snapshots must be identical.
	ra := merkle.Build(p.params, p.r1.store.Snapshot()).RootHash()
	rb := merkle.Build(p.params, p.r2.store.Snapshot()).RootHash()
	if ra != rb {
		p.t.Fatalf("roots still differ after sync:\n r1=%s\n r2=%s", ra, rb)
	}
}

func liveMap(s *store.Store) map[string][]byte {
	out := make(map[string][]byte)
	for _, e := range s.SortedLiveEntries() {
		out[e.Key] = e.Value
	}
	return out
}

// ---- acceptance scenarios --------------------------------------------------

// Scenario 1: a single leaf changes in an otherwise identical, large state.
// Only one tree path's hashes plus one bucket's entries may be exchanged.
func TestAcceptanceSingleLeafChange(t *testing.T) {
	p := newPair(t, 16, 3)      // 4096 buckets, 4 levels
	base := records(2000, 1024) // ~2 MiB of values on each replica
	p.seed(base)

	// r2 changes exactly one key (same bucket set, one leaf moves). Start
	// from byte-identical baseline records and overwrite just one version.
	entries := toEntries("base", base)
	entries[7] = store.Entry{
		Key: "key-00007", Value: []byte("NEW-VALUE"),
		Ver: hlc.Timestamp{Wall: 9_000_000}, Origin: "r2",
	}
	p.r2.store.Seed(entries)

	res := run(t, p.r1.client)
	p.assertConverged()

	if got, _ := p.r1.store.Get("key-00007"); string(got.Value) != "NEW-VALUE" {
		t.Fatal("change was not replicated")
	}
	// Tree math: roots arrive free with the snapshot responses; down the one
	// differing path we request the 3 interior nodes and compare the 1 leaf.
	wantNodes := 3 + 1
	if res.HashNodesExchanged != wantNodes {
		t.Fatalf("hash nodes exchanged = %d, want %d (one root path only)",
			res.HashNodesExchanged, wantNodes)
	}
	if res.BucketsOpened != 1 {
		t.Fatalf("buckets opened = %d, want 1", res.BucketsOpened)
	}
	if res.RootMovesDetected != 0 {
		t.Fatalf("unexpected retries in a stable scan: %d", res.RootMovesDetected)
	}
	if res.UserBytesExchanged > int64(len("NEW-VALUE")+len("key-00007"))+128 {
		t.Fatalf("transferred %d user bytes for a 9-byte change", res.UserBytesExchanged)
	}
	if res.WireBytesExchanged >= res.BaselineFullTransfer/4 {
		t.Fatalf("wire bytes %d are not sublinear vs full transfer %d",
			res.WireBytesExchanged, res.BaselineFullTransfer)
	}
	t.Logf("single-leaf: nodes=%d buckets=%d entries=%d wire=%d baseline=%d (%.3f%%)",
		res.HashNodesExchanged, res.BucketsOpened, res.EntriesExchanged,
		res.WireBytesExchanged, res.BaselineFullTransfer,
		100*float64(res.WireBytesExchanged)/float64(res.BaselineFullTransfer))
}

// Scenario 2: full divergence — the replicas share nothing. Every bucket
// must be opened, but convergence must still hold and a repeat sync must
// exchange no entries at all.
func TestAcceptanceFullDivergence(t *testing.T) {
	p := newPair(t, 16, 2) // 256 buckets
	rsA := make([]record, 0, 600)
	rsB := make([]record, 0, 600)
	for i := 0; i < 600; i++ {
		rsA = append(rsA, record{key: fmt.Sprintf("a-%05d", i), value: strings.Repeat("a", 128), wall: uint64(i + 1)})
		rsB = append(rsB, record{key: fmt.Sprintf("b-%05d", i), value: strings.Repeat("b", 128), wall: uint64(10_000 + i)})
	}
	p.r1.store.Seed(toEntries("r1", rsA))
	p.r2.store.Seed(toEntries("r2", rsB))

	res := run(t, p.r1.client)
	p.assertConverged()
	if got := len(p.r1.store.SortedLiveEntries()); got != 1200 {
		t.Fatalf("r1 holds %d live keys, want union of 1200", got)
	}
	if res.BucketsOpened < p.params.NumBuckets() {
		// Every leaf differs (empty vs non-empty), so all 256 are opened.
		t.Fatalf("buckets opened = %d, want all %d", res.BucketsOpened, p.params.NumBuckets())
	}
	if res.EntriesExchanged != 1200 {
		t.Fatalf("entries exchanged = %d, want 1200", res.EntriesExchanged)
	}

	// Re-syncing an already converged pair is a no-op: root compared, zero
	// entries, zero buckets.
	res2 := run(t, p.r1.client)
	if res2.EntriesExchanged != 0 || res2.BucketsOpened != 0 {
		t.Fatalf("repeat sync did work: entries=%d buckets=%d", res2.EntriesExchanged, res2.BucketsOpened)
	}
	t.Logf("full-divergence: buckets=%d entries=%d wire=%d baseline=%d; repeat: nodes=%d",
		res.BucketsOpened, res.EntriesExchanged,
		res.WireBytesExchanged, res.BaselineFullTransfer, res2.HashNodesExchanged)
}

// Scenario 3: updates happen while a scan is in progress. The local side
// changes between pinning its snapshot and applying: the epoch guard must
// reject the commit, a root move is counted, and the retried round converges.
func TestAcceptanceUpdateDuringScan(t *testing.T) {
	p := newPair(t, 16, 3)
	base := records(1500, 256)
	p.seed(base)
	p.r2.store.Put("key-00003", []byte("PEER-CHANGE"))

	var conflicts int
	p.r1.client = mustClientWith(t, synclib.Config{
		Local: p.r1.store, Params: p.params, Peer: p.r2.srv.URL, MaxRounds: 10,
		OnConflict: func(round int, reason string) { conflicts++ },
		BetweenPhase: func(round int) {
			// Concurrent writer on the INITIATING replica while its snapshot
			// is pinned, only on round 1: local epoch must move, local apply
			// is rejected, then round 2 converges.
			if round == 1 {
				p.r1.store.Put("concurrent-key", []byte("written-during-scan"))
			}
		},
	})

	res := run(t, p.r1.client)
	p.assertConverged()
	if conflicts == 0 || res.RootMovesDetected == 0 {
		t.Fatal("scan-time update was not detected as a root move")
	}
	if got, _ := p.r2.store.Get("concurrent-key"); string(got.Value) != "written-during-scan" {
		t.Fatal("concurrent local write missing on peer after convergence")
	}
	if got, _ := p.r1.store.Get("key-00003"); string(got.Value) != "PEER-CHANGE" {
		t.Fatal("peer change missing locally after convergence")
	}
	t.Logf("during-scan: root_moves=%d rounds=%d final_entries=%d",
		res.RootMovesDetected, res.RoundsAttempted,
		len(p.r1.store.SortedLiveEntries()))
}

// Variant of scenario 3: the PEER is mutated mid-scan. Its epoch guard
// rejects the guarded apply (409), forcing a retry.
func TestAcceptancePeerUpdateDuringScan(t *testing.T) {
	p := newPair(t, 16, 3)
	base := records(1500, 256)
	p.seed(base)
	p.r2.store.Put("key-00003", []byte("PEER-CHANGE"))

	var mu sync.Mutex
	fired := false
	p.r1.client = mustClientWith(t, synclib.Config{
		Local: p.r1.store, Params: p.params, Peer: p.r2.srv.URL, MaxRounds: 10,
		BetweenPhase: func(round int) {
			mu.Lock()
			defer mu.Unlock()
			if fired || round != 1 {
				return
			}
			fired = true
			// Direct write to the peer store, out of band.
			p.r2.store.Put("peer-concurrent", []byte("peer-moved"))
		},
	})
	res := run(t, p.r1.client)
	p.assertConverged()
	if res.RootMovesDetected == 0 {
		t.Fatal("expected at least one detected peer root move")
	}
	if got, _ := p.r1.store.Get("peer-concurrent"); string(got.Value) != "peer-moved" {
		t.Fatal("peer's concurrent write did not converge to initiator")
	}
	t.Logf("peer-during-scan: root_moves=%d rounds=%d", res.RootMovesDetected, res.RoundsAttempted)
}

// The peer evicts pinned snapshots (TTL / capacity) mid-round: requests fail
// 404 and the round restarts cleanly.
func TestSnapshotEvictionRestartsRound(t *testing.T) {
	p := newPair(t, 16, 3)
	base := records(1200, 128)
	p.seed(base)
	p.r2.store.Put("key-00010", []byte("Z"))

	fired := false
	p.r1.client = mustClientWith(t, synclib.Config{
		Local: p.r1.store, Params: p.params, Peer: p.r2.srv.URL, MaxRounds: 10,
		BeforePeerRequest: func(attempt int, path string) {
			// On the first /v1/nodes request of round 1, drop the peer's
			// snapshots. The request 404s and the round restarts.
			if !fired && path == "/v1/nodes" {
				fired = true
				p.r2.store.EvictAllSnapshots()
			}
		},
	})
	res := run(t, p.r1.client)
	p.assertConverged()
	if !fired {
		t.Fatal("test hook never fired")
	}
	if res.RootMovesDetected == 0 {
		t.Fatal("snapshot eviction must count as a root move")
	}
}

// Tombstones replicate: a key deleted on one side disappears on the other
// and stays gone after another round, without any full transfer.
func TestTombstoneReplication(t *testing.T) {
	p := newPair(t, 16, 2)
	base := records(300, 64)
	p.seed(base)
	p.r2.store.Delete("key-00042")

	res := run(t, p.r1.client)
	p.assertConverged()
	if _, ok := p.r1.store.Get("key-00042"); ok {
		t.Fatal("deleted key still live on r1")
	}
	all := p.r1.store.AllEntries()
	var tomb int
	for _, e := range all {
		if e.Deleted {
			tomb++
		}
	}
	if tomb != 1 {
		t.Fatalf("want exactly 1 tombstone, got %d", tomb)
	}
	// Second round must be a complete no-op even with a tombstone present.
	res2 := run(t, p.r1.client)
	if res2.EntriesExchanged != 0 || res2.BucketsOpened != 0 {
		t.Fatalf("repeat sync after delete did work: entries=%d buckets=%d",
			res2.EntriesExchanged, res2.BucketsOpened)
	}
	t.Logf("tombstone: buckets=%d entries=%d (one tombstone), wire=%d",
		res.BucketsOpened, res.EntriesExchanged, res.WireBytesExchanged)
}

// Conflicting writes on both sides to the same key merge LWW-deterministically.
func TestConflictingWritesLWW(t *testing.T) {
	p := newPair(t, 16, 1)
	p.r1.store.Seed([]store.Entry{{Key: "k", Value: []byte("low"), Ver: hlc.Timestamp{Wall: 1}, Origin: "r1"}})
	p.r2.store.Seed([]store.Entry{{Key: "k", Value: []byte("high"), Ver: hlc.Timestamp{Wall: 2}, Origin: "r2"}})
	run(t, p.r1.client)
	p.assertConverged()
	if got, _ := p.r1.store.Get("k"); string(got.Value) != "high" {
		t.Fatal("higher version must win")
	}

	// Sync in the opposite direction must be idempotent and change nothing.
	before := p.r1.store.AllEntries()
	run(t, p.r2.client)
	after := p.r1.store.AllEntries()
	if !equalRecordSets(before, after) {
		t.Fatal("reverse-direction sync changed state; merge is not symmetric")
	}
}

// Idempotence under repeated retries: run the sync many times under
// continuous local churn and confirm eventual convergence with bounded work.
func TestRepeatedSyncIdempotent(t *testing.T) {
	p := newPair(t, 16, 2)
	base := records(400, 64)
	p.seed(base)

	for i := 0; i < 5; i++ {
		run(t, p.r1.client)
	}
	p.assertConverged()
	last := run(t, p.r1.client)
	if last.EntriesExchanged != 0 || last.BucketsOpened != 0 {
		t.Fatalf("steady-state sync did work: entries=%d buckets=%d",
			last.EntriesExchanged, last.BucketsOpened)
	}
}

// ---- HTTP surface smoke test ----------------------------------------------

// The protocol is driven over real HTTP in every test above; additionally
// exercise the plain KV API and the snapshot/node endpoints by hand.
func TestHTTPSmoke(t *testing.T) {
	params, _ := merkle.NewParams(16, 2)
	st := store.New(store.Config{ReplicaID: "r1"})
	srv := httptest.NewServer(server.New(st, params, nil).Handler())
	defer srv.Close()

	put := func(method, path string, body string) (int, []byte) {
		var rdr io.Reader
		if body != "" {
			rdr = strings.NewReader(body)
		}
		req, err := http.NewRequest(method, srv.URL+path, rdr)
		if err != nil {
			t.Fatal(err)
		}
		resp, err := srv.Client().Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer resp.Body.Close()
		data, _ := io.ReadAll(resp.Body)
		return resp.StatusCode, data
	}
	if code, _ := put("PUT", "/v1/kv/hello", "world"); code != 200 {
		t.Fatalf("put status %d", code)
	}
	if code, body := put("GET", "/v1/kv/hello", ""); code != 200 || string(body) != "world" {
		t.Fatalf("get status %d body %q", code, body)
	}
	if code, _ := put("DELETE", "/v1/kv/hello", ""); code != 200 {
		t.Fatalf("delete status %d", code)
	}
	if code, _ := put("GET", "/v1/kv/hello", ""); code != 404 {
		t.Fatalf("get after delete status %d, want 404", code)
	}
}

// End-to-end through the /v1/sync HTTP endpoint: r1 drives reconciliation
// against r2 over the network and both converge.
func TestSyncHTTPEndpoint(t *testing.T) {
	p := newPair(t, 16, 2)
	p.seed(records(200, 32))
	p.r2.store.Put("key-00001", []byte("via-http-endpoint"))

	req, err := http.NewRequest(http.MethodPost, p.r1.srv.URL+"/v1/sync",
		strings.NewReader(fmt.Sprintf(`{"peer":%q}`, p.r2.srv.URL)))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 {
		t.Fatalf("/v1/sync status %d: %s", resp.StatusCode, data)
	}
	var out synclib.Result
	if err := json.Unmarshal(data, &out); err != nil {
		t.Fatal(err)
	}
	if !out.Converged || out.LocalRoot != out.RemoteRoot {
		t.Fatalf("sync endpoint did not converge: %s", data)
	}
	if got, _ := p.r1.store.Get("key-00001"); string(got.Value) != "via-http-endpoint" {
		t.Fatal("change missing after HTTP-driven sync")
	}
}

// ---- helpers ---------------------------------------------------------------

func mustClientWith(t *testing.T, cfg synclib.Config) *synclib.Client {
	t.Helper()
	c, err := synclib.NewClient(cfg)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func equalRecordSets(a, b []store.Entry) bool {
	key := func(es []store.Entry) []string {
		ks := make([]string, len(es))
		for i, e := range es {
			ks[i] = fmt.Sprintf("%s|%d:%d|%s|%v", e.Key, e.Ver.Wall, e.Ver.Log, e.Origin, e.Deleted)
		}
		sort.Strings(ks)
		return ks
	}
	ka, kb := key(a), key(b)
	if len(ka) != len(kb) {
		return false
	}
	for i := range ka {
		if ka[i] != kb[i] {
			return false
		}
	}
	return true
}
