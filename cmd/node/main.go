// Command node runs one stub test node over gRPC. It serves the canonical
// chain from the trusted sample but applies the misbehaviors declared in its
// JSON config (timeouts, corrupted bodies, bad-parent forks, inflated height).
package main

import (
	"context"
	"flag"
	"fmt"
	"net"
	"os"
	"os/signal"
	"syscall"

	"google.golang.org/grpc"

	pb "nodesync/internal/pb"
	"nodesync/internal/sample"
	"nodesync/internal/stub"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:50061", "listen address")
	samplePath := flag.String("sample", "testdata/trusted_sample.json", "trusted sample chain")
	configPath := flag.String("config", "", "stub node behavior config (required)")
	flag.Parse()

	if *configPath == "" {
		fmt.Fprintln(os.Stderr, "node: --config is required")
		os.Exit(2)
	}

	s, err := sample.Load(*samplePath)
	if err != nil {
		fatal(err)
	}
	blocks, err := s.ToBlocks()
	if err != nil {
		fatal(err)
	}
	cfg, err := stub.LoadConfig(*configPath)
	if err != nil {
		fatal(err)
	}

	lis, err := net.Listen("tcp", *addr)
	if err != nil {
		fatal(fmt.Errorf("listen %s: %w", *addr, err))
	}
	gs := grpc.NewServer()
	pb.RegisterNodeSyncServer(gs, stub.NewServer(*cfg, blocks))

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	go func() {
		<-ctx.Done()
		gs.GracefulStop()
	}()

	fmt.Printf("stub node %q listening on %s (tip %d, advertised %d)\n",
		cfg.NodeID, *addr, s.TipHeight, s.TipHeight+cfg.AdvertisedDelta)
	if err := gs.Serve(lis); err != nil {
		fatal(err)
	}
}

func fatal(err error) {
	fmt.Fprintln(os.Stderr, "node:", err)
	os.Exit(1)
}
