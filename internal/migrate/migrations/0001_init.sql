-- SignalBoard schema.
-- All money is integer cents. All timestamps are timestamptz (UTC);
-- clients send ISO-8601 with an explicit UTC offset.

CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE stores (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL,
    timezone        text NOT NULL,           -- IANA name, e.g. 'Asia/Shanghai'
    current_version integer NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- One mutable draft per store. Drafts never affect the live menu.
CREATE TABLE drafts (
    store_id   uuid PRIMARY KEY REFERENCES stores(id) ON DELETE CASCADE,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE draft_items (
    store_id           uuid NOT NULL REFERENCES drafts(store_id) ON DELETE CASCADE,
    item_key           text NOT NULL,
    name               text NOT NULL,
    price_cents        integer NOT NULL CHECK (price_cents >= 0),
    sold_out_threshold integer NOT NULL DEFAULT 0 CHECK (sold_out_threshold >= 0),
    position           integer NOT NULL DEFAULT 0,
    PRIMARY KEY (store_id, item_key)
);

-- Temporary price bands on the draft. Half-open [starts_at, ends_at).
-- The exclusion constraint makes overlapping bands for the same item
-- impossible even under concurrent writers (no check-then-insert race).
CREATE TABLE draft_temp_prices (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id    uuid NOT NULL REFERENCES drafts(store_id) ON DELETE CASCADE,
    item_key    text NOT NULL,
    price_cents integer NOT NULL CHECK (price_cents >= 0),
    starts_at   timestamptz NOT NULL,
    ends_at     timestamptz NOT NULL,
    CHECK (starts_at < ends_at),
    FOREIGN KEY (store_id, item_key) REFERENCES draft_items(store_id, item_key) ON DELETE CASCADE,
    EXCLUDE USING gist (
        store_id WITH =,
        item_key WITH =,
        tstzrange(starts_at, ends_at, '[)') WITH &&
    )
);

-- Immutable published versions.
CREATE TABLE menu_versions (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id     uuid NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    version      integer NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (store_id, version)
);

CREATE TABLE version_items (
    version_id         uuid NOT NULL REFERENCES menu_versions(id) ON DELETE CASCADE,
    item_key           text NOT NULL,
    name               text NOT NULL,
    price_cents        integer NOT NULL,
    sold_out_threshold integer NOT NULL,
    position           integer NOT NULL,
    PRIMARY KEY (version_id, item_key)
);

CREATE TABLE version_temp_prices (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id  uuid NOT NULL REFERENCES menu_versions(id) ON DELETE CASCADE,
    item_key    text NOT NULL,
    price_cents integer NOT NULL,
    starts_at   timestamptz NOT NULL,
    ends_at     timestamptz NOT NULL,
    CHECK (starts_at < ends_at)
);

-- Raw sales events. The PK is the client-supplied event id, which makes
-- ingestion idempotent: a retried event conflicts on id and is not recounted.
CREATE TABLE sales_events (
    id          uuid PRIMARY KEY,
    store_id    uuid NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    item_key    text NOT NULL,
    quantity    integer NOT NULL CHECK (quantity > 0),
    occurred_at timestamptz NOT NULL,   -- when the sale actually happened
    received_at timestamptz NOT NULL DEFAULT now()
);

-- Per-store, per-item, per-day (store-local day) aggregates.
CREATE TABLE daily_sales (
    store_id uuid NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    item_key text NOT NULL,
    day      date NOT NULL,
    quantity integer NOT NULL,
    PRIMARY KEY (store_id, item_key, day)
);

-- Menu screens. Only the SHA-256 hash of the bearer token is stored.
CREATE TABLE screens (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id          uuid NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    name              text NOT NULL,
    token_hash        text NOT NULL UNIQUE,
    created_at        timestamptz NOT NULL DEFAULT now(),
    last_heartbeat_at timestamptz
);
