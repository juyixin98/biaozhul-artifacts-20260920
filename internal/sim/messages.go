package sim

// All network messages are plain structs exchanged between the router
// (coordinator) and storage nodes. Gen is the sender's retry generation; a
// reply carrying a stale generation is ignored by the sender.

type msgClientPut struct {
	ReqID   uint64
	Gen     int
	Key     string
	Value   string
	Version uint64
}

type msgPutAck struct {
	ReqID  uint64
	Gen    int
	NodeID string
}

type msgClientGet struct {
	ReqID uint64
	Gen   int
	Key   string
}

type msgGetResp struct {
	ReqID  uint64
	Gen    int
	NodeID string
	Rec    Record
	Found  bool
}

type msgFetch struct {
	TaskID string
	Gen    int
	Cursor int
	Limit  int
}

type msgBatch struct {
	TaskID     string
	Gen        int
	Records    []kv
	NextCursor int
	Done       bool
}

type msgTransferPut struct {
	TaskID  string
	Gen     int
	Records []kv
}

type msgTransferAck struct {
	TaskID string
	Gen    int
	Count  int
}

type msgDecommission struct{}

type msgDecommissionAck struct {
	NodeID string
}

// message is the tagged envelope handed to the network.
type message struct{ v any }

const routerAddr = "router"
