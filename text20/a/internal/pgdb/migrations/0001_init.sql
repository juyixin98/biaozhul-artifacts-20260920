-- SignalBoard initial schema.
-- All monetary amounts are integer cents (BIGINT). All times are timestamptz.

CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE stores (
    id           BIGSERIAL PRIMARY KEY,
    name         TEXT NOT NULL CHECK (length(btrim(name)) > 0),
    timezone     TEXT NOT NULL,
    menu_version BIGINT NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Editable dish definitions. These are the DRAFT / catalog state: editing them
-- never changes what screens show. Live content lives in menu_versions*.
CREATE TABLE dishes (
    id          BIGSERIAL PRIMARY KEY,
    store_id    BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    sku         TEXT NOT NULL CHECK (length(sku) > 0),
    name        TEXT NOT NULL CHECK (length(name) > 0),
    base_price  BIGINT NOT NULL CHECK (base_price >= 0), -- cents
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    daily_limit BIGINT,                                  -- sold-out threshold per local day; NULL = no limit
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (store_id, sku)
);
CREATE INDEX idx_dishes_store ON dishes (store_id);

-- Immutable published menu versions. Once written, rows are never updated.
CREATE TABLE menu_versions (
    id           BIGSERIAL PRIMARY KEY,
    store_id     BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    version      BIGINT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    note         TEXT NOT NULL DEFAULT '',
    UNIQUE (store_id, version)
);

CREATE TABLE menu_version_items (
    id            BIGSERIAL PRIMARY KEY,
    version_id    BIGINT NOT NULL REFERENCES menu_versions(id) ON DELETE CASCADE,
    sku           TEXT NOT NULL,
    name          TEXT NOT NULL,
    base_price    BIGINT NOT NULL CHECK (base_price >= 0),
    display_order INTEGER NOT NULL DEFAULT 0,
    UNIQUE (version_id, sku)
);
CREATE INDEX idx_version_items_version ON menu_version_items (version_id);

-- Temporary prices are attached to the publish that introduced them.
-- Half-open [start_at, end_at) windows per (version, sku) must not overlap.
-- The GiST exclusion constraint is the hard guarantee: no transaction, no
-- matter how concurrent, can commit an overlapping pair. Adjacent windows
-- ([a,b) and [b,c)) are allowed, matching left-closed/right-open semantics.
CREATE TABLE temporary_prices (
    id         BIGSERIAL PRIMARY KEY,
    store_id   BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    version_id BIGINT NOT NULL REFERENCES menu_versions(id) ON DELETE CASCADE,
    sku        TEXT NOT NULL,
    price      BIGINT NOT NULL CHECK (price >= 0), -- cents
    start_at   TIMESTAMPTZ NOT NULL,
    end_at     TIMESTAMPTZ NOT NULL,
    CHECK (start_at < end_at),
    CONSTRAINT temp_price_no_overlap
        EXCLUDE USING gist (
            version_id WITH =,
            sku       WITH =,
            tstzrange(start_at, end_at, '[)') WITH &&
        )
);
CREATE INDEX idx_temp_prices_version_sku ON temporary_prices (version_id, sku);
CREATE INDEX idx_temp_prices_lookup
    ON temporary_prices (version_id, sku, start_at, end_at);

-- Inbound sales events. (store_id, event_id) is the idempotency key.
CREATE TABLE sales_events (
    id          BIGSERIAL PRIMARY KEY,
    store_id    BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    event_id    TEXT NOT NULL CHECK (length(event_id) > 0),
    sku         TEXT NOT NULL,
    qty         INTEGER NOT NULL CHECK (qty > 0),
    occurred_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (store_id, event_id)
);

-- Per-day counters; the day is derived from occurred_at in the STORE's own
-- timezone, so late events land on the day they actually happened.
CREATE TABLE daily_sales (
    id        BIGSERIAL PRIMARY KEY,
    store_id  BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    sales_day DATE NOT NULL,
    sku       TEXT NOT NULL,
    qty       BIGINT NOT NULL CHECK (qty >= 0),
    UNIQUE (store_id, sales_day, sku)
);

-- Sold-out markers are day-scoped (local day), so a new day is an automatic
-- recovery: reads only match a marker whose sales_day equals "today" locally.
CREATE TABLE sold_outs (
    store_id     BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    sku          TEXT NOT NULL,
    sales_day    DATE NOT NULL,
    triggered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (store_id, sku, sales_day)
);

-- Physical screens authenticate with an opaque per-screen token (only the
-- SHA-256 hash is stored). The token scopes the screen to exactly one store.
CREATE TABLE screens (
    id             BIGSERIAL PRIMARY KEY,
    store_id       BIGINT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    name           TEXT NOT NULL CHECK (length(btrim(name)) > 0),
    token_hash     TEXT NOT NULL UNIQUE,
    last_heartbeat TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_screens_store ON screens (store_id);
