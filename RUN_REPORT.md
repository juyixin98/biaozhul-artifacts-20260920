# 运行报告（RUN_REPORT）

本报告如实记录在交付环境中的构建与运行结果、遇到的问题及取舍、以及未完成项。

## 环境

- OS：Linux 6.8.0-90-generic（x86_64，Ubuntu 24.04），`cc` 13.3.0。
- 工具链：**rustc / cargo 1.98.1（stable，2026-09-01）**，2021 edition。
- 依赖：见 `Cargo.lock`（共 67 个包条目，含本项目；锁定如下关键版本）：
  `axum 0.7.9`、`tokio 1.53.1`、`hyper 1.11.1`、`serde 1.0.x`、
  `serde_json 1.0.151`、`sha2 0.10.9`、`hex 0.4.3`。
- 无外部数据库/服务；数据落盘为普通文件。

> 注：交付机是多租户环境，多个会话共享 `~/.rustup` / `~/.cargo`，期间共享工具链
> 被反复卸载重装、`/home/admin/.cargo/config.toml` 还配置了第三方镜像。为避免相互干扰，
> 本次构建实际使用了**隔离的 `RUSTUP_HOME`/`CARGO_HOME`**，并以官方 crates.io
> （`--offline` 复用本机已缓存的官方同名包 + 从 `static.crates.io` 直连补齐少量小包）。
> 用户在自己机器上按 README 用标准 `cargo build / cargo test` 即可，无需任何镜像或隔离目录。

## 构建

```
$ cargo build --release
    Finished `release` profile [optimized]   # 零 warning
$ ls -la target/release/refcount-store
-rwxr-xr-x ... 1.8M  target/release/refcount-store
```

`cargo build`（debug）与 `cargo build --release` 均成功，**编译器无任何 warning**。

## 自动化测试（实际运行）

命令：`cargo test`（`cargo test --offline` 在隔离环境运行，结果相同）。

```
running 5 tests   # tests/http_test.rs（真实 TCP 启动 Axum 服务，逐字节校验）
test object_metadata_reports_kind_and_size ... ok
test binary_roundtrip_preserves_bytes ........ ok   # 0..=255 全字节往返
test rejects_invalid_and_dangling_inputs ..... ok   # 400 / 404
test roots_list_and_persist_semantics ........ ok
test full_acceptance_flow_over_http .......... ok   # HTTP 端到端验收

running 8 tests   # tests/store_test.rs
test content_addressing_is_deterministic .......................... ok
test publishing_dangling_root_is_rejected .......................... ok
test data_block_looking_like_manifest_is_not_traversed ............. ok
test gc_keeps_shared_subgraph_and_collects_orphan .................. ok
test deleting_both_roots_collects_everything ....................... ok
test completed_upload_can_be_published_and_then_lives_on ........... ok
test open_upload_objects_are_retained_then_collected ............... ok
test concurrent_publish_vs_restarting_gc_never_drops_live_objects .. ok

test result: ok. 5 passed; 0 failed   (http_test)
test result: ok. 8 passed; 0 failed   (store_test)
```

**13/13 全部通过**。并发验收测试连续多次（单独重跑 3 次以上、整套多次）均稳定通过。

## 验收点的实际演示（对运行中的 release 服务执行）

启动：`RCS_DATA_DIR=... RCS_BIND=127.0.0.1:<port> RCS_RETENTION=1 ./refcount-store`，
然后 `BASE=http://127.0.0.1:<port> ./examples/demo.sh`，脚本退出码 `0`。实测：

1. **构造共享子图**：b1、b2 ← 共享清单 `shared`；根清单
   `mA=[shared,aOnly]`、`mB=[shared,bOnly]`，分别发布为根 A、B。
2. **删除一个根 + GC**：删除根 A 后执行 `POST /gc`，实际返回
   ```json
   {"reachable":5,"retained_by_upload":0,"open_uploads":0,
    "deleted":["<orphan>","<aOnly>","<mA>"]}
   ```
   - 仍可达块保留：b1、b2、shared、mB、bOnly 经 `GET /objects/...` 全部 **HTTP 200**。
   - 孤立块最终可回收：mA、aOnly、orphan 全部 **HTTP 404**（出现在 `deleted`）。
3. **并发发布另一根 + 回收重启**：先在一个**打开的上传会话**里暂存新闭包
   （c1、mC），随后 `POST /gc` 与 `PUT /roots/C` 并发发出；发布后再 complete、再 GC。
   新根块 c1 最终 `GET` 为 **HTTP 200**——证明“删除旧根/发布新根”与 GC 之间
   不会误删新根可达对象（GC 进行中由打开的上传保留，发布后由根可达保留）。
4. **未完成上传的独立保留期**：
   - 打开会话放块后 GC：`{"open_uploads":2,"retained_by_upload":2,"deleted":[]}`，块 **200**；
   - complete 后立即 GC（1s 保留期内）：仍保留；
   - 等待 >2s 越过保留期再 GC：该块出现在 `deleted`，随后 `GET` 为 **HTTP 404**。
5. **跨进程重启持久化**：对同一数据目录重启服务后，`roots.json` 中的根与
   `objects/` 中的对象（8 个）完整恢复，健康检查与根查询结果与重启前一致。

## 设计中被测试驱动发现并修复的真实并发缺陷（如实记录）

在最初实现上运行高并发压力测试，暴露并修复了三个问题（这些都由自动化测试捕获）：

1. **put 与 GC 的文件/索引窗口**：原实现“rename 落盘”和“加入内存索引”分两步，
   GC 可能在中间把刚 rename 的文件当垃圾删掉。修复：整段在对象索引写锁内完成，
   与 GC 全程互斥。
2. **上传登记窗口**：`put_for_upload` 原先先写对象、再登记到上传保留集合，两步之间
   GC 可回收“已在索引但尚未被上传/根保护”的在途对象。修复：把“写文件 + 入索引 +
   记入打开的上传”放在同一把对象索引写锁内原子完成（锁顺序 index→uploads，与 GC 一致）。
3. **元数据临时文件名冲突**：`kinds.json`/`roots.json` 用固定临时文件名，多写者并发时
   一个 rename 会消耗另一个的临时文件导致 `NotFound`。修复：改用带纳秒+序号的唯一
   临时文件名再原子 rename。

另外修正了测试/演示脚本的一个**语义错误**：让两个根清单引用完全相同的 refs，
内容寻址会使二者哈希相同（去重），删一个根不会回收该清单。改为各自带一个独有块，
才能真实展示“共享部分保留、各自私有部分随根删除而回收”。

## 未完成项 / 限制（如实说明）

- **无鉴权 / TLS / 鉴权与限流**：纯后端练习服务，默认只监听 `127.0.0.1`，未实现
  身份认证、HTTPS、配额或速率限制。
- **单机文件存储**：对象与元数据存于单目录；未做分布式/多副本、文件分片或压缩。
  GC 全程持有进程内锁，仅保证**单进程内**的并发安全（多进程共享同一目录不在范围内）。
- **上传会话为进程内状态**：打开的上传记录保存在内存（不落盘）。进程重启后打开的
  上传会丢失其“保留”语义（其对象若不被任何根可达，将可被回收）；已发布根和已落盘
  对象本身是持久的。需要更强语义时可把上传记录也持久化（当前未做）。
- **保留期按整秒时间戳**判断，过期回收需在保留期结束后再触发一次 GC（不会后台自动回收）。
- **未安装/运行 `rustfmt`、`clippy`**：交付工具链为 minimal profile 且离线，未含这两个
  组件；代码已手工按 rustfmt 默认风格排版，编译零 warning。
- 目录中提供的是源码、`Cargo.lock`、测试、`examples/demo.sh` 与文档；未附带预编译二进制。
