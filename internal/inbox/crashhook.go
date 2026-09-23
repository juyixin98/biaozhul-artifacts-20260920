package inbox

import (
	"os"
	"strconv"
	"strings"
)

// EnvCrashAt is the env var used to force a real process crash.
//
// XIMBOX_CRASH_AT is parsed as "source_chain=<c>,channel=<ch>,sequence=<n>".
// When set, the server exits(99) after the SQL effects of executing that exact
// message but BEFORE its transaction commits. It exists purely for the
// crash/restart tests.
const EnvCrashAt = "XIMBOX_CRASH_AT"

// envCrashHook is the production-process implementation of CrashHook; it is
// inert unless XIMBOX_CRASH_AT identifies the next message to execute.
type envCrashHook struct {
	chain   string
	channel string
	seq     int64
	armed   bool
}

// NewEnvCrashHook parses XIMBOX_CRASH_AT. Returns nil when unset/empty or
// malformed (a malformed test hook must never crash unrelated messages).
func NewEnvCrashHook() CrashHook {
	raw := strings.TrimSpace(os.Getenv(EnvCrashAt))
	if raw == "" {
		return nil
	}
	fields := map[string]string{}
	for _, part := range strings.Split(raw, ",") {
		kv := strings.SplitN(part, "=", 2)
		if len(kv) != 2 {
			return nil
		}
		fields[strings.TrimSpace(kv[0])] = strings.TrimSpace(kv[1])
	}
	chain, ok1 := fields["source_chain"]
	channel, ok2 := fields["channel"]
	seqStr, ok3 := fields["sequence"]
	if !ok1 || !ok2 || !ok3 || chain == "" || channel == "" {
		return nil
	}
	seq, err := strconv.ParseInt(seqStr, 10, 64)
	if err != nil || seq < 0 {
		return nil
	}
	return &envCrashHook{chain: chain, channel: channel, seq: seq, armed: true}
}

// BeforeCommit crashes (exit 99) on the configured message, once.
func (h *envCrashHook) BeforeCommit(chain, channel string, sequence int64) (*int, error) {
	if !h.armed {
		return nil, nil
	}
	if h.chain == chain && h.channel == channel && h.seq == sequence {
		h.armed = false
		code := 99
		return &code, nil
	}
	return nil, nil
}
