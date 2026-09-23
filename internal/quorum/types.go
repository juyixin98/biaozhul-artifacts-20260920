// Package quorum implements validation and exhaustive analysis of weighted
// read/write quorum configurations over failure domains.
//
// The analysis is exact for small configurations: every possible quorum node
// set is enumerated as a bit mask (up to 20 nodes), and read/write as well as
// write/write intersection safety is decided by exhaustive search over
// survivor sets after whole failure-domain outages.
package quorum

// Node is a single voting member. Weight must be non-negative. Domain groups
// nodes that fail together (rack, AZ, ...).
type Node struct {
	ID     string `json:"id"`
	Weight int    `json:"weight"`
	Domain string `json:"domain"`
}

// Config is a weighted quorum configuration. ReadQuorum / WriteQuorum are
// integer weight thresholds; a quorum is any node subset whose total weight
// reaches the threshold.
type Config struct {
	Nodes           []Node `json:"nodes"`
	ReadQuorum      int    `json:"read_quorum"`
	WriteQuorum     int    `json:"write_quorum"`
	TolerateDomains int    `json:"tolerate_domains,omitempty"`
}

// Warning describes a non-fatal configuration issue.
type Warning struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

// Counterexample is a pair of quorums (within one failure scenario) that do
// not intersect. Kind is "ww" (two write quorums) or "rw" (read and a write
// quorum).
type Counterexample struct {
	Kind    string   `json:"kind"`
	QuorumA []string `json:"quorum_a"`
	WeightA int      `json:"weight_a"`
	QuorumB []string `json:"quorum_b"`
	WeightB int      `json:"weight_b"`
	// FailedDomains is the failure scenario in which the pair is reachable
	// (empty for the no-failure scenario).
	FailedDomains []string `json:"failed_domains,omitempty"`
	Explanation   string   `json:"explanation"`

	// Witness is attached by callers (the simulator) and is never populated
	// by this package.
	Witness any `json:"witness,omitempty"`
}

// Scenario describes one concrete set of failed domains: what survives and
// whether quorums remain possible and mutually intersecting.
type Scenario struct {
	FailedDomains   []string        `json:"failed_domains"`
	Survivors       []string        `json:"survivors"`
	SurvivorWeight  int             `json:"survivor_weight"`
	ReadPossible    bool            `json:"read_possible"`
	WritePossible   bool            `json:"write_possible"`
	NumReadQuorums  int             `json:"num_read_quorums"`
	NumWriteQuorums int             `json:"num_write_quorums"`
	ReadQuorums     [][]string      `json:"read_quorums,omitempty"`
	WriteQuorums    [][]string      `json:"write_quorums,omitempty"`
	WWSafe          bool            `json:"ww_safe"`
	RWSafe          bool            `json:"rw_safe"`
	Counterexample  *Counterexample `json:"counterexample,omitempty"`
	// BeyondTolerance marks scenarios with more failed domains than the
	// configured tolerance (e.g. the all-domains-down scenario); they are
	// reported for reference and do not affect the availability verdict.
	BeyondTolerance bool `json:"beyond_tolerance,omitempty"`
}

// Report is the full result of validating and analysing a configuration.
type Report struct {
	Valid  bool     `json:"valid"`
	Errors []string `json:"errors,omitempty"`

	Warnings []Warning `json:"warnings,omitempty"`

	Nodes           []string `json:"nodes"`
	Domains         []string `json:"domains"`
	NumNodes        int      `json:"num_nodes"`
	NumDomains      int      `json:"num_domains"`
	TotalWeight     int      `json:"total_weight"`
	ReadQuorum      int      `json:"read_quorum"`
	WriteQuorum     int      `json:"write_quorum"`
	Exhaustive      bool     `json:"exhaustive"`
	ExhaustiveLimit int      `json:"exhaustive_limit"`

	// Quorum sets of the no-failure scenario (capped listings; counts are
	// exact even when the listing is capped).
	NumReadQuorums  int        `json:"num_read_quorums"`
	NumWriteQuorums int        `json:"num_write_quorums"`
	ReadQuorums     [][]string `json:"read_quorums,omitempty"`
	WriteQuorums    [][]string `json:"write_quorums,omitempty"`

	// Aggregate safety over every enumerated failure scenario.
	WWSafe bool `json:"ww_safe"`
	RWSafe bool `json:"rw_safe"`

	// Minimal counterexamples (fewest participating nodes, then least
	// weight, then lexicographic).
	MinimalWWCounterexample *Counterexample `json:"minimal_ww_counterexample,omitempty"`
	MinimalRWCounterexample *Counterexample `json:"minimal_rw_counterexample,omitempty"`
	MinimalCounterexample   *Counterexample `json:"minimal_counterexample,omitempty"`

	// Availability under up to TolerateDomains whole-domain outages.
	TolerateDomains            int      `json:"tolerate_domains"`
	NumScenarios               int      `json:"num_scenarios"`
	ScenarioListTruncated      bool     `json:"scenario_list_truncated,omitempty"`
	Available                  bool     `json:"available"`
	MinimalAvailabilityFailure []string `json:"minimal_availability_failure,omitempty"`
	AvailabilityWitness        any      `json:"availability_witness,omitempty"`

	// Enumerated scenarios (failing scenarios always; others capped).
	Scenarios []Scenario `json:"scenarios,omitempty"`
}

// MaxNodes is the largest configuration analysed by exhaustive mask
// enumeration. 2^20 masks keeps memory/time bounded (~1 MiB per uint8 array).
const MaxNodes = 20

// Listing caps.
const (
	maxListedSets       = 64
	maxListedScenarios  = 64
	maxHealthyScenarios = 30
	maxScenarios        = 200000
)
