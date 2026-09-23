//! PostgreSQL 持久化层：通道 nonce、租约、fencing token、回执裁决全部在数据库事务中完成，
//! 因此多个协调器进程 / 多个中继进程并发运行时语义仍然成立。

use chrono::{DateTime, Duration, Utc};
use serde_json::Value;
use sqlx::postgres::{PgPool, PgPoolOptions, PgRow};
use sqlx::{FromRow, Row};
use uuid::Uuid;

#[derive(Debug, Clone)]
pub struct Store {
    pub pool: PgPool,
}

/// 一条被领取的租约（含当前代 fencing token）。
#[derive(Debug, Clone, FromRow)]
pub struct Lease {
    pub submission_id: Uuid,
    pub channel_id: String,
    pub nonce: i64,
    pub payload: Value,
    pub fencing_token: i64,
    pub lease_owner: String,
    pub lease_expires_at: DateTime<Utc>,
    pub attempts: i32,
}

#[derive(Debug, Clone, FromRow)]
pub struct Message {
    pub submission_id: Uuid,
    pub channel_id: String,
    pub nonce: i64,
    pub payload: Value,
    pub status: String,
    pub fencing_token: i64,
    pub lease_owner: Option<String>,
    pub lease_expires_at: Option<DateTime<Utc>>,
    pub attempts: i32,
    pub target_tx_id: Option<String>,
    pub result: Option<Value>,
    pub error: Option<String>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
}

/// 回执裁决结果。
#[derive(Debug, PartialEq, Eq)]
pub enum ReceiptVerdict {
    /// token 匹配，状态已落盘。
    Applied,
    /// 旧代中继的回执（token 落后），拒绝，状态不动。
    StaleToken,
    /// 消息已经终态（确认 / 失败），幂等忽略。
    AlreadyFinal,
}

#[derive(Debug)]
pub enum StoreError {
    /// 通道已被某条失败消息阻塞。
    ChannelBlocked(String),
    /// 只能对 failed 消息执行重试。
    NotFailed,
    NotFound,
    Sqlx(String),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::ChannelBlocked(c) => write!(f, "channel {c:?} is blocked"),
            StoreError::NotFailed => write!(f, "message is not in failed state"),
            StoreError::NotFound => write!(f, "message not found"),
            StoreError::Sqlx(e) => write!(f, "database error: {e}"),
        }
    }
}

impl std::error::Error for StoreError {}

impl From<sqlx::Error> for StoreError {
    fn from(e: sqlx::Error) -> Self {
        StoreError::Sqlx(e.to_string())
    }
}

impl Store {
    pub async fn connect(database_url: &str) -> Result<Self, StoreError> {
        let pool = PgPoolOptions::new()
            .max_connections(10)
            .connect(database_url)
            .await?;
        Ok(Self { pool })
    }

    /// 内嵌迁移，启动时真实执行（不需要 sqlx-cli 或编译期 DATABASE_URL）。
    /// 逐条执行：多语句 simple query 在某些驱动/代理下行为不一致，逐条最稳。
    pub async fn migrate(&self) -> Result<(), StoreError> {
        let sql = include_str!("../migrations/0001_init.sql");
        for stmt in sql.split(';') {
            let s = stmt.trim();
            // 跳过空段与纯注释段。
            let meaningful: String = s
                .lines()
                .filter(|l| !l.trim_start().starts_with("--"))
                .collect::<Vec<_>>()
                .join("\n");
            if meaningful.trim().is_empty() {
                continue;
            }
            sqlx::query(s).execute(&self.pool).await.map_err(|e| {
                StoreError::Sqlx(format!("migration statement failed: {e}\nSQL: {s}"))
            })?;
        }
        Ok(())
    }

    /// 投递消息：通道不存在则创建；在事务内领取该通道的下一个连续 nonce。
    /// 通道处于 blocked 时返回 ChannelBlocked —— 失败必须先显式重试才能解除。
    pub async fn enqueue(&self, channel_id: &str, payload: Value) -> Result<Message, StoreError> {
        let mut tx = self.pool.begin().await?;

        sqlx::query(
            "INSERT INTO channels (channel_id) VALUES ($1) ON CONFLICT (channel_id) DO NOTHING",
        )
        .bind(channel_id)
        .execute(&mut *tx)
        .await?;

        let blocked: bool = sqlx::query_scalar("SELECT blocked FROM channels WHERE channel_id=$1")
            .bind(channel_id)
            .fetch_one(&mut *tx)
            .await?;
        if blocked {
            return Err(StoreError::ChannelBlocked(channel_id.to_string()));
        }

        let nonce: i64 =
            sqlx::query_scalar("UPDATE channels SET next_nonce = next_nonce + 1 WHERE channel_id=$1 RETURNING next_nonce - 1")
                .bind(channel_id)
                .fetch_one(&mut *tx)
                .await?;

        let id = Uuid::new_v4();
        let row = sqlx::query(
            "INSERT INTO messages (submission_id, channel_id, nonce, payload)
             VALUES ($1, $2, $3, $4)
             RETURNING *",
        )
        .bind(id)
        .bind(channel_id)
        .bind(nonce)
        .bind(payload)
        .fetch_one(&mut *tx)
        .await?;
        tx.commit().await?;
        Ok(Message::from_row(&row)?)
    }

    /// 领取一个可执行的队头消息租约。
    ///
    /// 选取规则（单条 SQL + 行锁，跨进程安全）：
    /// - 通道未 blocked；
    /// - 消息是 pending，或 leased 但租约已过期（超时可重领）；
    /// - 它前面没有同通道未完成的消息（每通道严格按 nonce 顺序）；
    /// - `FOR UPDATE SKIP LOCKED`：被别的领取事务持有的行直接跳过，
    ///   于是同一条消息同一时刻只会被一个中继领走。
    ///
    /// 领取成功后全局 fencing token +1 并写入该消息；旧代 token 从此失效。
    pub async fn acquire(
        &self,
        relay_id: &str,
        lease_ttl: Duration,
    ) -> Result<Option<Lease>, StoreError> {
        let mut tx = self.pool.begin().await?;

        let candidate: Option<PgRow> = sqlx::query(
            "SELECT m.*
             FROM messages m
             JOIN channels c ON c.channel_id = m.channel_id
             WHERE NOT c.blocked
               AND m.status IN ('pending', 'leased')
               AND (m.status = 'pending' OR m.lease_expires_at < now())
               AND NOT EXISTS (
                   SELECT 1 FROM messages h
                   WHERE h.channel_id = m.channel_id
                     AND h.nonce < m.nonce
                     AND h.status IN ('pending', 'leased')
               )
             ORDER BY m.created_at, m.channel_id, m.nonce
             LIMIT 1
             FOR UPDATE OF m SKIP LOCKED",
        )
        .fetch_optional(&mut *tx)
        .await?;

        let Some(row) = candidate else {
            tx.rollback().await?;
            return Ok(None);
        };

        // 全局单调递增 fencing token。
        let token: i64 = sqlx::query_scalar(
            "UPDATE fencing_seq SET value = value + 1 WHERE id = 1 RETURNING value",
        )
        .fetch_one(&mut *tx)
        .await?;

        let id: Uuid = row.try_get("submission_id")?;
        let expires = Utc::now() + lease_ttl;
        let updated: PgRow = sqlx::query(
            "UPDATE messages
             SET status='leased', fencing_token=$2, lease_owner=$3, lease_expires_at=$4,
                 attempts=attempts+1, updated_at=now()
             WHERE submission_id=$1
             RETURNING *",
        )
        .bind(id)
        .bind(token)
        .bind(relay_id)
        .bind(expires)
        .fetch_one(&mut *tx)
        .await?;
        tx.commit().await?;

        Ok(Some(lease_from_row(&updated)?))
    }

    /// 中继提交回执。`token` 必须等于该消息当前代的 fencing token，
    /// 否则判定为旧代中继的迟到回执并拒绝（状态绝不回退）。
    pub async fn submit_receipt(
        &self,
        submission_id: Uuid,
        token: i64,
        tx_id: Option<&str>,
        result: Option<Value>,
        error: Option<&str>,
    ) -> Result<ReceiptVerdict, StoreError> {
        let mut tx = self.pool.begin().await?;

        let row = sqlx::query("SELECT status, fencing_token FROM messages WHERE submission_id=$1 FOR UPDATE")
            .bind(submission_id)
            .fetch_optional(&mut *tx)
            .await?;
        let Some(row) = row else {
            tx.rollback().await?;
            return Err(StoreError::NotFound);
        };
        let status: String = row.try_get("status")?;
        let current_token: i64 = row.try_get("fencing_token")?;

        if status == "confirmed" || status == "failed" {
            tx.rollback().await?;
            return Ok(ReceiptVerdict::AlreadyFinal);
        }
        if current_token != token {
            // 旧代中继：租约早已过期并被新一代领走，回执丢弃。
            tx.rollback().await?;
            return Ok(ReceiptVerdict::StaleToken);
        }

        match (tx_id, error) {
            (Some(tx_id), _) => {
                sqlx::query(
                    "UPDATE messages
                     SET status='confirmed', target_tx_id=$2, result=$3,
                         lease_owner=NULL, lease_expires_at=NULL, updated_at=now()
                     WHERE submission_id=$1",
                )
                .bind(submission_id)
                .bind(tx_id)
                .bind(result)
                .execute(&mut *tx)
                .await?;
            }
            (None, Some(err)) => {
                // 失败：消息标记 failed 并阻塞本通道后续；其他通道不受影响。
                let channel: String =
                    sqlx::query_scalar("SELECT channel_id FROM messages WHERE submission_id=$1")
                        .bind(submission_id)
                        .fetch_one(&mut *tx)
                        .await?;
                sqlx::query(
                    "UPDATE messages
                     SET status='failed', error=$2, lease_owner=NULL,
                         lease_expires_at=NULL, updated_at=now()
                     WHERE submission_id=$1",
                )
                .bind(submission_id)
                .bind(err)
                .execute(&mut *tx)
                .await?;
                sqlx::query(
                    "UPDATE channels SET blocked=TRUE, blocked_reason=$2 WHERE channel_id=$1",
                )
                .bind(channel)
                .bind(err)
                .execute(&mut *tx)
                .await?;
            }
            (None, None) => {
                tx.rollback().await?;
                return Err(StoreError::Sqlx(
                    "receipt must carry either tx_id or error".into(),
                ));
            }
        }
        tx.commit().await?;
        Ok(ReceiptVerdict::Applied)
    }

    /// 把 failed 的消息重新置为队头 pending 并解除通道阻塞。
    pub async fn retry(&self, submission_id: Uuid) -> Result<Message, StoreError> {
        let mut tx = self.pool.begin().await?;
        let row = sqlx::query("SELECT * FROM messages WHERE submission_id=$1 FOR UPDATE")
            .bind(submission_id)
            .fetch_optional(&mut *tx)
            .await?;
        let Some(row) = row else {
            tx.rollback().await?;
            return Err(StoreError::NotFound);
        };
        let status: String = row.try_get("status")?;
        if status != "failed" {
            tx.rollback().await?;
            return Err(StoreError::NotFailed);
        }
        let channel: String = row.try_get("channel_id")?;

        let updated = sqlx::query(
            "UPDATE messages
             SET status='pending', error=NULL, lease_owner=NULL, lease_expires_at=NULL,
                 target_tx_id=NULL, result=NULL, updated_at=now()
             WHERE submission_id=$1
             RETURNING *",
        )
        .bind(submission_id)
        .fetch_one(&mut *tx)
        .await?;
        sqlx::query(
            "UPDATE channels SET blocked=FALSE, blocked_reason=NULL WHERE channel_id=$1",
        )
        .bind(channel)
        .execute(&mut *tx)
        .await?;
        tx.commit().await?;
        Ok(Message::from_row(&updated)?)
    }

    pub async fn get(&self, submission_id: Uuid) -> Result<Message, StoreError> {
        let row = sqlx::query("SELECT * FROM messages WHERE submission_id=$1")
            .bind(submission_id)
            .fetch_optional(&self.pool)
            .await?
            .ok_or(StoreError::NotFound)?;
        Ok(Message::from_row(&row)?)
    }

    /// 最近一条尚在租约中的队头消息何时到期；没有则返回 None。
    /// 协调器用它告诉中继"下次值得醒来的时刻"，避免忙等。
    pub async fn next_lease_due(&self) -> Result<Option<DateTime<Utc>>, StoreError> {
        let due: Option<DateTime<Utc>> = sqlx::query_scalar(
            "SELECT min(m.lease_expires_at)
             FROM messages m
             JOIN channels c ON c.channel_id = m.channel_id
             WHERE NOT c.blocked
               AND m.status = 'leased'
               AND NOT EXISTS (
                   SELECT 1 FROM messages h
                   WHERE h.channel_id = m.channel_id
                     AND h.nonce < m.nonce
                     AND h.status IN ('pending', 'leased')
               )",
        )
        .fetch_optional(&self.pool)
        .await?
        .flatten();
        Ok(due)
    }
}

fn lease_from_row(row: &PgRow) -> Result<Lease, sqlx::Error> {
    Ok(Lease {
        submission_id: row.try_get("submission_id")?,
        channel_id: row.try_get("channel_id")?,
        nonce: row.try_get("nonce")?,
        payload: row.try_get("payload")?,
        fencing_token: row.try_get("fencing_token")?,
        lease_owner: row.try_get("lease_owner")?,
        lease_expires_at: row.try_get("lease_expires_at")?,
        attempts: row.try_get("attempts")?,
    })
}

