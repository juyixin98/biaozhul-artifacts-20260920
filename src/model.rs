//! Domain models and input validation.
//!
//! Amounts cross the JSON boundary as decimal strings of 128-bit unsigned
//! integers (serde renders u128 itself as a number, but strings stay
//! interoperable with clients that cannot represent 128-bit numbers).

use serde::{Deserialize, Serialize};

/// Maximum hop count the router searches.
pub const MAX_HOPS: usize = 3;

/// Fee precision: basis points.
pub const FEE_BPS_MAX_EXCLUSIVE: u32 = 10_000;

/// Asset decimals above this are certainly malformed; u128 holds ~38.2
/// decimal digits, so amounts with more decimals could not even be
/// represented in raw units.
pub const DECIMALS_MAX: u8 = 38;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Asset {
    pub id: String,
    pub decimals: u8,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Pool {
    pub id: String,
    pub token0: String,
    pub token1: String,
    pub reserve0: u128,
    pub reserve1: u128,
    /// Fee in basis points, 0..10000.
    pub fee_bps: u32,
}

/// Fully validated, in-memory snapshot used by the router.
#[derive(Clone, Debug)]
pub struct Snapshot {
    pub id: i64,
    pub content_hash: String,
    pub created_at: String,
    pub assets: Vec<Asset>,
    pub pools: Vec<Pool>,
}

// ---------- API request / response DTOs ----------

#[derive(Deserialize)]
pub struct AssetInput {
    pub id: String,
    pub decimals: u32,
}

#[derive(Deserialize)]
pub struct PoolInput {
    pub id: String,
    pub token0: String,
    pub token1: String,
    /// Raw reserve amounts as decimal strings.
    pub reserve0: String,
    pub reserve1: String,
    pub fee_bps: u32,
}

#[derive(Deserialize)]
pub struct SnapshotInput {
    pub assets: Vec<AssetInput>,
    pub pools: Vec<PoolInput>,
}

#[derive(Serialize)]
pub struct SnapshotResponse {
    pub snapshot_id: i64,
    pub created_at: String,
    pub content_hash: String,
    pub asset_count: usize,
    pub pool_count: usize,
}

#[derive(Deserialize)]
pub struct QuoteRequest {
    pub snapshot_id: Option<i64>,
    pub token_in: String,
    pub token_out: String,
    pub amount_in: String,
    /// Fixed explicit cost charged per hop, in that hop's *output* token,
    /// raw units. Defaults to 0.
    #[serde(default = "default_zero_amount")]
    pub cost_per_hop: String,
    /// Slippage tolerance in bps applied to net output for `min_output`.
    /// 0..=10000; default 0.
    #[serde(default)]
    pub slippage_bps: Option<u32>,
    /// Client-side minimum acceptable net output; a route below this is a
    /// 422 rather than a quote.
    pub min_output: Option<String>,
    #[serde(default = "default_max_hops")]
    pub max_hops: usize,
}

fn default_max_hops() -> usize {
    MAX_HOPS
}

fn default_zero_amount() -> String {
    "0".to_string()
}

#[derive(Serialize, Clone)]
pub struct HopView {
    pub hop: usize,
    pub pool_id: String,
    pub token_in: String,
    pub token_out: String,
    pub amount_in: String,
    pub reserve_in: String,
    pub reserve_out: String,
    pub fee_bps: u32,
    pub gross_amount_out: String,
    pub explicit_cost: String,
    pub net_amount_out: String,
}

#[derive(Serialize)]
pub struct QuoteResponse {
    pub snapshot_id: i64,
    pub content_hash: String,
    pub token_in: String,
    pub token_out: String,
    pub amount_in: String,
    pub hops: Vec<HopView>,
    pub gross_amount_out: String,
    /// Explicit cost charged on the final hop, in output-token units.
    /// Per-hop costs (in their own hop output token) live in each hop.
    pub final_hop_explicit_cost: String,
    pub net_amount_out: String,
    pub min_output: String,
    pub paths_considered: usize,
    pub feasible_paths: usize,
}

/// Parse and validate a decimal-string u128 amount.
pub fn parse_amount(s: &str, field: &str) -> Result<u128, String> {
    let s = s.trim();
    if s.is_empty() {
        return Err(format!("{field} must be a non-empty decimal string"));
    }
    let v: u128 = s
        .parse()
        .map_err(|_| format!("{field} is not a valid u128 decimal amount: {s}"))?;
    Ok(v)
}

/// Validate the full ingestion payload: known precision for every asset
/// referenced, strictly positive reserves, fees in range, no duplicates.
pub fn validate_snapshot_input(input: &SnapshotInput) -> Result<(Vec<Asset>, Vec<Pool>), String> {
    if input.assets.is_empty() {
        return Err("snapshot must contain at least one asset".to_string());
    }
    if input.pools.is_empty() {
        return Err("snapshot must contain at least one pool".to_string());
    }

    let mut assets: Vec<Asset> = Vec::with_capacity(input.assets.len());
    for a in &input.assets {
        let id = a.id.trim();
        if id.is_empty() {
            return Err("asset id must be non-empty".to_string());
        }
        if a.decimals as u64 > DECIMALS_MAX as u64 {
            return Err(format!(
                "asset {id} has unknown/unsupported precision: decimals {} > {DECIMALS_MAX}",
                a.decimals
            ));
        }
        if assets.iter().any(|x: &Asset| x.id == id) {
            return Err(format!("duplicate asset id in snapshot: {id}"));
        }
        assets.push(Asset {
            id: id.to_string(),
            decimals: a.decimals as u8,
        });
    }

    let has_asset = |id: &str| assets.iter().any(|a| a.id == id);

    let mut pools: Vec<Pool> = Vec::with_capacity(input.pools.len());
    for p in &input.pools {
        let pid = p.id.trim();
        if pid.is_empty() {
            return Err("pool id must be non-empty".to_string());
        }
        if pools.iter().any(|x: &Pool| x.id == pid) {
            return Err(format!("duplicate pool id in snapshot: {pid}"));
        }
        if p.token0 == p.token1 {
            return Err(format!("pool {pid} must connect two distinct tokens"));
        }
        if !has_asset(&p.token0) {
            return Err(format!(
                "pool {pid} references token0 {} whose precision is unknown",
                p.token0
            ));
        }
        if !has_asset(&p.token1) {
            return Err(format!(
                "pool {pid} references token1 {} whose precision is unknown",
                p.token1
            ));
        }
        let r0 = parse_amount(&p.reserve0, &format!("pool {pid} reserve0"))?;
        let r1 = parse_amount(&p.reserve1, &format!("pool {pid} reserve1"))?;
        if r0 == 0 || r1 == 0 {
            return Err(format!("pool {pid} has a zero reserve and cannot be routed through"));
        }
        if p.fee_bps >= FEE_BPS_MAX_EXCLUSIVE {
            return Err(format!(
                "pool {pid} fee_bps {} must be in 0..10000",
                p.fee_bps
            ));
        }
        pools.push(Pool {
            id: pid.to_string(),
            token0: p.token0.clone(),
            token1: p.token1.clone(),
            reserve0: r0,
            reserve1: r1,
            fee_bps: p.fee_bps,
        });
    }

    Ok((assets, pools))
}
