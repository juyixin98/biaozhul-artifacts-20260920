# Immutable configuration snapshot published after every committed revision.
# Callbacks must treat it as read-only.
uint64 version                 # 0 = built-in default (never persisted); monotonically increasing
float64 sample_rate_hz         # sampling rate in Hz
uint32 buffer_length           # ring-buffer length in samples
uint64 allowed_latency_ms      # tolerable latency budget in milliseconds
int64 committed_at_ms          # wall-clock commit time, milliseconds since Unix epoch (0 for v0)
