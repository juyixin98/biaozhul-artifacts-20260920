# Durable summary of one goal, returned by ListGoals.
string goal_id
uint8 state                  # canonical goal state (see GetGoal.srv constants)
int32 completed_segments
int32 total_segments
uint8 recovery_policy
string fingerprint           # SHA-256 over the canonical goal parameters
string result_hash
string error_code
