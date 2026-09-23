//! 领域模型与 SQLite 持久化。
//!
//! 配额账本（均按租户聚合）：
//!   预留占用 = status = 'reserved' 且未到期的预留之和
//!   实际占用 = status = 'committed' 的预留之和
//!   约束    ：预留占用 + 实际占用 <= 配额上限（字节、对象数两维独立判断）
//!
//! 所有状态转换都在一个 `BEGIN IMMEDIATE` 事务内完成并立即落盘，
//! 因此并发预留由 SQLite 写锁串行化，不会越过同一剩余额度。

use std::sync::{Arc, Mutex};

use rusqlite::{params, Connection};
use serde::Serialize;

use crate::clock::Clock;

/// 预留生命周期状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Status {
    /// 已预留，占用额度但尚未转实占
    Reserved,
    /// 已提交，转为实际占用
    Committed,
    /// 已取消，额度释放
    Cancelled,
    /// 预留超时，额度释放
    Expired,
}

impl Status {
    pub fn as_str(self) -> &'static str {
        match self {
            Status::Reserved => "reserved",
            Status::Committed => "committed",
            Status::Cancelled => "cancelled",
            Status::Expired => "expired",
        }
    }

    pub fn parse(s: &str) -> Option<Status> {
        match s {
            "reserved" => Some(Status::Reserved),
            "committed" => Some(Status::Committed),
            "cancelled" => Some(Status::Cancelled),
            "expired" => Some(Status::Expired),
            _ => None,
        }
    }
}

#[derive(Debug)]
pub enum StoreError {
    /// 额度不足：(已占用+本次需要的字节, 字节上限, 对象同理)
    QuotaExceeded {
        bytes_used: i64,
        bytes_wanted: i64,
        bytes_limit: i64,
        objects_used: i64,
        objects_wanted: i64,
        objects_limit: i64,
    },
    /// 预留不存在
    NotFound,
    /// 预留已到期（提交/取消时）
    Expired,
    /// 状态不允许该转换，如对已提交预留再次取消
    Conflict { from: &'static str, action: &'static str },
    /// 租户不存在
    TenantNotFound,
    /// 租户已存在
    TenantExists,
    /// 其它持久化错误
    Db(rusqlite::Error),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::QuotaExceeded { bytes_used, bytes_wanted, bytes_limit, objects_used, objects_wanted, objects_limit } => {
                write!(
                    f,
                    "quota exceeded: bytes {bytes_used}+{bytes_wanted}>{bytes_limit} or objects {objects_used}+{objects_wanted}>{objects_limit}"
                )
            }
            StoreError::NotFound => write!(f, "reservation not found"),
            StoreError::Expired => write!(f, "reservation already expired"),
            StoreError::Conflict { from, action } => {
                write!(f, "illegal transition: cannot {action} a {from} reservation")
            }
            StoreError::TenantNotFound => write!(f, "tenant not found"),
            StoreError::TenantExists => write!(f, "tenant already exists"),
            StoreError::Db(e) => write!(f, "database error: {e}"),
        }
    }
}

impl std::error::Error for StoreError {}

impl From<rusqlite::Error> for StoreError {
    fn from(e: rusqlite::Error) -> Self {
        StoreError::Db(e)
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct Usage {
    pub tenant_id: String,
    pub byte_limit: i64,
    pub object_limit: i64,
    /// 活跃（未到期）预留字节数
    pub reserved_bytes: i64,
    /// 活跃预留对象数
    pub reserved_objects: i64,
    /// 已提交（实占）字节数
    pub committed_bytes: i64,
    pub committed_objects: i64,
    /// 本次统计使用的时钟时间（毫秒）
    pub at_ms: i64,
}

impl Usage {
    /// 预留 + 实占合计，验收即检查该值不超过上限。
    pub fn total_bytes(&self) -> i64 {
        self.reserved_bytes + self.committed_bytes
    }
    pub fn total_objects(&self) -> i64 {
        self.reserved_objects + self.committed_objects
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct ReservationView {
    pub id: String,
    pub tenant_id: String,
    pub byte_size: i64,
    pub object_count: i64,
    pub status: String,
    pub created_at: i64,
    pub expires_at: i64,
    pub updated_at: i64,
}

/// 线程安全的配额存储：单连接 + Mutex，写事务天然串行。
#[derive(Clone)]
pub struct Store {
    conn: Arc<Mutex<Connection>>,
}

impl Store {
    /// 打开（不存在则创建）数据库并初始化表结构。
    pub fn open(path: &str) -> Result<Self, StoreError> {
        let conn = Connection::open(path)?;
        conn.pragma_update(None, "journal_mode", "WAL")?;
        conn.pragma_update(None, "foreign_keys", "ON")?;
        conn.pragma_update(None, "synchronous", "FULL")?;
        conn.busy_timeout(std::time::Duration::from_secs(5))?;
        conn.execute_batch(include_str!("schema.sql"))?;
        Ok(Self { conn: Arc::new(Mutex::new(conn)) })
    }

    /// 测试用内存数据库。
    pub fn in_memory() -> Result<Self, StoreError> {
        let conn = Connection::open_in_memory()?;
        conn.execute_batch(include_str!("schema.sql"))?;
        Ok(Self { conn: Arc::new(Mutex::new(conn)) })
    }

    pub fn create_tenant(
        &self,
        tenant_id: &str,
        byte_quota: i64,
        object_quota: i64,
        now_ms: i64,
    ) -> Result<(), StoreError> {
        if byte_quota < 0 || object_quota < 0 {
            return Err(StoreError::Conflict { from: "invalid", action: "create quota" });
        }
        let conn = self.conn.lock().unwrap();
        let affected = conn.execute(
            "INSERT INTO tenants(tenant_id, byte_quota, object_quota, created_at)
             VALUES (?1, ?2, ?3, ?4)
             ON CONFLICT(tenant_id) DO NOTHING",
            params![tenant_id, byte_quota, object_quota, now_ms],
        )?;
        if affected == 0 {
            Err(StoreError::TenantExists)
        } else {
            Ok(())
        }
    }

    /// 创建预留：先把到期预留扫成 expired，再在同一事务内做两维额度检查。
    pub fn reserve(
        &self,
        clock: &dyn Clock,
        tenant_id: &str,
        byte_size: i64,
        object_count: i64,
        ttl_ms: i64,
    ) -> Result<(String, i64), StoreError> {
        if byte_size < 0 || object_count < 0 {
            return Err(StoreError::Conflict { from: "invalid", action: "reserve" });
        }
        if ttl_ms <= 0 {
            return Err(StoreError::Conflict { from: "invalid", action: "reserve with non-positive ttl" });
        }
        let now = clock.now_ms();
        let id = uuid::Uuid::new_v4().to_string();
        let expires_at = now + ttl_ms;

        let conn = self.conn.lock().unwrap();
        // 到期清理先独立提交：即使本次预留因超额回滚，到期状态也已持久化。
        expire_due_committed(&conn, now)?;
        conn.execute_batch("BEGIN IMMEDIATE")?;
        // 用闭包保证事务在任何错误路径下都会结束（ROLLBACK / COMMIT）。
        let result = (|| -> Result<(String, i64), StoreError> {
            let quota: (i64, i64) = conn
                .query_row(
                    "SELECT byte_quota, object_quota FROM tenants WHERE tenant_id = ?1",
                    params![tenant_id],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .map_err(|e| match e {
                    rusqlite::Error::QueryReturnedNoRows => StoreError::TenantNotFound,
                    other => StoreError::Db(other),
                })?;

            let used: (i64, i64) = conn.query_row(
                "SELECT COALESCE(SUM(byte_size),0), COALESCE(SUM(object_count),0)
                 FROM reservations
                 WHERE tenant_id = ?1 AND status IN ('reserved', 'committed')",
                params![tenant_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )?;

            let bytes_used = used.0;
            let objects_used = used.1;
            if bytes_used + byte_size > quota.0 || objects_used + object_count > quota.1 {
                return Err(StoreError::QuotaExceeded {
                    bytes_used,
                    bytes_wanted: byte_size,
                    bytes_limit: quota.0,
                    objects_used,
                    objects_wanted: object_count,
                    objects_limit: quota.1,
                });
            }

            conn.execute(
                "INSERT INTO reservations
                   (id, tenant_id, byte_size, object_count, status, created_at, expires_at, updated_at)
                 VALUES (?1, ?2, ?3, ?4, 'reserved', ?5, ?6, ?5)",
                params![id, tenant_id, byte_size, object_count, now, expires_at],
            )?;
            Ok((id, expires_at))
        })();
        finish_tx(&conn, result)
    }

    /// 提交：reserved -> committed。已到期预留会先转为 expired 并报错。
    pub fn commit(&self, clock: &dyn Clock, reservation_id: &str) -> Result<(), StoreError> {
        self.transition(clock, reservation_id, Status::Committed)
    }

    /// 取消：reserved -> cancelled。
    /// 重复取消是幂等的（第二次直接返回当前状态），绝不会二次释放，
    /// 因为释放只来自一条 `... WHERE status='reserved'` 的条件更新。
    pub fn cancel(&self, clock: &dyn Clock, reservation_id: &str) -> Result<Status, StoreError> {
        let now = clock.now_ms();
        let conn = self.conn.lock().unwrap();
        expire_due_committed(&conn, now)?;
        conn.execute_batch("BEGIN IMMEDIATE")?;
        let result = (|| -> Result<Status, StoreError> {
            let row = conn.query_row(
                "SELECT status, expires_at FROM reservations WHERE id = ?1",
                params![reservation_id],
                |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)),
            );
            let (status_str, expires_at) = match row {
                Ok(v) => v,
                Err(rusqlite::Error::QueryReturnedNoRows) => return Err(StoreError::NotFound),
                Err(e) => return Err(StoreError::Db(e)),
            };
            let status = Status::parse(&status_str).unwrap();

            // 活跃但已到期：先落 expired 再拒绝。
            if status == Status::Reserved && expires_at <= now {
                set_status(&conn, reservation_id, Status::Expired, now)?;
                return Err(StoreError::Expired);
            }
            match status {
                Status::Cancelled => Ok(Status::Cancelled), // 幂等：不重复释放
                Status::Expired => Err(StoreError::Expired),
                Status::Committed => Err(StoreError::Conflict {
                    from: "committed",
                    action: "cancel",
                }),
                Status::Reserved => {
                    let affected = conn.execute(
                        "UPDATE reservations SET status='cancelled', updated_at=?2
                         WHERE id=?1 AND status='reserved'",
                        params![reservation_id, now],
                    )?;
                    debug_assert_eq!(affected, 1);
                    Ok(Status::Cancelled)
                }
            }
        })();
        finish_tx(&conn, result)
    }

    fn transition(
        &self,
        clock: &dyn Clock,
        reservation_id: &str,
        target: Status,
    ) -> Result<(), StoreError> {
        let now = clock.now_ms();
        let conn = self.conn.lock().unwrap();
        expire_due_committed(&conn, now)?;
        conn.execute_batch("BEGIN IMMEDIATE")?;
        let result = (|| -> Result<(), StoreError> {
            let row = conn.query_row(
                "SELECT status, expires_at FROM reservations WHERE id = ?1",
                params![reservation_id],
                |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?)),
            );
            let (status_str, expires_at) = match row {
                Ok(v) => v,
                Err(rusqlite::Error::QueryReturnedNoRows) => return Err(StoreError::NotFound),
                Err(e) => return Err(StoreError::Db(e)),
            };
            let status = Status::parse(&status_str).unwrap();

            if status == Status::Reserved && expires_at <= now {
                set_status(&conn, reservation_id, Status::Expired, now)?;
                return Err(StoreError::Expired);
            }
            match (status, target) {
                (Status::Reserved, Status::Committed) => {
                    let affected = conn.execute(
                        "UPDATE reservations SET status='committed', updated_at=?2
                         WHERE id=?1 AND status='reserved'",
                        params![reservation_id, now],
                    )?;
                    debug_assert_eq!(affected, 1);
                    Ok(())
                }
                (Status::Committed, Status::Committed) => Ok(()), // 重复提交幂等
                (Status::Expired, _) => Err(StoreError::Expired),
                (other, _) => Err(StoreError::Conflict {
                    from: other.as_str(),
                    action: target.as_str(),
                }),
            }
        })();
        finish_tx(&conn, result)
    }

    /// 查询用量：先按注入时钟扫到期，再聚合（统计结果即已扣除过期预留）。
    pub fn usage(&self, clock: &dyn Clock, tenant_id: &str) -> Result<Usage, StoreError> {
        let now = clock.now_ms();
        let conn = self.conn.lock().unwrap();
        expire_due_committed(&conn, now)?;
        conn.execute_batch("BEGIN IMMEDIATE")?;
        let result = (|| -> Result<Usage, StoreError> {
            let limits = conn.query_row(
                "SELECT byte_quota, object_quota FROM tenants WHERE tenant_id = ?1",
                params![tenant_id],
                |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?)),
            );
            let (byte_limit, object_limit) = match limits {
                Ok(v) => v,
                Err(rusqlite::Error::QueryReturnedNoRows) => return Err(StoreError::TenantNotFound),
                Err(e) => return Err(StoreError::Db(e)),
            };
            let sums = conn.query_row(
                "SELECT
                    COALESCE(SUM(CASE WHEN status='reserved'  THEN byte_size END),0),
                    COALESCE(SUM(CASE WHEN status='reserved'  THEN object_count END),0),
                    COALESCE(SUM(CASE WHEN status='committed' THEN byte_size END),0),
                    COALESCE(SUM(CASE WHEN status='committed' THEN object_count END),0)
                 FROM reservations WHERE tenant_id = ?1",
                params![tenant_id],
                |r| {
                    Ok((
                        r.get::<_, i64>(0)?,
                        r.get::<_, i64>(1)?,
                        r.get::<_, i64>(2)?,
                        r.get::<_, i64>(3)?,
                    ))
                },
            )?;
            Ok(Usage {
                tenant_id: tenant_id.to_string(),
                byte_limit,
                object_limit,
                reserved_bytes: sums.0,
                reserved_objects: sums.1,
                committed_bytes: sums.2,
                committed_objects: sums.3,
                at_ms: now,
            })
        })();
        finish_tx(&conn, result)
    }

    pub fn get_reservation(&self, id: &str) -> Result<ReservationView, StoreError> {
        let conn = self.conn.lock().unwrap();
        conn.query_row(
            "SELECT id, tenant_id, byte_size, object_count, status, created_at, expires_at, updated_at
             FROM reservations WHERE id = ?1",
            params![id],
            |r| {
                Ok(ReservationView {
                    id: r.get(0)?,
                    tenant_id: r.get(1)?,
                    byte_size: r.get(2)?,
                    object_count: r.get(3)?,
                    status: r.get(4)?,
                    created_at: r.get(5)?,
                    expires_at: r.get(6)?,
                    updated_at: r.get(7)?,
                })
            },
        )
        .map_err(|e| match e {
            rusqlite::Error::QueryReturnedNoRows => StoreError::NotFound,
            other => StoreError::Db(other),
        })
    }

    /// 主动用当前时钟执行一次过期扫描（也可由 reserve/usage 间接触发）。
    pub fn sweep_expired(&self, clock: &dyn Clock) -> Result<usize, StoreError> {
        let now = clock.now_ms();
        let conn = self.conn.lock().unwrap();
        conn.execute_batch("BEGIN IMMEDIATE")?;
        let result = expire_due(&conn, now);
        finish_tx(&conn, result)
    }
}

/// 事务内：把所有 reserved 且到期的行置为 expired，返回受影响行数。
fn expire_due(conn: &Connection, now_ms: i64) -> Result<usize, StoreError> {
    let n = conn.execute(
        "UPDATE reservations
         SET status='expired', updated_at=?2
         WHERE status='reserved' AND expires_at <= ?1",
        params![now_ms, now_ms],
    )?;
    Ok(n)
}

/// 在调用方事务之外先把到期扫描独立提交，保证“到期”这一状态转换一定落盘。
fn expire_due_committed(conn: &Connection, now_ms: i64) -> Result<(), StoreError> {
    conn.execute_batch("BEGIN IMMEDIATE")?;
    let result = expire_due(conn, now_ms);
    finish_tx(conn, result.map(|_| ()))
}

fn set_status(conn: &Connection, id: &str, status: Status, now_ms: i64) -> Result<(), StoreError> {
    conn.execute(
        "UPDATE reservations SET status=?2, updated_at=?3 WHERE id=?1",
        params![id, status.as_str(), now_ms],
    )?;
    Ok(())
}

/// 统一事务收尾：Ok 提交，Err 回滚。
fn finish_tx<T>(conn: &Connection, result: Result<T, StoreError>) -> Result<T, StoreError> {
    match &result {
        Ok(_) => conn.execute_batch("COMMIT")?,
        Err(_) => {
            // 回滚失败会掩盖原始错误，记录但返回原始业务错误。
            let _ = conn.execute_batch("ROLLBACK");
        }
    }
    result
}
