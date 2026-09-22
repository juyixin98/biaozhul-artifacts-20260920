// 远端节点 gRPC 客户端及同步器使用的 Peer 抽象。
package harness

import (
	"context"
	"fmt"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"nodesync/internal/chain"
	pb "nodesync/proto/syncpb"
)

// Peer 是同步器眼中的一个远端源。
type Peer struct {
	ID   string
	Addr string

	conn   *grpc.ClientConn
	client pb.BlockSyncClient
}

// Dial 连接桩节点。
func Dial(id, addr string) (*Peer, error) {
	conn, err := grpc.NewClient(addr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, err
	}
	return &Peer{ID: id, Addr: addr, conn: conn, client: pb.NewBlockSyncClient(conn)}, nil
}

// Close 关闭连接。
func (p *Peer) Close() error { return p.conn.Close() }

// AdvertisedTip 返回节点"宣称"的高度与哈希（未经证实，仅作参考）。
func (p *Peer) AdvertisedTip(ctx context.Context) (uint64, []byte, error) {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	ti, err := p.client.ChainTip(ctx, &pb.GetTipRequest{})
	if err != nil {
		return 0, nil, err
	}
	return ti.Height, ti.BlockHash, nil
}

// Fetch 拉取 [start, start+limit)。返回 chain.Block 切片与实际服务节点 ID。
func (p *Peer) Fetch(ctx context.Context, start, limit uint64) ([]*chain.Block, string, error) {
	seg, err := p.client.FetchSegments(ctx, &pb.GetSegmentsRequest{Start: start, Limit: limit})
	if err != nil {
		return nil, p.ID, err
	}
	blocks := make([]*chain.Block, 0, len(seg.Blocks))
	for _, b := range seg.Blocks {
		blocks = append(blocks, &chain.Block{
			Height:     b.Height,
			ParentHash: b.ParentHash,
			Payload:    b.Payload,
			Signature:  b.Signature,
		})
	}
	nodeID := seg.NodeId
	if nodeID == "" {
		nodeID = p.ID
	}
	return blocks, nodeID, nil
}

// Ensure Peer 实现 fmt.Stringer 便于日志。
func (p *Peer) String() string { return fmt.Sprintf("peer(%s@%s)", p.ID, p.Addr) }
