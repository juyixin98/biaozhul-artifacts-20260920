# Sample request frames

Each file is one raw wire frame (24-byte header + payload). Send it verbatim over TCP; request_id differs per file.

| file | method | request_id | payload (hex) |
|---|---|---|---|
| request_echo.bin | Echo | 0000000100000001 | 00 01 68 65 6c 6c 6f 2d 72 70 63 |
| request_slow.bin | Slow | 0000000100000002 | 00 02 00 00 00 96 6c 61 74 65 72 |
| request_add.bin | Add | 0000000100000003 | 00 03 00 00 00 00 00 00 00 28 00 00 00 00 00 00 00 02 |
| request_stats.bin | Stats | 0000000100000004 | 00 04 |
