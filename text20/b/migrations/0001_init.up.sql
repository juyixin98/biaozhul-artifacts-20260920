-- SignalBoard initial schema
-- btree_gist enables "=" operator classes for scalar columns in EXCLUDE constraints.
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- Money is stored as integer cents everywhere.
-- All timestamps are timestamptz; daily sales are bucketed in the store's local timezone.

CREATE TABLE stores (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        TEXT NOT NULL,
    timezone    TEXT NOT NULL, -- IANA zone, e.g. "America/New_York"
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE screens (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    store_id     BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    token_hash   TEXT NOT NULL UNIQUE, -- sha256 hex of the opaque token
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_screens_store ON screens(store_id);

-- ---------------------------------------------------------------------------
-- Draft / catalog side
-- ---------------------------------------------------------------------------

CREATE TABLE dishes (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    store_id    BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    base_price  BIGINT NOT NULL CHECK (base_price >= 0), -- cents
    is_active   BOOLEAN NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (store_id, name)
);
CREATE INDEX idx_dishes_store ON dishes(store_id);

-- Draft of the menu line-up (ordered list of active dishes shown on screens).
-- Only one row per store; updated freely, never visible to screens.
CREATE TABLE menu_drafts (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    store_id    BIGINT NOT NULL UNIQUE REFERENCES stores(id) ON DELETE CASCADE,
    -- draft version counter, bumped on every edit; used for conditional requests
    -- on the management side and as the optimistic-lock target for publish.
    version     BIGINT NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE draft_items (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    draft_id    BIGINT NOT NULL REFERENCES menu_drafts(id) ON DELETE CASCADE,
    dish_id     BIGINT NOT NULL REFERENCES dishes(id) ON DELETE CASCADE,
    position    INT NOT NULL,
    -- draft price override; falls back to dishes.base_price when NULL
    price       BIGINT CHECK (price IS NULL OR price >= 0),
    UNIQUE (draft_id, dish_id),
    UNIQUE (draft_id, position)
);
CREATE INDEX idx_draft_items_draft ON draft_items(draft_id);

-- ---------------------------------------------------------------------------
-- Published side (immutable)
-- ---------------------------------------------------------------------------

CREATE TABLE menu_versions (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    store_id     BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    version      BIGINT NOT NULL,          -- per-store monotonically increasing
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_by TEXT NOT NULL DEFAULT '',
    UNIQUE (store_id, version)
);
CREATE INDEX idx_menu_versions_store ON menu_versions(store_id);

CREATE TABLE menu_version_items (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version_id BIGINT NOT NULL REFERENCES menu_versions(id) ON DELETE CASCADE,
    dish_id    BIGINT NOT NULL,
    dish_name  TEXT NOT NULL,               -- snapshotted, immutable
    position   INT NOT NULL,
    price      BIGINT NOT NULL CHECK (price >= 0), -- effective price at publish time
    UNIQUE (version_id, dish_id),
    UNIQUE (version_id, position)
);
CREATE INDEX idx_mvi_version ON menu_version_items(version_id);
CREATE INDEX idx_mvi_dish ON menu_version_items(dish_id);

-- Temporary price schedules attached to an immutable published version.
-- Half-open [starts_at, ends_at); overlap is impossible per dish per version:
-- enforced in the database with an exclusion constraint so that concurrent
-- writers cannot bypass application-level checks.
CREATE TABLE temp_prices (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    version_id BIGINT NOT NULL REFERENCES menu_versions(id) ON DELETE CASCADE,
    dish_id    BIGINT NOT NULL REFERENCES dishes(id) ON DELETE CASCADE,
    price      BIGINT NOT NULL CHECK (price >= 0),
    starts_at  TIMESTAMPTZ NOT NULL,
    ends_at    TIMESTAMPTZ NOT NULL,
    CHECK (starts_at < ends_at),
    CONSTRAINT temp_prices_no_overlap EXCLUDE USING gist (
        version_id WITH =,
        dish_id WITH =,
        tstzrange(starts_at, ends_at, '[)') WITH &&
    )
);
CREATE INDEX idx_temp_prices_lookup ON temp_prices(version_id, dish_id);

-- ---------------------------------------------------------------------------
-- Sales / sellout side
-- ---------------------------------------------------------------------------

-- Ingested sales events. (store_id, event_id) is the idempotency key.
CREATE TABLE sales_events (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    store_id     BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    event_id     TEXT NOT NULL,
    dish_id      BIGINT NOT NULL,
    quantity     INT NOT NULL CHECK (quantity > 0),
    occurred_at  TIMESTAMPTZ NOT NULL,  -- when the sale happened (may be late)
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (store_id, event_id)
);

-- Daily sales totals keyed by store-local calendar date.
CREATE TABLE daily_sales (
    id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    store_id  BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    sales_day DATE NOT NULL,
    dish_id   BIGINT NOT NULL REFERENCES dishes(id) ON DELETE CASCADE,
    quantity  BIGINT NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    sold_out  BOOLEAN NOT NULL DEFAULT false,
    UNIQUE (store_id, sales_day, dish_id)
);
CREATE INDEX idx_daily_sales_lookup ON daily_sales(store_id, sales_day);

-- Per-dish daily sellout threshold.
CREATE TABLE sellout_thresholds (
    store_id  BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    dish_id   BIGINT NOT NULL REFERENCES dishes(id) ON DELETE CASCADE,
    threshold BIGINT NOT NULL CHECK (threshold > 0),
    PRIMARY KEY (store_id, dish_id)
);
