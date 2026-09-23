package sim

// Config is the JSON run interface for the simulator.
//
// A run is fully described by this structure: the initial weighted topology,
// the key universe, the deterministic client workload, the lossy network
// parameters and a timed schedule of topology operations (scale out, scale
// in, simulated network interruptions).
type Config struct {
	Seed          int64   `json:"seed"`
	StopTick      int     `json:"stopTick"`
	VNodes        int     `json:"vnodesPerWeight"`
	LossRate      float64 `json:"lossRate"`
	DuplicateRate float64 `json:"duplicateRate"`
	ReorderRate   float64 `json:"reorderRate"`
	// Min/MaxLinkDelay bound the random one-way delivery delay in ticks.
	MinLinkDelay int `json:"minLinkDelay"`
	MaxLinkDelay int `json:"maxLinkDelay"`
	// RetryTicks: client outstanding request / migration control timeout.
	RetryTicks int `json:"retryTicks"`
	// MigrationConcurrency: max keys migrating in parallel from one node.
	MigrationConcurrency int `json:"migrationConcurrency"`
	// BarrierTicks: delay between BEGIN for a key and its switch barrier.
	BarrierTicks int `json:"barrierTicks"`
	// DrainTicks: quiet period after the last scheduled op before stopTick, used
	// if StopTick is 0.
	DrainTicks int `json:"drainTicks"`

	InitialNodes []NodeCfg `json:"initialNodes"`
	Keys         []string  `json:"keys"`
	// Preload, when true, writes one seed record ("seed:<key>") to every key
	// on its ring-0 owner at tick 0. It gives every migration real snapshot
	// data to move and makes the transferred-volume statistics meaningful.
	Preload    bool        `json:"preload"`
	Clients    []ClientCfg `json:"clients"`
	Operations []OpCfg     `json:"operations"`
}

// NodeCfg configures one initial physical node.
type NodeCfg struct {
	ID     string `json:"id"`
	Weight int    `json:"weight"`
}

// ClientCfg configures one client actor and the write stream it generates.
type ClientCfg struct {
	ID string `json:"id"`
	// Writes is the total number of writes this client issues.
	Writes int `json:"writes"`
	// StartTick / EndTick bound the uniform spread of issue times.
	StartTick int `json:"startTick"`
	EndTick   int `json:"endTick"`
	// ReadEvery: issue a read every Nth operation slot (0 disables reads).
	ReadEvery int `json:"readEvery"`
}

// OpCfg is one timed topology / failure operation.
//
// Kind:
//   - "scaleOut": Add lists nodes joining the ring.
//   - "scaleIn":  Remove lists nodes leaving; the simulator refuses to remove
//     a node until every key it owned has crossed the barrier.
//   - "interrupt": establish a directed network blackhole between all
//     migration (node-to-node) traffic during [Tick, EndTick).
//     Client/control traffic is unaffected, so the migration
//     protocol's retransmission can be observed recovering.
type OpCfg struct {
	Kind    string    `json:"kind"`
	Tick    int       `json:"tick"`
	EndTick int       `json:"endTick"` // interrupt only
	Add     []NodeCfg `json:"add"`     // scaleOut
	Remove  []string  `json:"remove"`  // scaleIn
}

// Report is the JSON result of a run.
type Report struct {
	Seed           int64             `json:"seed"`
	StopTick       int               `json:"stopTick"`
	FinalRing      []NodeView        `json:"finalRing"`
	RemovedNodes   []string          `json:"removedNodes"`
	Interruption   *InterruptionView `json:"interruption,omitempty"`
	TopologyEpochs []EpochView       `json:"topologyEpochs"`
	Stats          Stats             `json:"stats"`
	// MigrationBytes is the number of key-record payload bytes applied at a
	// destination node as part of migration (snapshot/forward stream), summed
	// over every migration. Each migrated version is counted once.
	Migration     MigrationReport `json:"migration"`
	Verifications Verifications   `json:"verifications"`
}

// NodeView is a node's weight and the keys it stores at end of run.
type NodeView struct {
	ID     string   `json:"id"`
	Weight int      `json:"weight"`
	Keys   []string `json:"keys"`
}

// EpochView records one ring epoch and when it became authoritative.
type EpochView struct {
	Version  int      `json:"version"`
	Nodes    []string `json:"nodes"`
	BeganAt  int      `json:"beganAt"`
	CommitAt int      `json:"commitAt"`
}

// InterruptionView records a simulated blackhole window.
type InterruptionView struct {
	StartTick int `json:"startTick"`
	EndTick   int `json:"endTick"`
}

// Stats holds network and workload counters.
type Stats struct {
	MessagesSent       int `json:"messagesSent"`
	MessagesDelivered  int `json:"messagesDelivered"`
	MessagesDropped    int `json:"messagesDropped"`
	MessagesDuplicated int `json:"messagesDuplicated"`
	MessagesReordered  int `json:"messagesReordered"`
	BytesDelivered     int `json:"bytesDelivered"`
	WritesIssued       int `json:"writesIssued"`
	WritesConfirmed    int `json:"writesConfirmed"`
	WritesFailed       int `json:"writesFailed"` // exhausted retries
	ReadsIssued        int `json:"readsIssued"`
	ReadsSucceeded     int `json:"readsSucceeded"`
	ReadsFailed        int `json:"readsFailed"`
}

// MigrationReport quantifies the migration.
type MigrationReport struct {
	// KeysMoved is the number of keys whose owner changed across COMMITTED
	// epochs. A migration whose barriers passed but whose epoch never committed
	// (an interruption) is not counted here.
	KeysMoved int `json:"keysMoved"`
	// KeysSwitched counts keys whose switch barrier completed, including keys
	// whose epoch never committed. KeysMoved <= KeysSwitched.
	KeysSwitched int `json:"keysSwitched"`
	// EpochsCommitted is the number of topology epochs that reached commit.
	EpochsCommitted int `json:"epochsCommitted"`
	// RecordVersionsTransferred counts every record version applied at the new
	// owner through snapshot/forward streams across committed migrations.
	RecordVersionsTransferred int `json:"recordVersionsTransferred"`
	// BytesTransferred sums payload sizes of the transferred record versions.
	BytesTransferred int `json:"bytesTransferred"`
	// BytesMoved counts each migrated key's final record payload once: the
	// minimum data movement the migration cannot avoid.
	BytesMoved int `json:"bytesMoved"`
	// OverheadBytes is BytesTransferred - BytesMoved (writes during migration
	// that had to be forwarded too).
	OverheadBytes int `json:"overheadBytes"`
	// BarriersPassed counts switch barriers completed.
	BarriersPassed int `json:"barriersPassed"`
	// Movements details each key migration, including uncommitted ones.
	Movements []MovementView `json:"movements"`
}

// MovementView describes one key's migration between two epochs.
type MovementView struct {
	Key        string `json:"key"`
	From       string `json:"from"`
	To         string `json:"to"`
	Version    int    `json:"ringVersion"`
	Committed  bool   `json:"committed"`
	BeganAt    int    `json:"beganAt"`
	SwitchedAt int    `json:"switchedAt"`
}

// Verifications holds end-of-run correctness checks.
type Verifications struct {
	// Ownership: every key lives on its owner in the final ring, and only
	// there for a replicated-factor-1 store.
	Ownership CheckResult `json:"ownership"`
	// Version: every stored record is the latest applied version with the
	// correct single-winner value.
	Version CheckResult `json:"version"`
	// ConfirmedWritesSurvive: every write that received an ack is present at
	// its key's final owner with its committed (or newer) version.
	ConfirmedWritesSurvive CheckResult `json:"confirmedWritesSurvive"`
	// RemovalSafety: no key's latest record exists solely on a removed node;
	// the decommission gate itself also enforces this before a node is dropped.
	RemovalSafety CheckResult `json:"removalSafety"`
	// NoStaleReadAccepted: any read the simulator could validate returned a
	// version >= the newest committed version at request time (reads racing a
	// not-yet-delivered newer write are tolerated, never an older committed one).
	NoStaleReadAccepted CheckResult `json:"noStaleReadAccepted"`
	// Determinism is populated only by tests re-running the same config.
	Determinism *CheckResult `json:"determinism,omitempty"`
}

// CheckResult is one verification outcome.
type CheckResult struct {
	Name    string `json:"name"`
	Pass    bool   `json:"pass"`
	Details string `json:"details,omitempty"`
	// Offenses lists at most the first few violations to keep reports compact.
	Offenses []string `json:"offenses,omitempty"`
}
