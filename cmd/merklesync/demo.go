package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	"merklesync/internal/merkle"
	"merklesync/internal/store"
	"merklesync/internal/syncapi"
)

func must(err error) {
	if err != nil {
		panic(err)
	}
}

func seedCommon(ctx context.Context, c *syncapi.Client, n int, valueSize int) {
	val := make([]byte, valueSize)
	for i := range val {
		val[i] = 'x'
	}
	for i := 0; i < n; i++ {
		key := fmt.Sprintf("key-%04d", i)
		_, err := c.Put(ctx, key, string(val), 1)
		must(err)
	}
}

// naiveFullTransfer measures what a "ship the whole store" approach would
// cost: one GET /snapshot of the peer. It is the baseline the Merkle sync is
// contrasted against.
func naiveFullTransfer(ctx context.Context, peerURL string) syncapi.Stats {
	c := syncapi.NewClient(peerURL)
	_, err := c.Snapshot(ctx)
	must(err)
	return c.Stats()
}

func printJSON(label string, v any) {
	fmt.Printf("  %s:\n", label)
	raw, _ := json.MarshalIndent(v, "    ", "  ")
	fmt.Println(string(raw))
}

func cmdDemo(args []string) {
	fs := flag.NewFlagSet("demo", flag.ExitOnError)
	n := fs.Int("seed", 200, "number of common seed keys")
	_ = fs.Parse(args)

	ctx := context.Background()

	local := store.New()
	peer := store.New()
	localURL, localSrv := startHTTPServer(local)
	peerURL, peerSrv := startHTTPServer(peer)
	defer func() { _ = localSrv.Close(); _ = peerSrv.Close() }()

	results := make(map[string]any)

	// ---------------- scenario 1: single-leaf change ----------------
	fmt.Println("== 场景 1：单叶变化（peer 修改一个已有 key）==")
	lc := syncapi.NewClient(localURL)
	pc := syncapi.NewClient(peerURL)
	must(lc.Reset(ctx))
	must(pc.Reset(ctx))
	seedCommon(ctx, lc, *n, 100)
	seedCommon(ctx, pc, *n, 100)

	// Mutate exactly one key on the peer (same bucket/leaf as its old value).
	target := fmt.Sprintf("key-%04d", *n/3)
	_, err := pc.Put(ctx, target, "the-value-changed", 2)
	must(err)

	baseline1 := naiveFullTransfer(ctx, peerURL)
	syncer1 := syncapi.NewSyncer(syncapi.DirectLocal{St: local}, syncapi.NewClient(peerURL))
	res1, err := syncer1.Sync(ctx)
	must(err)
	if !res1.Converged {
		panic("scenario 1 did not converge")
	}
	e1, _ := local.Get(target)
	if e1.Value != "the-value-changed" || e1.Version != 2 {
		panic(fmt.Sprintf("scenario 1 wrong value: %+v", e1))
	}
	printJSON("朴素全量拉取 (GET /snapshot) 开销", baseline1)
	printJSON("Merkle 同步结果", struct {
		*syncapi.SyncStats
		Wire syncapi.Stats `json:"wire"`
	}{SyncStats: res1, Wire: syncer1.WireStats})
	if syncer1.WireStats.ValuesRecv != 1 {
		panic(fmt.Sprintf("scenario 1 expected exactly 1 value transferred, got %d", syncer1.WireStats.ValuesRecv))
	}
	results["single-leaf"] = map[string]any{"merkle": res1, "wire": syncer1.WireStats, "fullBaseline": baseline1}

	// Re-running sync on already-converged replicas must be a single /root.
	res1b, err := syncapi.NewSyncer(syncapi.DirectLocal{St: local}, syncapi.NewClient(peerURL)).Sync(ctx)
	must(err)
	if !res1b.Converged || res1b.Attempts != 1 || syncer1.WireStats.ValuesRecv != 1 {
		panic("scenario 1 re-sync did not short-circuit")
	}
	fmt.Println("  收敛后再次同步: attempts=1, 只交换 1 个根哈希 ✓")

	// ---------------- scenario 2: full divergence ----------------
	fmt.Println("\n== 场景 2：双方各自演进（更新 / 删除 / 新增）后的全量差异 ==")
	lc2 := syncapi.NewClient(localURL)
	pc2 := syncapi.NewClient(peerURL)
	must(lc2.Reset(ctx))
	must(pc2.Reset(ctx))
	seedCommon(ctx, lc2, *n, 100)
	seedCommon(ctx, pc2, *n, 100)

	// Peer updates 10 keys to v2 and deletes 5 existing keys (tombstones).
	for i := 0; i < 10; i++ {
		_, err := pc2.Put(ctx, fmt.Sprintf("key-%04d", 50+i), "peer-new-value", 2)
		must(err)
	}
	for i := 0; i < 5; i++ {
		_, err := pc2.DeleteKey(ctx, fmt.Sprintf("key-%04d", 10+i), 2)
		must(err)
	}
	// Local updates 10 other keys to v2 and adds 7 brand-new keys.
	for i := 0; i < 10; i++ {
		_, err := lc2.Put(ctx, fmt.Sprintf("key-%04d", 100+i), "local-new-value", 2)
		must(err)
	}
	for i := 0; i < 7; i++ {
		_, err := lc2.Put(ctx, fmt.Sprintf("local-only-%d", i), "born-here", 1)
		must(err)
	}

	baseline2 := naiveFullTransfer(ctx, peerURL)
	syncer2 := syncapi.NewSyncer(syncapi.DirectLocal{St: local}, syncapi.NewClient(peerURL))
	res2, err := syncer2.Sync(ctx)
	must(err)
	if !res2.Converged {
		panic("scenario 2 did not converge")
	}
	// Check tombstones arrived locally.
	for i := 0; i < 5; i++ {
		e, ok := local.Get(fmt.Sprintf("key-%04d", 10+i))
		if !ok || !e.Deleted || e.Version != 2 {
			panic(fmt.Sprintf("scenario 2 tombstone missing: %+v ok=%v", e, ok))
		}
	}
	// Local-only keys must reach the peer.
	for i := 0; i < 7; i++ {
		e, ok := peer.Get(fmt.Sprintf("local-only-%d", i))
		if !ok || e.Deleted {
			panic("scenario 2 local-only key missing on peer")
		}
	}
	printJSON("朴素全量拉取开销", baseline2)
	printJSON("Merkle 双向同步结果", struct {
		*syncapi.SyncStats
		Wire syncapi.Stats `json:"wire"`
	}{SyncStats: res2, Wire: syncer2.WireStats})
	results["full-divergence"] = map[string]any{"merkle": res2, "wire": syncer2.WireStats, "fullBaseline": baseline2}

	// ---------------- scenario 3: update during the scan ----------------
	fmt.Println("\n== 场景 3：扫描期间 peer 被写入（一致性快照冲突 + 重试）==")
	lc3 := syncapi.NewClient(localURL)
	pc3 := syncapi.NewClient(peerURL)
	must(lc3.Reset(ctx))
	must(pc3.Reset(ctx))
	seedCommon(ctx, lc3, *n, 100)
	seedCommon(ctx, pc3, *n, 100)

	// Original divergence: one v2 update on peer.
	_, err = pc3.Put(ctx, "key-0001", "before-scan-value", 2)
	must(err)
	// Scan-time mutation: armed to fire before read #1, i.e. the first
	// POST /nodes (read #0 is GET /root). The second key is a late change
	// that must not be lost when the first round 409s and retries.
	must(pc3.ArmChaos(ctx, 0, "put", "key-0002", "mid-scan-value", 2))

	syncer3 := syncapi.NewSyncer(syncapi.DirectLocal{St: local}, syncapi.NewClient(peerURL))
	start := time.Now()
	res3, err := syncer3.Sync(ctx)
	elapsed := time.Since(start)
	must(err)
	if !res3.Converged {
		panic("scenario 3 did not converge")
	}
	if res3.Conflicts != 1 {
		panic(fmt.Sprintf("scenario 3 expected exactly 1 conflict, got %d", res3.Conflicts))
	}
	e3a, _ := local.Get("key-0001")
	e3b, _ := local.Get("key-0002")
	if e3a.Value != "before-scan-value" || e3b.Value != "mid-scan-value" {
		panic(fmt.Sprintf("scenario 3 lost an update: %q %q", e3a.Value, e3b.Value))
	}
	_ = elapsed
	printJSON("Merkle 同步结果（期望 conflicts=1）", struct {
		*syncapi.SyncStats
		Wire         syncapi.Stats `json:"wire"`
		ElapsedMsecs int64         `json:"elapsedMillis"`
	}{SyncStats: res3, Wire: syncer3.WireStats, ElapsedMsecs: elapsed.Milliseconds()})
	results["scan-time-update"] = map[string]any{"merkle": res3, "wire": syncer3.WireStats}

	// Sanity: the tree math over both stores agrees at the end.
	tl := merkle.Build(local.Snapshot())
	tp := merkle.Build(peer.Snapshot())
	if tl.Root() != tp.Root() {
		panic("final roots differ")
	}
	fmt.Printf("\n全部场景通过，最终树根一致: %s…\n", tl.Root()[:16])

	raw, _ := json.MarshalIndent(results, "", "  ")
	if err := os.WriteFile("demo-results.json", raw, 0o644); err != nil {
		fmt.Fprintf(os.Stderr, "warning: could not write demo-results.json: %v\n", err)
	} else {
		fmt.Println("机器可读结果已写入 demo-results.json")
	}
}
