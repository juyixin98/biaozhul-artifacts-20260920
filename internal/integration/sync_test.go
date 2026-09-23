// Package integration drives the real gRPC stub servers over real TCP
// listeners, the real gRPC client adapter, the synchronizer engine and a
// file-backed SQLite database — the same path as the command-line binaries.
package integration

import (
	"context"
	"net"
	"path/filepath"
	"testing"
	"time"

	"google.golang.org/grpc"

	"nodesync/internal/chain"
	"nodesync/internal/gen"
	"nodesync/internal/grpcnode"
	pb "nodesync/internal/pb"
	"nodesync/internal/sample"
	"nodesync/internal/store"
	"nodesync/internal/stub"
	"nodesync/internal/syncer"
	"nodesync/internal/verify"
)

func startNode(t *testing.T, blocks []chain.Block, cfg stub.Config) string {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := grpc.NewServer()
	pb.RegisterNodeSyncServer(srv, stub.NewServer(cfg, blocks))
	go srv.Serve(lis)
	t.Cleanup(srv.Stop)
	return lis.Addr().String()
}

func buildEnv(t *testing.T, length int) ([]chain.Block, *sample.Sample, []string) {
	t.Helper()
	blocks := gen.Build(gen.Spec{Length: length, Seed: []byte("nodesync-genesis"), BodyNonce: []byte("p006")})
	s := sample.FromBlocks(blocks, []byte("nodesync-genesis"), []byte("p006"), "test")

	alpha := startNode(t, blocks, stub.Config{
		NodeID: "alpha", AdvertisedDelta: 7,
		Faults: []stub.Fault{{Type: stub.FaultTimeout, Start: 9, End: 16}},
	})
	beta := startNode(t, blocks, stub.Config{
		NodeID: "beta", AdvertisedDelta: 0,
		Faults: []stub.Fault{{Type: stub.FaultCorrupt, Start: 4, End: 7}},
	})
	gamma := startNode(t, blocks, stub.Config{
		NodeID: "gamma", AdvertisedDelta: -2,
		Faults: []stub.Fault{{Type: stub.FaultBadParent, Start: 17, End: 17}},
	})
	return blocks, s, []string{alpha, beta, gamma}
}

func dialClients(t *testing.T, addrs []string) ([]syncer.Client, func()) {
	t.Helper()
	ctx := context.Background()
	ids := []string{"alpha", "beta", "gamma"}
	var clients []syncer.Client
	var conns []*grpcnode.Client
	for i, addr := range addrs {
		c, err := grpcnode.Dial(ctx, ids[i], addr)
		if err != nil {
			t.Fatal(err)
		}
		clients = append(clients, c)
		conns = append(conns, c)
	}
	return clients, func() {
		for _, c := range conns {
			_ = c.Close()
		}
	}
}

func TestFullSyncAndRestartOverGRPC(t *testing.T) {
	blocks, samp, addrs := buildEnv(t, 32)
	dbPath := filepath.Join(t.TempDir(), "sync.db")

	ctx := context.Background()
	st, err := store.Open(ctx, dbPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := st.InitializeGenesis(ctx, blocks[0]); err != nil {
		t.Fatal(err)
	}

	cfg := syncer.Config{
		TargetHeight: samp.TipHeight,
		GenesisHash:  blocks[0].Hash,
		SegmentSize:  8,
		MaxParallel:  4,
		MaxAttempts:  6,
		RPCTimeout:   400 * time.Millisecond,
	}

	clients, closeClients := dialClients(t, addrs)
	rep, err := syncer.Run(ctx, st, clients, cfg)
	closeClients()
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if rep.FinalCheckpoint != 32 || !rep.Complete {
		t.Fatalf("checkpoint=%d complete=%v", rep.FinalCheckpoint, rep.Complete)
	}
	// Every fault class must have been observed and worked around.
	if rep.Attempts["alpha"].Timeout == 0 {
		t.Fatal("alpha timeout not observed")
	}
	if rep.Attempts["beta"].BadHash == 0 {
		t.Fatal("beta corruption not observed")
	}
	if rep.Failovers == 0 {
		t.Fatal("no source failover occurred")
	}

	sum, _, match, err := verify.ChainSummary(ctx, st, samp)
	if err != nil || !match {
		t.Fatalf("summary match=%v err=%v explanation=%s", match, err, sum.Explanation)
	}
	st.Close()

	// Restart: reopen the same file DB and resume from the durable checkpoint.
	st2, err := store.Open(ctx, dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer st2.Close()
	cp, _, err := st2.Checkpoint(ctx)
	if err != nil || cp != 32 {
		t.Fatalf("checkpoint after reopen = %d err=%v", cp, err)
	}
	clients2, close2 := dialClients(t, addrs)
	defer close2()
	rep2, err := syncer.Run(ctx, st2, clients2, cfg)
	if err != nil {
		t.Fatalf("restart Run: %v", err)
	}
	if rep2.StartCheckpoint != 32 || rep2.FinalCheckpoint != 32 {
		t.Fatalf("restart checkpoints %d -> %d", rep2.StartCheckpoint, rep2.FinalCheckpoint)
	}
}
