package stub

import (
	"context"
	"errors"
	"net"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"

	"nodesync/internal/chain"
	"nodesync/internal/gen"
	pb "nodesync/internal/pb"
	pbconv "nodesync/internal/pbconv"
)

func serve(t *testing.T, cfg Config, blocks []chain.Block) (pb.NodeSyncClient, func()) {
	t.Helper()
	lis := bufconn.Listen(1024 * 1024)
	srv := grpc.NewServer()
	pb.RegisterNodeSyncServer(srv, NewServer(cfg, blocks))
	go srv.Serve(lis)
	dialer := func(context.Context, string) (net.Conn, error) { return lis.Dial() }
	conn, err := grpc.DialContext(context.Background(), "bufnet",
		grpc.WithContextDialer(dialer),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatalf("dial bufconn: %v", err)
	}
	cleanup := func() { conn.Close(); srv.Stop() }
	return pb.NewNodeSyncClient(conn), cleanup
}

func refChain() []chain.Block {
	return gen.Build(gen.Spec{Length: 24, Seed: []byte("s"), BodyNonce: []byte("n")})
}

func TestHonestSegmentAndStatus(t *testing.T) {
	blocks := refChain()
	cli, cleanup := serve(t, Config{NodeID: "h", AdvertisedDelta: 5}, blocks)
	defer cleanup()
	ctx := context.Background()

	st, err := cli.Status(ctx, &pb.StatusRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if st.GetNodeId() != "h" || st.GetAdvertisedHeight() != 29 {
		t.Fatalf("unexpected status: id=%s adv=%d", st.GetNodeId(), st.GetAdvertisedHeight())
	}
	seg, err := cli.GetSegment(ctx, &pb.SegmentRequest{StartHeight: 1, Limit: 8})
	if err != nil {
		t.Fatal(err)
	}
	got := pbconv.FromProtoSlice(seg.GetBlocks())
	if err := chain.VerifyInternal(got); err != nil {
		t.Fatalf("honest segment failed internal verify: %v", err)
	}
	if got[0].Height != 1 || len(got) != 8 {
		t.Fatalf("segment shape wrong: first=%d n=%d", got[0].Height, len(got))
	}
}

func TestTimeoutFault(t *testing.T) {
	blocks := refChain()
	cfg := Config{NodeID: "slow", Faults: []Fault{{Type: FaultTimeout, Start: 9, End: 16}}}
	cli, cleanup := serve(t, cfg, blocks)
	defer cleanup()

	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()
	_, err := cli.GetSegment(ctx, &pb.SegmentRequest{StartHeight: 9, Limit: 8})
	if err == nil {
		t.Fatal("expected deadline error from timeout node")
	}
}

func TestCorruptFault(t *testing.T) {
	blocks := refChain()
	cfg := Config{NodeID: "evil", Faults: []Fault{{Type: FaultCorrupt, Start: 4, End: 4}}}
	cli, cleanup := serve(t, cfg, blocks)
	defer cleanup()

	seg, err := cli.GetSegment(context.Background(), &pb.SegmentRequest{StartHeight: 1, Limit: 8})
	if err != nil {
		t.Fatal(err)
	}
	got := pbconv.FromProtoSlice(seg.GetBlocks())
	if err := chain.VerifyInternal(got); !errors.Is(err, chain.ErrHashMismatch) {
		t.Fatalf("want ErrHashMismatch, got %v", err)
	}
}

func TestBadParentFault(t *testing.T) {
	blocks := refChain()
	// Window begins at the segment boundary (9): segment is internally
	// consistent but anchored at a bogus parent -> internal passes, boundary
	// append against the trusted predecessor fails.
	cfg := Config{NodeID: "fork", Faults: []Fault{{Type: FaultBadParent, Start: 9, End: 9}}}
	cli, cleanup := serve(t, cfg, blocks)
	defer cleanup()

	seg, err := cli.GetSegment(context.Background(), &pb.SegmentRequest{StartHeight: 9, Limit: 8})
	if err != nil {
		t.Fatal(err)
	}
	got := pbconv.FromProtoSlice(seg.GetBlocks())
	if err := chain.VerifyInternal(got); err != nil {
		t.Fatalf("boundary fork should be internally consistent: %v", err)
	}
	if err := chain.VerifyAppend(blocks[8].Hash, 9, got); !errors.Is(err, chain.ErrParentMismatch) {
		t.Fatalf("want ErrParentMismatch at anchor, got %v", err)
	}
}

func TestMidSegmentBadParentIsInternallyRejected(t *testing.T) {
	blocks := refChain()
	cfg := Config{NodeID: "fork2", Faults: []Fault{{Type: FaultBadParent, Start: 4, End: 4}}}
	cli, cleanup := serve(t, cfg, blocks)
	defer cleanup()

	seg, err := cli.GetSegment(context.Background(), &pb.SegmentRequest{StartHeight: 1, Limit: 8})
	if err != nil {
		t.Fatal(err)
	}
	got := pbconv.FromProtoSlice(seg.GetBlocks())
	if err := chain.VerifyInternal(got); !errors.Is(err, chain.ErrParentMismatch) {
		t.Fatalf("want internal ErrParentMismatch for mid-segment fork, got %v", err)
	}
}
