package twopc

import "twopc-sim/internal/wal"

// Protocol message payloads exchanged over the engine network.

// Prepare is phase 1 from coordinator to participant.
type Prepare struct {
	TxnID  string
	Writes []wal.KVWrite
}

// Vote is the participant's phase-1 reply.
type Vote struct {
	TxnID string
	Yes   bool
}

// Commit is the global COMMIT decision.
type Commit struct {
	TxnID  string
	Writes []wal.KVWrite
}

// Abort is the global ABORT decision.
type Abort struct {
	TxnID string
}

// Ack acknowledges COMMIT or ABORT so the coordinator stops retransmitting.
type Ack struct {
	TxnID string
}

// Query is a blocking participant asking the coordinator for a decision.
type Query struct {
	TxnID string
}

// QueryReply answers a Query. Known is false while the coordinator has not
// decided; the participant keeps waiting (it must not abort unilaterally).
type QueryReply struct {
	TxnID  string
	Known  bool
	Commit bool
	Writes []wal.KVWrite
}
