-- 0002_rules.sql: versioned sensitive-word rules
CREATE TABLE rule_versions (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version    INTEGER NOT NULL UNIQUE,
    note       TEXT NOT NULL DEFAULT '',
    active     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    activated_at TIMESTAMPTZ
);

CREATE TABLE sensitive_words (
    rule_version_id BIGINT NOT NULL REFERENCES rule_versions(id) ON DELETE CASCADE,
    word            TEXT NOT NULL,
    PRIMARY KEY (rule_version_id, word)
);
