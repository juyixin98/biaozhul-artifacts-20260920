# RUNLOG — 实际运行记录

生成时间: 2026-09-23 10:51:32 CST
主机: Linux 6.8.0-90-generic x86_64

```
cargo 1.98.1 (797e8a9bc 2026-08-05)
rustc 1.98.1 (48a229cea 2026-09-01)
```

## 1. 干净构建 (cargo clean && cargo build --release)
```
   Compiling merkle_store v0.1.0 (/home/admin/Downloads/biaozhul/opp10/a)
    Finished `release` profile [optimized] target(s) in 8.00s
exit=0
```

## 2. 单元 + 集成测试 (cargo test)
```
   Compiling merkle_store v0.1.0 (/home/admin/Downloads/biaozhul/opp10/a)
    Finished `test` profile [unoptimized + debuginfo] target(s) in 1.47s
     Running unittests src/lib.rs (target/debug/deps/merkle_store-bbf08c9f6f51e2b8)

running 32 tests
test base64::tests::rfc4648_vectors ... ok
test base64::tests::rejects_bad_input ... ok
test json::tests::rejects_bad_json ... ok
test json::tests::unicode_escape ... ok
test base64::tests::roundtrip ... ok
test json::tests::roundtrip_basic ... ok
test merkle::tests::empty_file_root_is_sentinel ... ok
test merkle::tests::empty_range_rejected_on_nonempty_tree ... ok
test merkle::tests::forged_lengths_detected ... ok
test merkle::tests::leaf_and_internal_domains_differ ... ok
test merkle::tests::first_and_last_single_block_ranges ... ok
test merkle::tests::full_range_needs_no_siblings ... ok
test merkle::tests::odd_rule_does_not_duplicate ... ok
test merkle::tests::proof_json_roundtrip ... ok
test store::tests::empty_repo_has_empty_root ... ok
test merkle::tests::tampered_block_detected ... ok
test merkle::tests::misaligned_and_resized_proofs_detected ... ok
test store::tests::manifest_roundtrip_and_validation ... ok
test store::tests::empty_reset_crash_recovers_to_empty ... ok
test store::tests::put_validation ... ok
test store::tests::last_block_can_shrink_and_grow ... ok
test store::tests::range_proof_from_store_verifies ... ok
test store::tests::silent_level_corruption_without_journal_is_detected ... ok
test vfs::tests::corrupt_flips_byte ... ok
test vfs::tests::faulty_fires_once_then_passes ... ok
test vfs::tests::memfs_write_read_and_holes ... ok
test store::tests::recover_torn_level_bytes ... ok
test store::tests::reset_then_crash_recovers ... ok
test store::tests::recover_after_each_failure_point ... ok
test store::tests::incremental_puts_match_full_rebuild ... ok
test sha256::tests::known_answer_vectors ... ok
test merkle::tests::all_ranges_verify_for_many_sizes ... ok

test result: ok. 32 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 1.01s

     Running unittests src/main.rs (target/debug/deps/merkle_store-9c434f745e568f74)

running 0 tests

test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

     Running tests/acceptance.rs (target/debug/deps/acceptance-49d16bf20a119088)

running 8 tests
test incremental_update_writes_only_one_path ... ok
test forged_length_is_rejected ... ok
test first_and_last_range_proofs_verify_without_reading_other_blocks ... ok
test empty_file_has_sentinel_root_and_verifies ... ok
test misaligned_proofs_are_rejected ... ok
test real_fs_crash_replay_at_every_step ... ok
test incremental_matches_full_rebuild_after_mixed_updates ... ok
test exhaustive_range_verification_small_trees ... ok

test result: ok. 8 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.72s

     Running tests/http_api.rs (target/debug/deps/http_api-ea1fea0d27f23228)

running 3 tests
test http_put_block_incremental_and_errors ... ok
test http_empty_file_verifies ... ok
test http_build_root_range_and_verify ... ok

test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.12s

   Doc-tests merkle_store

running 0 tests

test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s

exit=0
```

## 3. Lint (cargo clippy --all-targets)
```
    Checking merkle_store v0.1.0 (/home/admin/Downloads/biaozhul/opp10/a)
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 0.67s
exit=0
```

## 4. CLI 端到端（实际命令与输出）
```
initialized empty repository at /tmp/runlog-repo (block_size=4)
{
  "block_size": 4,
  "data_len": 11,
  "n": 3,
  "root": "8o17QRYFIiuME9lRW5szSeHoLhM79Jp50lbkLzkcR3g=",
  "root_hex": "f28d7b411605222b8c13d9515b9b3349e1e82e133bf49a79d256e42f391c4778"
}

initialized empty repository at /tmp/runlog-rebuild (block_size=4)
incremental root = 8o17QRYFIiuME9lRW5szSeHoLhM79Jp50lbkLzkcR3g=
full rebuild root= 8o17QRYFIiuME9lRW5szSeHoLhM79Jp50lbkLzkcR3g=
ROOTS MATCH
```

### 4a. 离线验证：合法 / 篡改 / 伪造长度 / 错位证明
```
$ ./target/release/merkle-store verify /tmp/runlog-first.json
{
  "valid": true
}
exit=0

$ ./target/release/merkle-store verify /tmp/runlog-last.json
{
  "valid": true
}
exit=0

$ ./target/release/merkle-store verify (tampered block)
{
  "error": "leaf 0 hash mismatch with supplied block bytes",
  "valid": false
}
exit=1

$ ./target/release/merkle-store verify examples/verify-forged-length.json
{
  "error": "declared byte length disagrees with leaf count and block size",
  "valid": false
}
exit=1

$ ./target/release/merkle-store verify examples/verify-misaligned-proof.json
{
  "error": "misaligned proof node: expected node (0,1), got (1,1)",
  "valid": false
}
exit=1

$ ./target/release/merkle-store verify examples/verify-empty-file.json
{
  "valid": true
}
exit=0
```

## 5. HTTP 端到端（两个独立进程：存储 :18380 / 空库验证者 :18381）
```
$ curl -s http://127.0.0.1:18380/health
{
  "status": "ok"
}

$ printf abcdefghijk | POST /build
{
  "data_len": 11,
  "n": 3,
  "ok": true,
  "root": "8o17QRYFIiuME9lRW5szSeHoLhM79Jp50lbkLzkcR3g=",
  "root_hex": "f28d7b411605222b8c13d9515b9b3349e1e82e133bf49a79d256e42f391c4778"
}

$ GET /range?which=first (在独立空库验证者上 /verify)
{
  "valid": true
}

$ GET /range?which=last (短末块，独立验证者)
{
  "valid": true
}

# 篡改块字节后在独立验证者上验证
{
  "error": "leaf 0 hash mismatch with supplied block bytes",
  "valid": false
}

# 增量写：先写满末块再追加
{'ok': True, 'index': 2, 'data_len': 12, 'n': 3}
{'ok': True, 'index': 3, 'data_len': 14, 'n': 4}
```

## 6. scripts/demo.sh（退出码）
```
demo.sh exit=0
```

## 7. 未通过项 / 已知限制

- 无未通过的验收测试：43/43 测试通过，clippy 零警告（见第 2、3 节）。
- 开发过程中发现并修复的真实缺陷（均有回归测试覆盖）：
  1. 覆盖非末块时旧实现会错误 truncate data.bin，已修复为仅末块可缩短；
  2. 短末块后追加新块会在 data.bin 产生零字节空洞，已在 API 层拒绝（HTTP 400）；
  3. 空载荷 reset 的日志此前为空，崩溃在 journal 之后会丢失“清空”事务，已强制始终写 RESET 条目。
- 已知非目标（见 README 第 6 节）：无鉴权/TLS、写串行化、journal 仅支持单语句事务。
- 端口 18081 在本机被其他进程（twin-superblock）占用，演示改用 18181/18281 等端口；与本项目无关。
