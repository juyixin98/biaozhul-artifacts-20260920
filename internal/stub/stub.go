// Package stub implements the three test nodes. Each node holds the canonical
// chain but can be configured to misbehave on chosen height windows:
//
//   - "timeout"    : never return in time (the client gives up with a deadline)
//   - "corrupt"    : return a block whose body was altered without recomputing
//     its hash (SHA-256 self-hash verification fails)
//   - "badparent"  : return a self-consistent fork whose first block commits to
//     a bogus parent hash. If the window starts at a segment
//     boundary this passes in-segment verification and is only
//     rejected at the trusted anchor; otherwise it is rejected
//     immediately as an internal parent mismatch.
//
// A node may also advertise an inflated tip height while serving the real
// chain, so that synchronizers cannot equate an advertised height with
// verified progress.
package stub

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"nodesync/internal/chain"
	pb "nodesync/internal/pb"
	pbconv "nodesync/internal/pbconv"
)

// FaultType enumerates configured misbehaviors.
type FaultType string

// Supported fault types.
const (
	FaultTimeout   FaultType = "timeout"
	FaultCorrupt   FaultType = "corrupt"
	FaultBadParent FaultType = "badparent"
)

// Fault applies to any request whose [start,end] overlaps [Start,End].
type Fault struct {
	Type  FaultType `json:"type"`
	Start int64     `json:"start"`
	End   int64     `json:"end"`
}

// Config is the on-disk node configuration.
type Config struct {
	NodeID          string `json:"node_id"`
	AdvertisedDelta int64  `json:"advertised_delta"`
	// DelayMs maps a request start height to an artificial response delay.
	DelayMs map[int64]int `json:"delay_ms"`
	Faults  []Fault       `json:"faults"`
}

// LoadConfig reads a node config file.
func LoadConfig(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var c Config
	if err := json.Unmarshal(data, &c); err != nil {
		return nil, fmt.Errorf("stub: parse %s: %w", path, err)
	}
	if c.NodeID == "" {
		return nil, fmt.Errorf("stub: config %s missing node_id", path)
	}
	return &c, nil
}

// Server is a NodeSync gRPC server backed by the canonical chain.
type Server struct {
	pb.UnimplementedNodeSyncServer
	cfg    Config
	blocks []chain.Block
	byH    map[int64]chain.Block
	tip    int64
}

// NewServer builds a stub server over the given canonical chain.
func NewServer(cfg Config, blocks []chain.Block) *Server {
	byH := make(map[int64]chain.Block, len(blocks))
	var tip int64
	for _, b := range blocks {
		byH[b.Height] = b
		tip = b.Height
	}
	return &Server{cfg: cfg, blocks: blocks, byH: byH, tip: tip}
}

// matchingFault returns the first fault whose window overlaps the request.
func (s *Server) matchingFault(start, end int64) *Fault {
	for i := range s.cfg.Faults {
		f := &s.cfg.Faults[i]
		if start <= f.End && end >= f.Start {
			return f
		}
	}
	return nil
}

// Status reports a possibly inflated height while returning the real head hash.
func (s *Server) Status(_ context.Context, _ *pb.StatusRequest) (*pb.StatusResponse, error) {
	head := s.blocks[len(s.blocks)-1]
	return &pb.StatusResponse{
		NodeId:             s.cfg.NodeID,
		AdvertisedHeight:   s.tip + s.cfg.AdvertisedDelta,
		AdvertisedHeadHash: head.Hash,
		GenesisHash:        s.blocks[0].Hash,
	}, nil
}

// GetSegment serves a slice, applying a configured delay or fault.
func (s *Server) GetSegment(ctx context.Context, req *pb.SegmentRequest) (*pb.SegmentResponse, error) {
	start := req.GetStartHeight()
	if start < 0 {
		return nil, status.Errorf(codes.InvalidArgument, "negative start height %d", start)
	}
	if req.GetLimit() <= 0 {
		return nil, status.Errorf(codes.InvalidArgument, "limit must be positive, got %d", req.GetLimit())
	}
	end := start + int64(req.GetLimit()) - 1
	if end > s.tip {
		end = s.tip
	}

	// Artificial latency (used for timeouts and slow-node simulation).
	if d, ok := s.cfg.DelayMs[start]; ok && d > 0 {
		timer := time.NewTimer(time.Duration(d) * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil, status.FromContextError(ctx.Err()).Err()
		case <-timer.C:
		}
	}

	f := s.matchingFault(start, end)
	if f != nil && f.Type == FaultTimeout {
		// Block until the client cancels or deadline expires; never deliver.
		<-ctx.Done()
		return nil, status.FromContextError(ctx.Err()).Err()
	}

	if start > s.tip {
		return &pb.SegmentResponse{Blocks: nil}, nil
	}
	seg := make([]chain.Block, 0, end-start+1)
	for h := start; h <= end; h++ {
		seg = append(seg, s.byH[h].Clone())
	}

	if f != nil {
		switch f.Type {
		case FaultCorrupt:
			applyCorruption(seg, f.Start)
		case FaultBadParent:
			applyBadParent(seg, f.Start)
		}
	}
	return &pb.SegmentResponse{Blocks: pbconv.ToProtoSlice(seg)}, nil
}

// applyCorruption alters a body byte inside the window without updating the
// hash, so recomputing SHA-256 fails.
func applyCorruption(seg []chain.Block, windowStart int64) {
	idx := 0
	for i := range seg {
		if seg[i].Height >= windowStart {
			idx = i
			break
		}
	}
	b := &seg[idx]
	if len(b.Body) == 0 {
		b.Body = []byte{0xFF}
	} else {
		b.Body = append([]byte(nil), b.Body...)
		b.Body[0] ^= 0xFF
	}
	// Hash intentionally left stale.
}

// applyBadParent turns the suffix from windowStart into an internally
// consistent fork anchored at a bogus parent hash. Blocks before windowStart
// remain canonical, so when the window begins mid-segment the fork block no
// longer links to the preceding canonical block (internal parent mismatch).
func applyBadParent(seg []chain.Block, windowStart int64) {
	startIdx := -1
	for i := range seg {
		if seg[i].Height >= windowStart {
			startIdx = i
			break
		}
	}
	if startIdx == -1 {
		startIdx = 0
	}
	for i := startIdx; i < len(seg); i++ {
		b := &seg[i]
		if i == startIdx {
			fake := make([]byte, chain.HashLen)
			for j := range fake {
				fake[j] = 0xEE
			}
			b.ParentHash = fake
		} else {
			b.ParentHash = append([]byte(nil), seg[i-1].Hash...)
		}
		b.Rehash()
	}
}
