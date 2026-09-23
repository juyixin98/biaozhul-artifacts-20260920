//! 数据访问层。所有租约代次（fencing token）与 nonce 顺序保证都落在 SQL 事务里：
//! - 领取事务先锁通道行、再锁该通道所有“未越过”的提交，检查队头是否可领；
//! - nonce 仅在首次领取时分配（COALESCE 保证重领不换号）；
//! - 任何带 fence 的更新都必须同时匹配 (id, fence, leased_by)，旧代次回执 0 行命中。

use serde_json::Value;
use sqlx::postgres::PgPoolOptions;
use sqlx::{FromRow, PgPool};
use sqlx::types::Json;
use chrono::{DateTime, Utc};

#[derive(Debug, Clone, FromRow)]
pub struct Submission {
    pub id: String,
    pub channel_id: String,
    pub seq: i64,
    pub nonce: Option<i64>,
    pub payload: Json<Value>,
    pub status: String,
    pub fence: i32,
    pub leased_by: Option<String>,
    pub leased_until: Option<DateTime<Utc>>,
    pub attempts: i32,
    pub tx_hash: Option<String>,
    pub last_error: Option<String>,
}

#[derive(Debug, FromRow)]
struct ChannelHead {
    status: String,
    expired: bool,
}

pub async fn connect(database_url: &str) -> anyhow::Result<PgPool> {
    let pool = PgPoolOptions::new()
        .max_connections(10)
        .connect(database_url)
        .await?;
    Ok(pool)
}

pub async fn run_migrations(pool: &PgPool) -> anyhow::Result<()> {
    sqlx::migrate!("./migrations").run(pool).await?;
    Ok(())
}

async fn audit(
    pool: &PgPool,
    submission_id: &str,
    relay_id: &str,
    fence: i32,
    nonce: Option<i64>,
    event: &str,
    detail: Option<&str>,
) {
    let _ = sqlx::query(
        "INSERT INTO delivery_attempts (submission_id, relay_id, fence, nonce, event, detail)
         VALUES ($1,$2,$3,$4,$5,$6)",
    )
    .bind(submission_id)
    .bind(relay_id)
    .bind(fence)
    .bind(nonce)
    .bind(event)
    .bind(detail)
    .execute(pool)
    .await;
}

const SELECT_COLS: &str = "id, channel_id, seq, nonce, payload, status, fence, \
     leased_by, leased_until, attempts, tx_hash, last_error";

/// 入队：幂等（commit_id 冲突不覆盖），同批按输入顺序占用通道 seq。
pub async fn enqueue(
    pool: &PgPool,
    channel_id: &str,
    commits: &[(String, Value)],
) -> anyhow::Result<Vec<Submission>> {
    let mut tx = pool.begin().await?;

    sqlx::query("INSERT INTO channels (id) VALUES ($1) ON CONFLICT (id) DO NOTHING")
        .bind(channel_id)
        .execute(&mut *tx)
        .await?;
    // 锁通道，保证并发入队的 seq 不交错
    sqlx::query("SELECT id FROM channels WHERE id = $1 FOR UPDATE")
        .bind(channel_id)
        .execute(&mut *tx)
        .await?;

    for (id, payload) in commits {
        let seq: (i64,) = sqlx::query_as(
            "SELECT COALESCE(MAX(seq) + 1, 0) FROM submissions WHERE channel_id = $1",
        )
        .bind(channel_id)
        .fetch_one(&mut *tx)
        .await?;
        sqlx::query(
            "INSERT INTO submissions (id, channel_id, seq, payload)
             VALUES ($1,$2,$3,$4) ON CONFLICT (id) DO NOTHING",
        )
        .bind(id)
        .bind(channel_id)
        .bind(seq.0)
        .bind(Json(payload))
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;

    let ids: Vec<String> = commits.iter().map(|(id, _)| id.clone()).collect();
    let rows = sqlx::query_as::<_, Submission>(&format!(
        "SELECT {SELECT_COLS} FROM submissions WHERE id = ANY($1) ORDER BY channel_id, seq"
    ))
    .bind(&ids)
    .fetch_all(pool)
    .await?;
    Ok(rows)
}

pub async fn get(pool: &PgPool, id: &str) -> anyhow::Result<Option<Submission>> {
    let row = sqlx::query_as::<_, Submission>(&format!(
        "SELECT {SELECT_COLS} FROM submissions WHERE id = $1"
    ))
    .bind(id)
    .fetch_optional(pool)
    .await?;
    Ok(row)
}

pub async fn list(pool: &PgPool, channel_id: &str, limit: i64) -> anyhow::Result<Vec<Submission>> {
    let rows = sqlx::query_as::<_, Submission>(&format!(
        "SELECT {SELECT_COLS} FROM submissions WHERE channel_id = $1 ORDER BY seq LIMIT $2"
    ))
    .bind(channel_id)
    .bind(limit)
    .fetch_all(pool)
    .await?;
    Ok(rows)
}

/// 领取一条可提交的消息。
///
/// 规则：通道之间互不影响（其他通道即使被失败阻塞也照常领取）；
/// 同一通道必须按 seq 连续推进——只要存在更早的未确认提交（活跃租约 / 失败），
/// 后续全部阻塞；过期租约视为可重领，重领时 fence+1。
pub async fn claim(
    pool: &PgPool,
    relay_id: &str,
    lease_secs: i32,
) -> anyhow::Result<Option<Submission>> {
    let mut tx = pool.begin().await?;

    // 候选：存在未确认提交（含 failed——它也要作为队头参与阻塞判断）的通道。
    let candidates = sqlx::query_as::<_, (String,)>(
        "SELECT DISTINCT channel_id
         FROM submissions
         WHERE status <> 'confirmed'
         ORDER BY channel_id",
    )
    .fetch_all(&mut *tx)
    .await?;

    for (channel_id,) in candidates {
        // 锁通道（保护 next_nonce / 通道推进顺序）
        let next_nonce: (i64,) =
            sqlx::query_as("SELECT next_nonce FROM channels WHERE id = $1 FOR UPDATE")
                .bind(&channel_id)
                .fetch_one(&mut *tx)
                .await?;

        // 队头 = 该通道 seq 最小的未确认提交（含 failed）。
        // SKIP LOCKED：队头正被其他事务领取时，本中继直接转去其他通道。
        let head = sqlx::query_as::<_, ChannelHead>(
            "SELECT status, (leased_until IS NOT NULL AND leased_until < now()) AS expired
             FROM submissions
             WHERE channel_id = $1 AND status <> 'confirmed'
             ORDER BY seq
             LIMIT 1
             FOR UPDATE SKIP LOCKED",
        )
        .bind(&channel_id)
        .fetch_optional(&mut *tx)
        .await?;

        let Some(head) = head else { continue };
        let claimable = match head.status.as_str() {
            "failed" => false,           // 失败阻塞本通道，等待人工 requeue
            "pending" => true,
            "leased" | "delivered" => head.expired,
            _ => false,
        };
        if !claimable {
            // 失败阻塞，或队头仍被有效租约持有；跳过本通道（不影响其他通道）
            continue;
        }

        // 取被锁定的队头 id
        let head_id: (String,) = sqlx::query_as(
            "SELECT id FROM submissions
             WHERE channel_id = $1 AND status <> 'confirmed'
             ORDER BY seq LIMIT 1",
        )
        .bind(&channel_id)
        .fetch_one(&mut *tx)
        .await?;

        let updated = sqlx::query_as::<_, Submission>(&format!(
            "UPDATE submissions SET
                status = 'leased',
                fence = fence + 1,
                leased_by = $2,
                leased_until = now() + make_interval(secs => $4),
                nonce = COALESCE(nonce, $3),
                attempts = attempts + 1,
                updated_at = now()
             WHERE id = $1
             RETURNING {SELECT_COLS}"
        ))
        .bind(&head_id.0)
        .bind(relay_id)
        .bind(next_nonce.0) // $3：仅当 nonce 为 NULL（首次领取）时使用
        .bind(lease_secs as f64) // $4：make_interval(secs => double precision)
        .fetch_optional(&mut *tx)
        .await?;

        if let Some(row) = updated {
            // nonce 实际被消费：仅在首次分配时推进通道 next_nonce
            if row.nonce == Some(next_nonce.0) {
                sqlx::query("UPDATE channels SET next_nonce = next_nonce + 1 WHERE id = $1")
                    .bind(&channel_id)
                    .execute(&mut *tx)
                    .await?;
            }
            tx.commit().await?;
            audit(
                pool,
                &row.id,
                relay_id,
                row.fence,
                row.nonce,
                "claim",
                Some(&format!("ttl={lease_secs}s")),
            )
            .await;
            return Ok(Some(row));
        }
    }

    tx.commit().await?;
    Ok(None)
}

/// 心跳续租。fence 或 relay 不匹配即拒绝（旧代次无法续命）。
pub async fn heartbeat(
    pool: &PgPool,
    id: &str,
    fence: i32,
    relay_id: &str,
    lease_secs: i32,
) -> anyhow::Result<bool> {
    let done = sqlx::query(
        "UPDATE submissions
         SET leased_until = now() + make_interval(secs => $4), updated_at = now()
         WHERE id = $1 AND fence = $2 AND leased_by = $3",
    )
    .bind(id)
    .bind(fence)
    .bind(relay_id)
    .bind(lease_secs as f64)
    .execute(pool)
    .await?
    .rows_affected();
    Ok(done == 1)
}

/// 目标已受理（mempool/accepted）。仅当前代次、当前 relay 可写；幂等。
pub async fn mark_delivered(
    pool: &PgPool,
    id: &str,
    fence: i32,
    relay_id: &str,
) -> anyhow::Result<bool> {
    // RETURNING 旧状态：用事务快照前的行值判断是否真实跃迁
    let row = sqlx::query_as::<_, (String,)>(
        "UPDATE submissions s SET status = 'delivered', updated_at = now()
         FROM (SELECT id, status AS old_status FROM submissions WHERE id = $1 FOR UPDATE) old
         WHERE s.id = old.id AND s.fence = $2 AND s.leased_by = $3
           AND s.status IN ('leased','delivered')
         RETURNING old.old_status",
    )
    .bind(id)
    .bind(fence)
    .bind(relay_id)
    .fetch_optional(pool)
    .await?;
    match row {
        Some((old_status,)) if old_status == "leased" => {
            audit(pool, id, relay_id, fence, None, "delivered", None).await;
            Ok(true)
        }
        Some(_) => Ok(true), // 已是 delivered 的幂等重入
        None => Ok(false),
    }
}

/// 最终确认（带交易哈希）。旧代次回执 0 行命中 -> 调用方上报 stale。
pub async fn complete(
    pool: &PgPool,
    id: &str,
    fence: i32,
    relay_id: &str,
    tx_hash: &str,
) -> anyhow::Result<bool> {
    let nonce: Option<(Option<i64>,)> = sqlx::query_as(
        "SELECT nonce FROM submissions WHERE id = $1 AND fence = $2 AND leased_by = $3",
    )
    .bind(id)
    .bind(fence)
    .bind(relay_id)
    .fetch_optional(pool)
    .await?;
    let Some((nonce,)) = nonce else {
        return Ok(false);
    };

    let done = sqlx::query(
        "UPDATE submissions
         SET status = 'confirmed', tx_hash = $4, leased_until = NULL, updated_at = now()
         WHERE id = $1 AND fence = $2 AND leased_by = $3
           AND status IN ('leased','delivered')",
    )
    .bind(id)
    .bind(fence)
    .bind(relay_id)
    .bind(tx_hash)
    .execute(pool)
    .await?
    .rows_affected();

    if done == 1 {
        audit(pool, id, relay_id, fence, nonce, "confirm", Some(tx_hash)).await;
    }
    Ok(done == 1)
}

/// 可重试失败：归还租约，回到 pending 等待（可能被其他中继领取）。
pub async fn report_retryable(
    pool: &PgPool,
    id: &str,
    fence: i32,
    relay_id: &str,
    error: &str,
) -> anyhow::Result<bool> {
    let done = sqlx::query(
        "UPDATE submissions
         SET status = 'pending', leased_by = NULL, leased_until = NULL,
             last_error = $4, updated_at = now()
         WHERE id = $1 AND fence = $2 AND leased_by = $3
           AND status IN ('leased','delivered')",
    )
    .bind(id)
    .bind(fence)
    .bind(relay_id)
    .bind(error)
    .execute(pool)
    .await?
    .rows_affected();
    if done == 1 {
        audit(pool, id, relay_id, fence, None, "retryable", Some(error)).await;
    }
    Ok(done == 1)
}

/// 致命失败：置 failed，阻塞本通道队头（后续不再领取），直到人工 requeue。
pub async fn report_fatal(
    pool: &PgPool,
    id: &str,
    fence: i32,
    relay_id: &str,
    error: &str,
) -> anyhow::Result<bool> {
    let done = sqlx::query(
        "UPDATE submissions
         SET status = 'failed', leased_by = NULL, leased_until = NULL,
             last_error = $4, updated_at = now()
         WHERE id = $1 AND fence = $2 AND leased_by = $3
           AND status IN ('leased','delivered')",
    )
    .bind(id)
    .bind(fence)
    .bind(relay_id)
    .bind(error)
    .execute(pool)
    .await?
    .rows_affected();
    if done == 1 {
        audit(pool, id, relay_id, fence, None, "fatal", Some(error)).await;
    }
    Ok(done == 1)
}

/// 人工解除失败阻塞：failed -> pending，并 bump fence 使任何在途旧代次作废。
pub async fn requeue(pool: &PgPool, id: &str) -> anyhow::Result<Option<i32>> {
    let row = sqlx::query_as::<_, (i32,)>(
        "UPDATE submissions
         SET status = 'pending', leased_by = NULL, leased_until = NULL,
             fence = fence + 1, last_error = NULL, updated_at = now()
         WHERE id = $1 AND status = 'failed'
         RETURNING fence",
    )
    .bind(id)
    .fetch_optional(pool)
    .await?;
    Ok(row.map(|(f,)| f))
}
