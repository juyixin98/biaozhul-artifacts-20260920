# Gas benchmark — forge script (deterministic EVM)

| checkpoints (n) | operation | gas |
|---:|---|---:|
| 0 | `append_first` | 90,180 |
| 1 | `append` | 75,467 |
| 2 | `merge_same_block` | 33,390 |
| 200 | `append` | 30,590 |
| 200 | `merge_same_block` | 33,390 |
| 4 | `lookup_latest_cold` | 10,697 |
| 16 | `lookup_latest_cold` | 15,793 |
| 64 | `lookup_latest_cold` | 20,889 |
| 256 | `lookup_latest_cold` | 25,985 |
| 1024 | `lookup_latest_cold` | 31,081 |
