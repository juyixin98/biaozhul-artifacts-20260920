CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE schema_migrations (
    version INT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE stores (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    timezone TEXT NOT NULL,
    current_version INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Draft (working-copy) menu items. Edits here never touch the live menu.
CREATE TABLE items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id UUID NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    price_cents INT NOT NULL CHECK (price_cents >= 0),
    sold_out_threshold INT NOT NULL DEFAULT 0 CHECK (sold_out_threshold >= 0),
    position INT NOT NULL DEFAULT 0,
    archived BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX items_store_idx ON items (store_id);

-- Immutable published versions. Rows are never updated or deleted.
CREATE TABLE menu_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id UUID NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    version INT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (store_id, version)
);

CREATE TABLE menu_version_items (
    version_id UUID NOT NULL REFERENCES menu_versions(id) ON DELETE CASCADE,
    item_id UUID NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    price_cents INT NOT NULL,
    sold_out_threshold INT NOT NULL DEFAULT 0,
    position INT NOT NULL DEFAULT 0,
    PRIMARY KEY (version_id, item_id)
);

-- Temporary price windows. Half-open [lower, upper), stored in UTC.
-- The exclusion constraint guarantees non-overlap per item even under
-- concurrent writers (enforced by PostgreSQL, not by application code).
CREATE TABLE temp_prices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id UUID NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    item_id UUID NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    price_cents INT NOT NULL CHECK (price_cents >= 0),
    "window" TSTZRANGE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    EXCLUDE USING gist (item_id WITH =, "window" WITH &&)
);
CREATE INDEX temp_prices_store_idx ON temp_prices (store_id);

-- Idempotency ledger for sales events. PK makes duplicates impossible;
-- content_hash detects the same event id carrying different payloads.
CREATE TABLE sales_events (
    store_id UUID NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL,
    item_id UUID NOT NULL,
    quantity INT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    sale_date DATE NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (store_id, event_id)
);

-- Per-day sales totals in the store's local timezone.
CREATE TABLE daily_sales (
    store_id UUID NOT NULL,
    item_id UUID NOT NULL,
    sale_date DATE NOT NULL,
    quantity BIGINT NOT NULL,
    PRIMARY KEY (store_id, item_id, sale_date)
);

CREATE TABLE screens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id UUID NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    last_heartbeat_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
