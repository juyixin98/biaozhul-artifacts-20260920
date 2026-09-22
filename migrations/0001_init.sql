-- 员工活动异常检测：初始化迁移（版本 0001）。
-- 所有时间列以 UTC DATETIME(6) 存储；应用层负责按员工时区换算本地时间。

CREATE TABLE IF NOT EXISTS departments (
    id          CHAR(26) NOT NULL,
    name        VARCHAR(100) NOT NULL,
    created_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at  DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uk_departments_name (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS employees (
    id             CHAR(26) NOT NULL,
    department_id  CHAR(26) NOT NULL,
    name           VARCHAR(100) NOT NULL,
    email          VARCHAR(190) NOT NULL,
    timezone       VARCHAR(64) NOT NULL DEFAULT 'UTC',
    is_active      TINYINT(1) NOT NULL DEFAULT 1,
    created_at     DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at     DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uk_employees_email (email),
    KEY idx_employees_department (department_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS users (
    id              CHAR(26) NOT NULL,
    username        VARCHAR(64) NOT NULL,
    password_hash   VARCHAR(255) NOT NULL,
    role            ENUM('analyst','admin') NOT NULL,
    is_active       TINYINT(1) NOT NULL DEFAULT 1,
    created_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at      DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uk_users_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 分析员可见的部门分配；admin 默认可见全部部门，不使用本表。
CREATE TABLE IF NOT EXISTS user_departments (
    user_id        CHAR(26) NOT NULL,
    department_id  CHAR(26) NOT NULL,
    created_at     DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (user_id, department_id),
    KEY idx_ud_department (department_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 原始事件。event_id 为上报方提供的幂等键；content_hash 用于“同 ID 不同内容”冲突检测。
CREATE TABLE IF NOT EXISTS events (
    id                       CHAR(26) NOT NULL,
    event_id                 VARCHAR(190) NOT NULL,
    event_type               ENUM('login','file_download','usb') NOT NULL,
    employee_id              CHAR(26) NOT NULL,
    occurred_at              DATETIME(6) NOT NULL,
    received_at              DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    metadata                 JSON NULL,
    content_hash             CHAR(64) NOT NULL,
    detected_at              DATETIME(6) NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_events_event_id (event_id),
    KEY idx_events_employee_time (employee_id, event_type, occurred_at),
    KEY idx_events_detected (detected_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 规则带版本：同一 rule_key 同时只有一个 is_active=1 的版本，
-- 告警落库时固化 rule_version，保留“告警依据 + 规则版本”。
CREATE TABLE IF NOT EXISTS detection_rules (
    id           CHAR(26) NOT NULL,
    rule_key     VARCHAR(40) NOT NULL,
    version      INT NOT NULL,
    name         VARCHAR(120) NOT NULL,
    description  VARCHAR(500) NOT NULL DEFAULT '',
    params       JSON NOT NULL,
    is_active    TINYINT(1) NOT NULL DEFAULT 0,
    created_by   CHAR(26) NULL,
    created_at    DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uk_rules_key_version (rule_key, version),
    KEY idx_rules_active (rule_key, is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS alerts (
    id             CHAR(26) NOT NULL,
    rule_key       VARCHAR(40) NOT NULL,
    rule_version   INT NOT NULL,
    employee_id    CHAR(26) NOT NULL,
    department_id  CHAR(26) NOT NULL,
    severity       ENUM('low','medium','high') NOT NULL,
    status         ENUM('new','investigating','escalated','resolved','false_positive') NOT NULL DEFAULT 'new',
    title          VARCHAR(200) NOT NULL,
    summary        VARCHAR(1000) NOT NULL DEFAULT '',
    -- dedup_key 唯一：补算/重复调度重算同一窗口时幂等 upsert，避免重复告警。
    dedup_key      VARCHAR(190) NOT NULL,
    evidence       JSON NOT NULL,
    window_start   DATETIME(6) NULL,
    window_end     DATETIME(6) NULL,
    first_seen_at  DATETIME(6) NOT NULL,
    last_event_at  DATETIME(6) NOT NULL,
    acknowledged_at DATETIME(6) NULL,
    resolved_at    DATETIME(6) NULL,
    assigned_to    CHAR(26) NULL,
    created_by     CHAR(26) NULL,
    created_at     DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at     DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uk_alerts_dedup (dedup_key),
    KEY idx_alerts_dept_status (department_id, status),
    KEY idx_alerts_employee (employee_id),
    KEY idx_alerts_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS audit_logs (
    id           CHAR(26) NOT NULL,
    actor_id     CHAR(26) NULL,
    actor_name   VARCHAR(64) NOT NULL DEFAULT '',
    action       VARCHAR(60) NOT NULL,
    target_type  VARCHAR(40) NOT NULL DEFAULT '',
    target_id    VARCHAR(190) NOT NULL DEFAULT '',
    detail       JSON NULL,
    created_at   DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    KEY idx_audit_actor_time (actor_id, created_at),
    KEY idx_audit_target (target_type, target_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- 调度单实例锁：心跳行锁，保证升级/补算任务在任意时刻只有一个执行者。
CREATE TABLE IF NOT EXISTS scheduler_locks (
    name         VARCHAR(60) NOT NULL,
    locked_by    VARCHAR(120) NOT NULL,
    locked_at    DATETIME(6) NOT NULL,
    expires_at   DATETIME(6) NOT NULL,
    heartbeat_at DATETIME(6) NOT NULL,
    PRIMARY KEY (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
