# 实际运行记录（2026-09-23）

本机环境：Linux 6.8.0 x86_64，gcc 可用。

## 工具链

- rustc / cargo：**1.98.1 stable**（2021 edition）
- `rusqlite` 使用 `bundled` 特性，编译期自带 SQLite，系统未安装 libsqlite3 也可构建。
- 本机网络访问 crates.io / static.rust-lang.org 较慢，实际构建时使用了
  USTC 镜像加速（未写入仓库配置，不影响其它网络环境）：

  ```bash
  # 仅在访问官方源很慢时才需要，普通网络直接 cargo build 即可
  export RUSTUP_DIST_SERVER=https://mirrors.ustc.edu.cn/rust-static   # 装工具链时
  # ~/.cargo/config.toml:
  # [source.crates-io]
  # replace-with = "ustc"
  # [source.ustc]
  # registry = "sparse+https://mirrors.ustc.edu.cn/crates.io-index/"
  ```

## 构建

```text
$ cargo build
    Finished `dev` profile [unoptimized + debuginfo] target(s)
$ cargo build --release
    Finished `release` profile [optimized] target(s)
```

## 自动化测试（cargo test）

11 个测试全部通过；连续运行 3 轮结果一致（含并发测试，无偶发失败）：

```text
running 2 tests
test double_cancel_over_http_is_idempotent ... ok
test full_lifecycle_over_http ... ok
test result: ok. 2 passed; 0 failed

running 9 tests
test reserve_then_usage_reflects_hold ... ok
test quota_rejects_over_byte_and_object_limits ... ok
test commit_converts_hold_to_real_usage ... ok
test cancel_releases_quota_and_double_cancel_does_not_over_release ... ok
test expired_reservations_release_quota_on_injected_clock ... ok
test illegal_transitions_and_missing_ids_are_rejected ... ok
test concurrent_reservations_never_oversubscribe ... ok              # 20 线程并发抢 10 个名额
test acceptance_interleaved_commit_double_cancel_and_timeout ... ok  # 验收组合场景
test state_survives_database_reopen ... ok
test result: ok. 9 passed; 0 failed
```

## 真实 HTTP 运行抽查（target/debug/quota-reserve）

1. **基本生命周期**：建租户 → 预留 600/6 → 超额预留返回 `409 quota_exceeded`
   （响应体带两维 used/requested/limit）→ 提交后 `committed_bytes=600` →
   对已提交预留取消返回 `409 illegal_transition`。
2. **并发验收（真实 HTTP，20 个 curl 并发）**：配额 1000 字节/10 对象，
   20 个请求各要 100 字节/1 对象，结果 **恰好 10 个 201、10 个 409**；
   `GET /usage` 显示 `reserved_bytes=1000, reserved_objects=10`，未越过剩余额度。
3. **超时**：`ttl_ms=1200` 的预留在真实等待 2s 后提交返回
   `410 reservation_expired`，预留状态为 `expired`，额度随后可被新预留使用。
4. **重复取消不多释放**：同一预留连续两次 `POST /cancel` 均返回
   `200 {"status":"cancelled"}`，用量两次查询完全相同（幂等，不二次释放）。
5. **持久化**：预留一笔长 TTL（1 小时）后 SIGTERM 停服、重开同一 DB 文件，
   预留仍为 `reserved`、占用仍计入账本；已 `committed` 的 600/6 也完好；
   超过 TTL 的预留重开后按当前时钟正确判定为过期。
6. `examples/curl-demo.sh` 对运行中的服务实际执行通过。

## 未完成项 / 已知取舍

- 未做鉴权与多租户网络层隔离；默认只监听 127.0.0.1。
- 无后台定时清理线程；超时为请求路径惰性判定 + `POST /admin/sweep` 主动扫描，
  生产可外部挂 cron 周期调用。
- 单连接 + Mutex + SQLite，面向单机强一致，未做高可用/水平扩展。
- 时间戳为毫秒；未提供配额修改（PUT 已存在租户目前返回 409）接口。
