//! Store 层直接测试：额度、超时（注入时钟）、幂等取消、并发超订、持久化。

use std::sync::{Arc, Barrier};

use quota_reserve::clock::FakeClock;
use quota_reserve::store::{Status, Store, StoreError};

const T0: i64 = 1_700_000_000_000; // 固定起始时间（毫秒）

fn setup(byte: i64, objects: i64) -> (Store, Arc<FakeClock>) {
    let store = Store::in_memory().unwrap();
    let clock = Arc::new(FakeClock::new(T0));
    store.create_tenant("t1", byte, objects, T0).unwrap();
    (store, clock)
}

#[test]
fn reserve_then_usage_reflects_hold() {
    let (s, c) = setup(1000, 10);
    let (id, exp) = s.reserve(c.as_ref(), "t1", 300, 3, 1000).unwrap();
    assert_eq!(exp, T0 + 1000);

    let u = s.usage(c.as_ref(), "t1").unwrap();
    assert_eq!(u.reserved_bytes, 300);
    assert_eq!(u.reserved_objects, 3);
    assert_eq!(u.committed_bytes, 0);
    assert_eq!(u.total_bytes(), 300);
    assert_eq!(u.total_objects(), 3);

    let r = s.get_reservation(&id).unwrap();
    assert_eq!(r.status, "reserved");
}

#[test]
fn quota_rejects_over_byte_and_object_limits() {
    let (s, c) = setup(1000, 10);

    // 字节超限
    let err = s.reserve(c.as_ref(), "t1", 1001, 1, 1000).unwrap_err();
    assert!(matches!(
        err,
        StoreError::QuotaExceeded { bytes_used: 0, bytes_wanted: 1001, bytes_limit: 1000, .. }
    ));

    s.reserve(c.as_ref(), "t1", 500, 6, 1000).unwrap();

    // 对象数超限（字节还够）
    let err = s.reserve(c.as_ref(), "t1", 100, 5, 1000).unwrap_err();
    match err {
        StoreError::QuotaExceeded { objects_used, objects_wanted, objects_limit, .. } => {
            assert_eq!((objects_used, objects_wanted, objects_limit), (6, 5, 10));
        }
        other => panic!("expected quota exceeded, got {other:?}"),
    }

    // 被拒绝的预留不落库，占用不变
    let u = s.usage(c.as_ref(), "t1").unwrap();
    assert_eq!(u.total_bytes(), 500);
    assert_eq!(u.total_objects(), 6);
}

#[test]
fn commit_converts_hold_to_real_usage() {
    let (s, c) = setup(1000, 10);
    let (id, _) = s.reserve(c.as_ref(), "t1", 400, 4, 1000).unwrap();
    s.commit(c.as_ref(), &id).unwrap();

    let u = s.usage(c.as_ref(), "t1").unwrap();
    assert_eq!(u.reserved_bytes, 0);
    assert_eq!(u.committed_bytes, 400);
    assert_eq!(u.committed_objects, 4);
    assert_eq!(u.total_bytes(), 400);
    assert_eq!(s.get_reservation(&id).unwrap().status, "committed");

    // 重复提交幂等，不重复计账
    s.commit(c.as_ref(), &id).unwrap();
    let u = s.usage(c.as_ref(), "t1").unwrap();
    assert_eq!(u.committed_bytes, 400);
}

#[test]
fn cancel_releases_quota_and_double_cancel_does_not_over_release() {
    let (s, c) = setup(1000, 10);
    let (id, _) = s.reserve(c.as_ref(), "t1", 800, 8, 1000).unwrap();

    // 额度已满，再预留失败
    assert!(matches!(
        s.reserve(c.as_ref(), "t1", 300, 3, 1000),
        Err(StoreError::QuotaExceeded { .. })
    ));

    // 第一次取消释放
    let st = s.cancel(c.as_ref(), &id).unwrap();
    assert_eq!(st, Status::Cancelled);
    // 第二次取消：幂等成功，但绝不二次释放
    let st = s.cancel(c.as_ref(), &id).unwrap();
    assert_eq!(st, Status::Cancelled);

    let u = s.usage(c.as_ref(), "t1").unwrap();
    assert_eq!(u.reserved_bytes, 0);
    assert_eq!(u.reserved_objects, 0);
    assert_eq!(u.committed_bytes, 0);

    // 释放后额度可被重新使用
    s.reserve(c.as_ref(), "t1", 1000, 10, 1000).unwrap();
    let u = s.usage(c.as_ref(), "t1").unwrap();
    assert_eq!(u.total_bytes(), 1000);
    assert_eq!(u.total_objects(), 10);
}

#[test]
fn expired_reservations_release_quota_on_injected_clock() {
    let (s, c) = setup(1000, 10);
    let (id1, _) = s.reserve(c.as_ref(), "t1", 600, 6, 1000).unwrap();
    let (id2, _) = s.reserve(c.as_ref(), "t1", 400, 4, 500).unwrap();
    assert_eq!(s.usage(c.as_ref(), "t1").unwrap().total_bytes(), 1000);

    // 推进 600ms：id2（ttl 500）到期，id1 仍活跃
    c.advance(600);
    assert_eq!(s.sweep_expired(c.as_ref()).unwrap(), 1);
    let u = s.usage(c.as_ref(), "t1").unwrap();
    assert_eq!(u.reserved_bytes, 600);
    assert_eq!(s.get_reservation(&id2).unwrap().status, "expired");

    // 到期后提交被拒，状态保持 expired
    assert!(matches!(s.commit(c.as_ref(), &id2), Err(StoreError::Expired)));
    assert_eq!(s.get_reservation(&id2).unwrap().status, "expired");

    // 释放出的 400 字节 / 4 对象可重新预留
    s.reserve(c.as_ref(), "t1", 400, 4, 1000).unwrap();
    assert_eq!(s.usage(c.as_ref(), "t1").unwrap().total_bytes(), 1000);

    // 推进到 id1 也到期；提交时按注入时钟判定失败
    c.advance(500);
    assert!(matches!(s.commit(c.as_ref(), &id1), Err(StoreError::Expired)));
    let u = s.usage(c.as_ref(), "t1").unwrap();
    // 后预留的那笔仍有效（其 ttl 从 T0+600 起算），id1 已过期
    assert_eq!(u.reserved_bytes, 400);
}

#[test]
fn illegal_transitions_and_missing_ids_are_rejected() {
    let (s, c) = setup(1000, 10);
    let (id, _) = s.reserve(c.as_ref(), "t1", 100, 1, 1000).unwrap();
    s.commit(c.as_ref(), &id).unwrap();

    // 已提交不能取消
    assert!(matches!(
        s.cancel(c.as_ref(), &id),
        Err(StoreError::Conflict { from: "committed", action: "cancel" })
    ));

    let (id2, _) = s.reserve(c.as_ref(), "t1", 100, 1, 1000).unwrap();
    s.cancel(c.as_ref(), &id2).unwrap();
    // 已取消不能提交
    assert!(matches!(s.commit(c.as_ref(), &id2), Err(StoreError::Conflict { .. })));

    // 不存在
    assert!(matches!(s.commit(c.as_ref(), "nope"), Err(StoreError::NotFound)));
    assert!(matches!(s.cancel(c.as_ref(), "nope"), Err(StoreError::NotFound)));

    // 未知租户 / 重复创建
    assert!(matches!(
        s.reserve(c.as_ref(), "ghost", 1, 1, 1000),
        Err(StoreError::TenantNotFound)
    ));
    assert!(matches!(
        s.create_tenant("t1", 1, 1, T0),
        Err(StoreError::TenantExists)
    ));
}

/// 验收核心：并发预留越过同一剩余额度时，合计占用绝不超限。
#[test]
fn concurrent_reservations_never_oversubscribe() {
    let (s, c) = setup(1000, 10);
    let threads = 20;
    let barrier = Arc::new(Barrier::new(threads));
    let mut handles = Vec::new();

    for _ in 0..threads {
        let store = s.clone();
        let clock: Arc<FakeClock> = c.clone();
        let barrier = barrier.clone();
        handles.push(std::thread::spawn(move || {
            barrier.wait(); // 尽量同时发起
            store.reserve(clock.as_ref(), "t1", 100, 1, 100_000)
        }));
    }

    let mut ok = 0;
    let mut rejected = 0;
    for h in handles {
        match h.join().unwrap() {
            Ok(_) => ok += 1,
            Err(StoreError::QuotaExceeded { .. }) => rejected += 1,
            Err(other) => panic!("unexpected error: {other:?}"),
        }
    }
    assert_eq!(ok, 10, "恰好 10 笔成功");
    assert_eq!(rejected, 10, "超出的 10 笔被拒");

    let u = s.usage(c.as_ref(), "t1").unwrap();
    assert_eq!(u.total_bytes(), 1000);
    assert_eq!(u.total_objects(), 10);
    assert!(u.total_bytes() <= u.byte_limit);
    assert!(u.total_objects() <= u.object_limit);
}

/// 验收场景：并发抢额度 → 穿插提交 / 重复取消 / 超时 → 全程不超限、不多释放。
#[test]
fn acceptance_interleaved_commit_double_cancel_and_timeout() {
    let (s, c) = setup(1000, 10);

    // 1) 20 个线程并发，各要 100 字节 / 1 对象，TTL 相同
    let winners: Vec<String> = {
        let barrier = Arc::new(Barrier::new(20));
        let mut handles = Vec::new();
        for _ in 0..20 {
            let store = s.clone();
            let clock: Arc<FakeClock> = c.clone();
            let barrier = barrier.clone();
            handles.push(std::thread::spawn(move || {
                barrier.wait();
                store.reserve(clock.as_ref(), "t1", 100, 1, 1000)
            }));
        }
        let mut ids = Vec::new();
        for h in handles {
            if let Ok((id, _)) = h.join().unwrap() {
                ids.push(id);
            }
        }
        assert_eq!(ids.len(), 10);
        ids
    };

    // 2) 提交前 3 笔，取消第 4、5 笔（第 4 笔取消两次）
    for id in &winners[0..3] {
        s.commit(c.as_ref(), id).unwrap();
    }
    s.cancel(c.as_ref(), &winners[3]).unwrap();
    s.cancel(c.as_ref(), &winners[3]).unwrap(); // 重复取消：不多释放
    s.cancel(c.as_ref(), &winners[4]).unwrap();

    let u = s.usage(c.as_ref(), "t1").unwrap();
    assert_eq!(u.committed_bytes, 300);
    assert_eq!(u.reserved_bytes, 500);
    assert_eq!(u.total_bytes(), 800);

    // 3) 时钟越过 TTL：剩余 5 笔全部到期
    c.advance(1001);
    let n = s.sweep_expired(c.as_ref()).unwrap();
    assert_eq!(n, 5, "剩余 5 笔预留应全部到期");

    // 对剩余预留尝试提交：全部明确返回超时，状态保持 expired
    let mut newly_committed = 0;
    for id in &winners[5..] {
        match s.commit(c.as_ref(), id) {
            Ok(()) => newly_committed += 1,
            Err(StoreError::Expired) => {}
            Err(other) => panic!("unexpected: {other:?}"),
        }
    }
    assert_eq!(newly_committed, 0);

    // 4) 全程不变量：预留 + 实占 <= 配额（两维）
    let u = s.usage(c.as_ref(), "t1").unwrap();
    assert!(u.total_bytes() <= u.byte_limit, "usage={:?}", u);
    assert!(u.total_objects() <= u.object_limit, "usage={:?}", u);
    assert_eq!(u.reserved_bytes, 0, "到期后不应再有活跃预留, {:?}", u);
    assert_eq!(u.committed_bytes, 300);
    assert_eq!(u.committed_objects, 3);

    // 5) 重复取消不影响账：已取消的两笔状态仍是 cancelled，用量里无其痕迹
    assert_eq!(s.get_reservation(&winners[3]).unwrap().status, "cancelled");
    assert_eq!(s.get_reservation(&winners[4]).unwrap().status, "cancelled");

    // 6) 释放出的额度现在可重新使用：补到恰好上限，再提交
    let free_bytes = u.byte_limit - u.committed_bytes;
    let free_objs = u.object_limit - u.committed_objects;
    let (new_id, _) = s.reserve(c.as_ref(), "t1", free_bytes, free_objs, 50_000).unwrap();
    let u = s.usage(c.as_ref(), "t1").unwrap();
    assert_eq!(u.total_bytes(), 1000);
    assert_eq!(u.total_objects(), 10);

    // 再多 1 字节 / 1 对象必被拒（预留+实占合并计算）
    assert!(matches!(
        s.reserve(c.as_ref(), "t1", 1, 1, 50_000),
        Err(StoreError::QuotaExceeded { .. })
    ));

    // 新预留可正常提交转实占
    s.commit(c.as_ref(), &new_id).unwrap();
    let u = s.usage(c.as_ref(), "t1").unwrap();
    assert_eq!(u.committed_bytes, 1000);
    assert_eq!(u.reserved_bytes, 0);
}

/// 所有状态转换持久化：重开数据库后账本一致。
#[test]
fn state_survives_database_reopen() {
    let dir = std::env::temp_dir();
    let path = dir.join(format!(
        "quota-test-{}.db",
        uuid::Uuid::new_v4().simple()
    ));
    let path_str = path.to_str().unwrap().to_string();

    {
        let s = Store::open(&path_str).unwrap();
        let clock = FakeClock::new(T0);
        s.create_tenant("t1", 1000, 10, T0).unwrap();
        let (id1, _) = s.reserve(&clock, "t1", 300, 3, 1000).unwrap();
        let (_id2, _) = s.reserve(&clock, "t1", 200, 2, 1000).unwrap();
        s.commit(&clock, &id1).unwrap();
    } // drop

    let s2 = Store::open(&path_str).unwrap();
    let clock = FakeClock::new(T0 + 1001); // 重开时已越过 ttl
    let u = s2.usage(&clock, "t1").unwrap(); // usage 触发过期扫描
    assert_eq!(u.committed_bytes, 300);
    assert_eq!(u.committed_objects, 3);
    assert_eq!(u.reserved_bytes, 0, "未提交的预留重开后按时钟判定到期");
    assert_eq!(u.total_bytes(), 300);

    let _ = std::fs::remove_file(&path);
    let _ = std::fs::remove_file(path.with_extension("db-wal"));
    let _ = std::fs::remove_file(path.with_extension("db-shm"));
}
