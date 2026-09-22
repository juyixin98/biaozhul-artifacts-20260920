// Package harness 提供可注入故障的桩节点（真实 gRPC 服务器）与客户端。
//
// 三个桩节点共享同一份"诚实链"夹具，但各自可配置故障行为：
//   - 段中间区块 payload 损坏（哈希承诺 + Ed25519 签名失败）
//   - 段中间区块父哈希错误（用真实私钥重签，密码学自洽但链断裂）
//   - 请求超时（sleep 到客户端 deadline 之后）
//   - 虚高宣称高度（ChainTip 返回不存在的高度）
//   - 短段（返回区块数少于请求数，模拟只持有部分数据的节点）
//   - gRPC 错误（直接拒绝某些区间的请求）
package harness

import (
	"context"
	"net"
	"sync"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"nodesync/internal/chain"
	pb "nodesync/proto/syncpb"
)

// FaultSpec 描述单个桩节点的故障注入。
type FaultSpec struct {
	// CorruptPayloadRanges 中命中的段，把段内"中间区块"的 payload 损坏。
	// 半开区间 [start, start+limit) 与请求区间重叠即命中。
	CorruptPayloadRanges []Range
	// CorruptParentRanges 命中的段，把段内中间区块的父哈希改为错误值（重新签名）。
	CorruptParentRanges []Range
	// TimeoutRanges 命中的段，服务端 sleep Delay（超过客户端 deadline）。
	TimeoutRanges []Range
	TimeoutDelay  time.Duration
	// ErrorRanges 命中的段直接返回 gRPC Internal 错误。
	ErrorRanges []Range
	// AdvertisedTipInflate: 宣称高度 = 诚实链尖 + 该值（虚高）。
	AdvertisedTipInflate uint64
	// AdvertisedTipInvalid: 宣称哈希填全 0。
	AdvertisedTipInvalid bool
	// ShortRanges 命中的段只返回 1 个区块（短段）。
	ShortRanges []Range
}

// Range 是半开高度区间 [Start, End)。
type Range struct{ Start, End uint64 }

func (r Range) overlaps(start, limit uint64) bool {
	end := start + limit
	return start < r.End && r.Start < end
}

func anyOverlap(rs []Range, start, limit uint64) bool {
	for _, r := range rs {
		if r.overlaps(start, limit) {
			return true
		}
	}
	return false
}

// Node 是一个运行中的桩节点。
type Node struct {
	ID          string
	Lis         net.Listener
	Server      *grpc.Server
	fixture     *chain.Fixture
	fault       FaultSpec
	mu          sync.Mutex
	reqCount    int
	inflight    int
	maxInflight int
}

// NewNode 在随机本地端口创建桩节点。
func NewNode(id string, fixture *chain.Fixture, fault FaultSpec) (*Node, error) {
	return NewNodeWithAddr(id, fixture, fault, "127.0.0.1:0")
}

// NewNodeWithAddr 在指定地址创建桩节点（"host:0" 表示随机端口）。
func NewNodeWithAddr(id string, fixture *chain.Fixture, fault FaultSpec, addr string) (*Node, error) {
	lis, err := net.Listen("tcp", addr)
	if err != nil {
		return nil, err
	}
	n := &Node{ID: id, Lis: lis, fixture: fixture, fault: fault}
	srv := grpc.NewServer()
	pb.RegisterBlockSyncServer(srv, &server{n: n})
	n.Server = srv
	return n, nil
}

// Serve 开始服务（阻塞）。
func (n *Node) Serve() { _ = n.Server.Serve(n.Lis) }

// Addr 返回监听地址。
func (n *Node) Addr() string { return n.Lis.Addr().String() }

// Stop 优雅停止。
func (n *Node) Stop() { n.Server.GracefulStop() }

// RequestCount 返回收到的 FetchSegments 请求数（测试统计用）。
func (n *Node) RequestCount() int {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.reqCount
}

// Stats 返回累计请求数与观测到的最大并发在途请求数。
func (n *Node) Stats() (total, maxInflight int) {
	n.mu.Lock()
	defer n.mu.Unlock()
	return n.reqCount, n.maxInflight
}

type server struct {
	pb.UnimplementedBlockSyncServer
	n *Node
}

func (s *server) ChainTip(context.Context, *pb.GetTipRequest) (*pb.TipInfo, error) {
	tip := s.n.fixture.ChainTip + s.n.fault.AdvertisedTipInflate
	var hash []byte
	if s.n.fault.AdvertisedTipInvalid {
		hash = make([]byte, 32)
	} else {
		hash = s.n.fixture.Blocks[s.n.fixture.ChainTip].Hash()
	}
	return &pb.TipInfo{Height: tip, BlockHash: hash, NodeId: s.n.ID}, nil
}

func (s *server) FetchSegments(ctx context.Context, req *pb.GetSegmentsRequest) (*pb.Segment, error) {
	s.n.mu.Lock()
	s.n.reqCount++
	s.n.inflight++
	if s.n.inflight > s.n.maxInflight {
		s.n.maxInflight = s.n.inflight
	}
	s.n.mu.Unlock()
	defer func() {
		s.n.mu.Lock()
		s.n.inflight--
		s.n.mu.Unlock()
	}()

	start, limit := req.Start, req.Limit

	if anyOverlap(s.n.fault.TimeoutRanges, start, limit) {
		delay := s.n.fault.TimeoutDelay
		if delay == 0 {
			delay = 5 * time.Second
		}
		select {
		case <-time.After(delay):
		case <-ctx.Done():
			// 客户端先放弃；如实返回取消错误，不产生任何数据。
			return nil, status.FromContextError(ctx.Err()).Err()
		}
	}
	if anyOverlap(s.n.fault.ErrorRanges, start, limit) {
		return nil, status.Errorf(codes.Internal, "node %s 注入故障: 拒绝区间 [%d,%d)", s.n.ID, start, start+limit)
	}

	end := start + limit
	if end > s.n.fixture.ChainTip+1 {
		end = s.n.fixture.ChainTip + 1
	}
	if start > s.n.fixture.ChainTip {
		// 节点并不持有它"宣称"的那些高度：虚高部分如实返回 NotFound。
		return nil, status.Errorf(codes.NotFound, "node %s 实际只到高度 %d", s.n.ID, s.n.fixture.ChainTip)
	}

	blocks := s.n.fixture.Blocks[start:end]
	if anyOverlap(s.n.fault.ShortRanges, start, limit) && len(blocks) > 1 {
		blocks = blocks[:1]
	}

	out := make([]*pb.Block, 0, len(blocks))
	for _, b := range blocks {
		out = append(out, toPB(b))
	}

	// 段内"中间区块"损坏（从不损坏段首，以暴露乱序缓存与连续前缀校验）。
	if len(out) >= 3 {
		mid := len(out) / 2
		midH := start + uint64(mid)
		if anyOverlap(s.n.fault.CorruptPayloadRanges, start, limit) {
			out[mid] = toPB(chain.CorruptPayload(s.n.fixture.Blocks[midH]))
		} else if anyOverlap(s.n.fault.CorruptParentRanges, start, limit) {
			out[mid] = toPB(chain.CorruptParent(s.n.fixture.Blocks[midH]))
		}
	}

	return &pb.Segment{Start: start, Blocks: out, NodeId: s.n.ID}, nil
}

func toPB(b *chain.Block) *pb.Block {
	return &pb.Block{
		Height:      b.Height,
		ParentHash:  append([]byte(nil), b.ParentHash...),
		PayloadHash: b.PayloadHash(),
		Payload:     append([]byte(nil), b.Payload...),
		Signature:   append([]byte(nil), b.Signature...),
	}
}
