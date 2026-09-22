using Dapper;
using Npgsql;

namespace VideoForge.Api.Data;

/// <summary>
/// Owns the PostgreSQL schema. Idempotent: safe to run on every startup.
/// </summary>
public sealed class Database
{
    private readonly NpgsqlDataSource _dataSource;

    public Database(NpgsqlDataSource dataSource) => _dataSource = dataSource;

    public async Task InitializeAsync(CancellationToken ct = default)
    {
        await using var conn = await _dataSource.OpenConnectionAsync(ct);
        await using var cmd = new NpgsqlCommand(SchemaSql, conn);
        await cmd.ExecuteNonQueryAsync(ct);
    }

    // All queue-state transitions live in conditional UPDATEs so that every
    // terminal state (completed / failed / cancelled) is reached by at most
    // one competing actor.
    private const string SchemaSql = """
        CREATE TABLE IF NOT EXISTS materials (
            id              UUID PRIMARY KEY,
            filename        TEXT NOT NULL,
            stored_path     TEXT NOT NULL UNIQUE,
            content_type    TEXT NOT NULL,
            media_kind      TEXT NOT NULL CHECK (media_kind IN ('image','video')),
            size_bytes      BIGINT NOT NULL CHECK (size_bytes > 0),
            width           INTEGER,
            height          INTEGER,
            duration_ms     BIGINT,
            checksum_sha256 TEXT NOT NULL,
            tags            TEXT[] NOT NULL DEFAULT '{}',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS materials_tags_gin ON materials USING gin(tags);

        CREATE TABLE IF NOT EXISTS projects (
            id                 UUID PRIMARY KEY,
            name               TEXT NOT NULL,
            target_duration_ms INTEGER NOT NULL CHECK (target_duration_ms BETWEEN 5000 AND 60000),
            transition         TEXT NOT NULL CHECK (transition IN ('cut','fade')),
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS scenes (
            project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            position    INTEGER NOT NULL,
            description TEXT NOT NULL CHECK (char_length(description) <= 500),
            keywords    TEXT[] NOT NULL DEFAULT '{}',
            PRIMARY KEY (project_id, position)
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id                 UUID PRIMARY KEY,
            project_id         UUID NOT NULL REFERENCES projects(id),
            submission_key     TEXT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'queued'
                                 CHECK (status IN ('queued','processing','completed','failed','cancelled')),
            priority           INTEGER NOT NULL DEFAULT 0,
            cancel_requested   BOOLEAN NOT NULL DEFAULT false,
            locked_by          TEXT,
            locked_at          TIMESTAMPTZ,
            heartbeat_at       TIMESTAMPTZ,
            started_at         TIMESTAMPTZ,
            finished_at        TIMESTAMPTZ,
            publish_path       TEXT,
            output_size        BIGINT,
            output_duration_ms BIGINT,
            error              TEXT,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        -- A submission key identifies one logical submission forever: replaying
        -- it returns the original job instead of creating a duplicate.
        CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_submission_key ON jobs(submission_key);
        CREATE INDEX IF NOT EXISTS ix_jobs_queue ON jobs(status, priority DESC, created_at ASC, id ASC)
            WHERE status = 'queued';

        CREATE TABLE IF NOT EXISTS job_attempts (
            id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            job_id            UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            attempt_no        INTEGER NOT NULL,
            worker_id         TEXT NOT NULL,
            started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at       TIMESTAMPTZ,
            outcome           TEXT CHECK (outcome IN ('succeeded','failed','cancelled','interrupted')),
            error             TEXT,
            ffmpeg_log        TEXT,
            material_manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (job_id, attempt_no)
        );

        -- Atomically pick one queued job. FOR UPDATE SKIP LOCKED lets several
        -- workers (or several API replicas) fight over the queue without any
        -- job being claimed twice.
        CREATE OR REPLACE FUNCTION claim_next_job(p_worker TEXT)
        RETURNS SETOF jobs AS $$
        BEGIN
          RETURN QUERY
          WITH picked AS (
            SELECT id
              FROM jobs
             WHERE status = 'queued'
             ORDER BY priority DESC, created_at ASC, id ASC
             LIMIT 1
             FOR UPDATE SKIP LOCKED
          ),
          updated AS (
            UPDATE jobs j
               SET status       = 'processing',
                   locked_by    = p_worker,
                   locked_at    = now(),
                   heartbeat_at = now(),
                   started_at   = COALESCE(j.started_at, now())
              FROM picked
             WHERE j.id = picked.id
            RETURNING j.*
          )
          SELECT * FROM updated;
        END;
        $$ LANGUAGE plpgsql;
        """;
}
