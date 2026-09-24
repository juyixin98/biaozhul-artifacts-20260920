# Durable record of one completed segment.
string goal_id
int32 segment_index
string segment_hash          # PBKDF2-derived digest of this single segment
string chained_hash          # running chained SHA-256 digest after this segment
string input_fingerprint     # SHA-256 of the segment input (raw input not echoed)
float64 duration_ms
bool recovered               # true if the row was found during crash recovery
