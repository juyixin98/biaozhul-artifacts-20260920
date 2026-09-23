package pb

import (
	"nodesync/internal/chain"
	pb "nodesync/internal/pb"
)

// FromProto converts a wire block to the domain type.
func FromProto(b *pb.Block) chain.Block {
	return chain.Block{
		Height:     b.GetHeight(),
		ParentHash: append([]byte(nil), b.GetParentHash()...),
		Hash:       append([]byte(nil), b.GetHash()...),
		Timestamp:  b.GetTimestamp(),
		Body:       append([]byte(nil), b.GetBody()...),
	}
}

// ToProto converts a domain block to the wire type.
func ToProto(b chain.Block) *pb.Block {
	return &pb.Block{
		Height:     b.Height,
		ParentHash: append([]byte(nil), b.ParentHash...),
		Hash:       append([]byte(nil), b.Hash...),
		Timestamp:  b.Timestamp,
		Body:       append([]byte(nil), b.Body...),
	}
}

// FromProtoSlice converts a slice of wire blocks.
func FromProtoSlice(in []*pb.Block) []chain.Block {
	out := make([]chain.Block, len(in))
	for i := range in {
		out[i] = FromProto(in[i])
	}
	return out
}

// ToProtoSlice converts a slice of domain blocks.
func ToProtoSlice(in []chain.Block) []*pb.Block {
	out := make([]*pb.Block, len(in))
	for i := range in {
		b := in[i]
		out[i] = ToProto(b)
	}
	return out
}
