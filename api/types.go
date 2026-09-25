// Package api defines the JSON protocol types shared by the cache server
// and its clients.
package api

// ErrorResponse is returned for all non-2xx responses.
type ErrorResponse struct {
	Error string `json:"error"`
}

// PutResponse is returned after a successful (or deduplicated) upload.
type PutResponse struct {
	Digest string `json:"digest"`
	Size   int64  `json:"size"`
	Dedup  bool   `json:"dedup"` // true when the object already existed
}

// BuildRequest describes one explicitly user-supplied fixture command.
// The service never invents or loads commands itself: it only executes
// exactly the argv supplied in the request.
type BuildRequest struct {
	Argv      []string          `json:"argv"`                 // exact command to execute (no shell expansion unless the argv asks for it)
	Inputs    map[string]string `json:"inputs,omitempty"`     // relative workdir path -> CAS digest
	Outputs   []string          `json:"outputs,omitempty"`    // relative paths to collect into the cache after the run
	TimeoutMs int               `json:"timeout_ms,omitempty"` // per-command timeout; 0 uses the server default
}

// BuildResult is the cached outcome of a BuildRequest.
type BuildResult struct {
	ActionDigest string            `json:"action_digest"`
	CacheHit     bool              `json:"cache_hit"`
	ExitCode     int               `json:"exit_code"`
	TimedOut     bool              `json:"timed_out,omitempty"`
	Stdout       string            `json:"stdout"`
	Stderr       string            `json:"stderr"`
	StdoutTrunc  bool              `json:"stdout_truncated,omitempty"`
	StderrTrunc  bool              `json:"stderr_truncated,omitempty"`
	Outputs      map[string]string `json:"outputs,omitempty"` // relative path -> CAS digest
	DurationMs   int64             `json:"duration_ms"`
}

// StatsResponse reports cache occupancy.
type StatsResponse struct {
	Objects       int64 `json:"objects"`
	Bytes         int64 `json:"bytes"`
	ACEntries     int64 `json:"ac_entries"`
	MaxObjectSize int64 `json:"max_object_size"`
}

// FsckReport is the result of a cache consistency scan. It is a
// point-in-time snapshot: objects being uploaded right now legitimately
// appear under leftover_tmp.
type FsckReport struct {
	ObjectsChecked int64    `json:"objects_checked"`
	BytesChecked   int64    `json:"bytes_checked"`
	Corrupt        []string `json:"corrupt"`      // objects whose content does not match their address
	LeftoverTmp    []string `json:"leftover_tmp"` // unpublished temporary uploads
	Unknown        []string `json:"unknown"`      // entries that do not belong to the cache layout
	OK             bool     `json:"ok"`
}
