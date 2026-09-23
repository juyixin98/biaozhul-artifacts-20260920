// Command syncer runs the interval synchronizer against the configured stub
// nodes, persists the verified prefix to SQLite, records provenance evidence,
// and prints a final chain summary compared against the trusted sample.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"sort"
	"strings"
	"syscall"
	"time"

	"nodesync/internal/grpcnode"
	"nodesync/internal/sample"
	"nodesync/internal/store"
	"nodesync/internal/syncer"
	"nodesync/internal/verify"
)

type nodeAddr struct {
	id   string
	addr string
}

func main() {
	var nodesFlag multiFlag
	samplePath := flag.String("sample", "testdata/trusted_sample.json", "trusted sample chain")
	dbPath := flag.String("db", "data/sync.db", "SQLite database path (prefix file:/memory? use a path)")
	segmentSize := flag.Int("segment", 8, "blocks per segment request")
	maxParallel := flag.Int("parallel", 4, "maximum concurrent segment fetches")
	maxAttempts := flag.Int("attempts", 6, "max attempts per segment")
	rpcTimeout := flag.Duration("timeout", 1500*time.Millisecond, "per-request timeout")
	evidenceOut := flag.String("evidence", "data/evidence.json", "write evidence log here (empty disables)")
	summaryOut := flag.String("summary", "data/summary.json", "write summary JSON here (empty disables)")
	flag.Var(&nodesFlag, "node", "node id=address (repeatable, at least one)")
	flag.Parse()

	if len(nodesFlag) == 0 {
		nodesFlag = multiFlag{
			"alpha=127.0.0.1:50061",
			"beta=127.0.0.1:50062",
			"gamma=127.0.0.1:50063",
		}
	}
	nodes, err := parseNodes(nodesFlag)
	if err != nil {
		fatal(err)
	}

	s, err := sample.Load(*samplePath)
	if err != nil {
		fatal(err)
	}
	blocks, err := s.ToBlocks()
	if err != nil {
		fatal(err)
	}
	genesisHash, err := s.GenesisHashBytes()
	if err != nil {
		fatal(err)
	}

	if dir := dirOf(*dbPath); dir != "" {
		_ = os.MkdirAll(dir, 0o755)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	st, err := store.Open(ctx, *dbPath)
	if err != nil {
		fatal(err)
	}
	defer st.Close()

	// On first run, anchor on the trusted genesis and checkpoint 0.
	init, err := st.Initialized(ctx)
	if err != nil {
		fatal(err)
	}
	if !init {
		if err := st.InitializeGenesis(ctx, blocks[0]); err != nil {
			fatal(err)
		}
	}

	clients := make([]syncer.Client, 0, len(nodes))
	conns := make([]*grpcnode.Client, 0, len(nodes))
	for _, n := range nodes {
		c, err := grpcnode.Dial(ctx, n.id, n.addr)
		if err != nil {
			fatal(err)
		}
		clients = append(clients, c)
		conns = append(conns, c)
	}
	defer func() {
		for _, c := range conns {
			_ = c.Close()
		}
	}()

	cfg := syncer.Config{
		TargetHeight: s.TipHeight,
		GenesisHash:  genesisHash,
		SegmentSize:  int32(*segmentSize),
		MaxParallel:  *maxParallel,
		MaxAttempts:  *maxAttempts,
		RPCTimeout:   *rpcTimeout,
	}

	fmt.Printf("syncer starting: trusted target=%d segment=%d parallel=%d\n",
		cfg.TargetHeight, cfg.SegmentSize, cfg.MaxParallel)
	rep, runErr := syncer.Run(ctx, st, clients, cfg)
	printReport(rep)

	sum, _, match, verr := verify.ChainSummary(context.Background(), st, s)
	if verr != nil {
		fatal(verr)
	}
	printSummary(sum)

	if *evidenceOut != "" {
		if err := writeEvidence(context.Background(), st, *evidenceOut); err != nil {
			fmt.Fprintln(os.Stderr, "evidence write:", err)
		} else {
			fmt.Println("evidence written:", *evidenceOut)
		}
	}
	if *summaryOut != "" {
		if err := writeJSON(*summaryOut, sum); err != nil {
			fmt.Fprintln(os.Stderr, "summary write:", err)
		} else {
			fmt.Println("summary written:", *summaryOut)
		}
	}

	switch {
	case runErr != nil:
		fmt.Println("RESULT: INCOMPLETE —", runErr)
		os.Exit(1)
	case !match:
		fmt.Println("RESULT: MISMATCH — final chain does not match trusted sample:", sum.Explanation)
		os.Exit(1)
	default:
		fmt.Println("RESULT: OK — published chain matches trusted sample")
	}
}

func printReport(r *syncer.Report) {
	if r == nil {
		return
	}
	fmt.Printf("checkpoint %d -> %d (target %d), failovers=%d\n",
		r.StartCheckpoint, r.FinalCheckpoint, r.TargetHeight, r.Failovers)
	ids := make([]string, 0, len(r.Attempts))
	for id := range r.Attempts {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		a := r.Attempts[id]
		adv := r.AdvertisedHeights[id]
		fmt.Printf("  node %-6s advertised=%-4d ok=%d timeout=%d badhash=%d badparent=%d rpcerr=%d\n",
			id, adv, a.OK, a.Timeout, a.BadHash, a.BadParent, a.RPCError)
	}
}

func printSummary(sm verify.Summary) {
	fmt.Println("final chain summary:")
	fmt.Println("  published heights:", sm.PublishedHeights)
	fmt.Println("  height range     :", sm.FirstHeight, "..", sm.LastHeight)
	fmt.Println("  genesis hash     :", sm.GenesisHash)
	fmt.Println("  tip hash         :", sm.TipHash)
	fmt.Println("  chain digest     :", sm.Digest)
	fmt.Println("  matches sample   :", sm.MatchesSample)
	if sm.Explanation != "" {
		fmt.Println("  note             :", sm.Explanation)
	}
}

type evidenceJSON struct {
	ID           int64  `json:"id"`
	AtUnixMs     int64  `json:"at_unix_ms"`
	NodeID       string `json:"node_id"`
	Start        int64  `json:"start_height"`
	End          int64  `json:"end_height"`
	Attempt      int    `json:"attempt"`
	Outcome      string `json:"outcome"`
	Detail       string `json:"detail"`
	RemoteHeight int64  `json:"remote_advertised_height"`
}

func writeEvidence(ctx context.Context, st *store.Store, path string) error {
	rows, err := st.Evidence(ctx)
	if err != nil {
		return err
	}
	out := make([]evidenceJSON, 0, len(rows))
	for _, r := range rows {
		out = append(out, evidenceJSON{
			ID: r.ID, AtUnixMs: r.AtUnixMs, NodeID: r.NodeID,
			Start: r.Start, End: r.End, Attempt: r.Attempt,
			Outcome: r.Outcome, Detail: r.Detail, RemoteHeight: r.RemoteHeig,
		})
	}
	return writeJSON(path, out)
}

func writeJSON(path string, v any) error {
	if dir := dirOf(path); dir != "" {
		_ = os.MkdirAll(dir, 0o755)
	}
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(data, '\n'), 0o644)
}

type multiFlag []string

func (m *multiFlag) String() string     { return strings.Join(*m, ",") }
func (m *multiFlag) Set(v string) error { *m = append(*m, v); return nil }

func parseNodes(flags []string) ([]nodeAddr, error) {
	out := make([]nodeAddr, 0, len(flags))
	seen := map[string]bool{}
	for _, f := range flags {
		id, addr, ok := strings.Cut(f, "=")
		if !ok || id == "" || addr == "" {
			return nil, fmt.Errorf("bad --node %q, want id=address", f)
		}
		if seen[id] {
			return nil, fmt.Errorf("duplicate node id %q", id)
		}
		seen[id] = true
		out = append(out, nodeAddr{id: id, addr: addr})
	}
	return out, nil
}

func dirOf(p string) string {
	for i := len(p) - 1; i >= 0; i-- {
		if p[i] == '/' || p[i] == os.PathSeparator {
			return p[:i]
		}
	}
	return ""
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "syncer:", err)
	os.Exit(1)
}
