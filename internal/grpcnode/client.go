// Package grpcnode adapts the generated gRPC NodeSync client to the syncer's
// transport interface.
package grpcnode

import (
	"context"
	"fmt"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	"nodesync/internal/chain"
	pb "nodesync/internal/pb"
	pbconv "nodesync/internal/pbconv"
)

// Client is a syncer client backed by a gRPC NodeSync connection.
type Client struct {
	id     string
	conn   *grpc.ClientConn
	client pb.NodeSyncClient
}

// Dial connects to a node. id is the local label used in evidence records.
func Dial(ctx context.Context, id, address string) (*Client, error) {
	dialCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	conn, err := grpc.DialContext(dialCtx, address,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithBlock(),
	)
	if err != nil {
		return nil, fmt.Errorf("dial node %s (%s): %w", id, address, err)
	}
	return &Client{id: id, conn: conn, client: pb.NewNodeSyncClient(conn)}, nil
}

// Close releases the connection.
func (c *Client) Close() error { return c.conn.Close() }

// NodeID returns the configured label.
func (c *Client) NodeID() string { return c.id }

// Status fetches the node's advertised status.
func (c *Client) Status(ctx context.Context) (int64, []byte, []byte, error) {
	resp, err := c.client.Status(ctx, &pb.StatusRequest{})
	if err != nil {
		return 0, nil, nil, err
	}
	return resp.GetAdvertisedHeight(),
		append([]byte(nil), resp.GetAdvertisedHeadHash()...),
		append([]byte(nil), resp.GetGenesisHash()...), nil
}

// GetSegment fetches a segment.
func (c *Client) GetSegment(ctx context.Context, start int64, limit int32) ([]chain.Block, error) {
	resp, err := c.client.GetSegment(ctx, &pb.SegmentRequest{StartHeight: start, Limit: limit})
	if err != nil {
		return nil, err
	}
	return pbconv.FromProtoSlice(resp.GetBlocks()), nil
}
