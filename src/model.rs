//! Domain model: assets, pools and local pool snapshots, plus validation
//! and the canonical hashing that defines a snapshot id.

use std::collections::HashSet;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::amount::Amount;

/// Maximum length (in bytes) of an asset or pool identifier.
pub const MAX_ID_LEN: usize = 128;

/// An on-chain asset. Precision is mandatory: routing a quote for an asset
/// whose decimals are unknown is refused (see [`Snapshot::validate`]).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Asset {
    /// Asset symbol / address, e.g. `"USDC"` or `"0xa0b86991…"`.
    pub id: String,
    /// Number of fractional digits the asset carries on chain (0..=255).
    pub decimals: u8,
}

/// A constant-product pool with two sides.
///
/// Direction semantics:
/// * swapping `token0 -> token1` consumes `reserve0`, produces `reserve1`,
///   then deducts the explicit cost [`Pool::cost_token1_out`] (denominated in
///   **token1**, the output asset);
/// * swapping `token1 -> token0` consumes `reserve1`, produces `reserve0`,
///   then deducts [`Pool::cost_token0_out`] (denominated in **token0**).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Pool {
    pub id: String,
    pub token0: String,
    pub token1: String,
    /// Reserve of [`Pool::token0`] in smallest units; must be non-zero.
    pub reserve0: Amount,
    /// Reserve of [`Pool::token1`] in smallest units; must be non-zero.
    pub reserve1: Amount,
    /// Proportional fee in basis points of the *input* amount, 0..=10000.
    pub fee_bps: u32,
    /// Explicit fixed cost taken from output when output is token0.
    #[serde(default = "Amount::zero_const")]
    pub cost_token0_out: Amount,
    /// Explicit fixed cost taken from output when output is token1.
    #[serde(default = "Amount::zero_const")]
    pub cost_token1_out: Amount,
}

impl Amount {
    // serde default helper
    #[doc(hidden)]
    pub fn zero_const() -> Self {
        Amount::ZERO
    }
}

/// A frozen, local view of on-chain liquidity that quotes are computed against.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Snapshot {
    pub assets: Vec<Asset>,
    pub pools: Vec<Pool>,
}

/// Reasons a snapshot upload may be refused.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SnapshotError {
    EmptyAssetId,
    AssetIdTooLong(String),
    EmptyPoolId,
    PoolIdTooLong(String),
    DuplicateAssetId(String),
    DuplicatePoolId(String),
    FeeOutOfRange { pool: String, fee_bps: u32 },
    SameSidePool(String),
    ZeroReserve(String),
    UnknownAsset { pool: String, asset: String },
}

impl std::fmt::Display for SnapshotError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SnapshotError::EmptyAssetId => f.write_str("asset id must be a non-empty string"),
            SnapshotError::AssetIdTooLong(id) => write!(f, "asset id {id:?} exceeds {MAX_ID_LEN} bytes"),
            SnapshotError::EmptyPoolId => f.write_str("pool id must be a non-empty string"),
            SnapshotError::PoolIdTooLong(id) => write!(f, "pool id {id:?} exceeds {MAX_ID_LEN} bytes"),
            SnapshotError::DuplicateAssetId(id) => write!(f, "duplicate asset id {id:?}"),
            SnapshotError::DuplicatePoolId(id) => write!(f, "duplicate pool id {id:?}"),
            SnapshotError::FeeOutOfRange { pool, fee_bps } => write!(
                f,
                "pool {pool:?} fee_bps={fee_bps} is outside the allowed range 0..=10000"
            ),
            SnapshotError::SameSidePool(id) => write!(f, "pool {id:?} has token0 == token1"),
            SnapshotError::ZeroReserve(id) => write!(f, "pool {id:?} has a zero reserve"),
            SnapshotError::UnknownAsset { pool, asset } => {
                write!(f, "pool {pool:?} references unknown asset {asset:?}")
            }
        }
    }
}

impl std::error::Error for SnapshotError {}

impl Snapshot {
    /// Structural validation: known precision for every referenced asset,
    /// positive reserves, sane fees and unique ids.
    pub fn validate(&self) -> Result<(), SnapshotError> {
        let mut assets = HashSet::new();
        for a in &self.assets {
            if a.id.is_empty() {
                return Err(SnapshotError::EmptyAssetId);
            }
            if a.id.len() > MAX_ID_LEN {
                return Err(SnapshotError::AssetIdTooLong(a.id.clone()));
            }
            if !assets.insert(&a.id) {
                return Err(SnapshotError::DuplicateAssetId(a.id.clone()));
            }
        }

        let mut pools = HashSet::new();
        for p in &self.pools {
            if p.id.is_empty() {
                return Err(SnapshotError::EmptyPoolId);
            }
            if p.id.len() > MAX_ID_LEN {
                return Err(SnapshotError::PoolIdTooLong(p.id.clone()));
            }
            if !pools.insert(&p.id) {
                return Err(SnapshotError::DuplicatePoolId(p.id.clone()));
            }
            if p.token0 == p.token1 {
                return Err(SnapshotError::SameSidePool(p.id.clone()));
            }
            if p.reserve0.is_zero() || p.reserve1.is_zero() {
                return Err(SnapshotError::ZeroReserve(p.id.clone()));
            }
            if p.fee_bps > 10_000 {
                return Err(SnapshotError::FeeOutOfRange {
                    pool: p.id.clone(),
                    fee_bps: p.fee_bps,
                });
            }
            for asset in [&p.token0, &p.token1] {
                if !assets.contains(asset) {
                    return Err(SnapshotError::UnknownAsset {
                        pool: p.id.clone(),
                        asset: asset.clone(),
                    });
                }
            }
        }
        Ok(())
    }

    /// Canonical byte serialization: deterministic field order (from the
    /// structs) and assets/pools sorted lexicographically by id, so that two
    /// semantically identical snapshots always hash identically.
    pub fn canonical_bytes(&self) -> Vec<u8> {
        let mut assets = self.assets.clone();
        assets.sort_by(|a, b| a.id.cmp(&b.id));
        let mut pools = self.pools.clone();
        pools.sort_by(|a, b| a.id.cmp(&b.id));
        let ordered = Snapshot { assets, pools };
        // Struct field order is fixed by declaration; every value type has a
        // canonical serde representation (amounts are digit strings).
        serde_json::to_vec(&ordered).expect("snapshot serialization is infallible")
    }

    /// Snapshot id: hex-encoded SHA-256 of the canonical bytes.
    pub fn id(&self) -> String {
        let mut hasher = Sha256::new();
        hasher.update(self.canonical_bytes());
        hex::encode(hasher.finalize())
    }

    pub fn asset(&self, id: &str) -> Option<&Asset> {
        self.assets.iter().find(|a| a.id == id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn usdc_eth() -> Snapshot {
        Snapshot {
            assets: vec![
                Asset { id: "ETH".into(), decimals: 18 },
                Asset { id: "USDC".into(), decimals: 6 },
            ],
            pools: vec![Pool {
                id: "p1".into(),
                token0: "ETH".into(),
                token1: "USDC".into(),
                reserve0: Amount::from(1_000_000_000_000_000_000_000u128),
                reserve1: Amount::from(3_000_000_000_000u128),
                fee_bps: 30,
                cost_token0_out: Amount::ZERO,
                cost_token1_out: Amount::ZERO,
            }],
        }
    }

    #[test]
    fn canonical_hash_is_order_independent() {
        let a = usdc_eth();
        let mut b = a.clone();
        b.assets.reverse();
        assert_eq!(a.id(), b.id());
        assert_eq!(a.id().len(), 64);
    }

    #[test]
    fn rejects_zero_reserve_missing_precision_and_bad_fee() {
        let mut s = usdc_eth();
        s.pools[0].reserve1 = Amount::ZERO;
        assert_eq!(s.validate().unwrap_err(), SnapshotError::ZeroReserve("p1".into()));

        let mut s = usdc_eth();
        s.pools[0].reserve1 = Amount::ZERO;
        s.pools[0].token1 = "USDC".into();
        s.pools[0].reserve0 = s.pools[0].reserve1;
        assert!(matches!(s.validate(), Err(SnapshotError::ZeroReserve(_))));

        let mut s = usdc_eth();
        s.pools[0].fee_bps = 10_001;
        assert!(matches!(s.validate(), Err(SnapshotError::FeeOutOfRange { .. })));

        let mut s = usdc_eth();
        s.pools[0].token1 = "DAI".into();
        assert!(matches!(s.validate(), Err(SnapshotError::UnknownAsset { .. })));

        let mut s = usdc_eth();
        s.assets[1].decimals = 255;
        assert!(s.validate().is_ok());
    }

    #[test]
    fn explicit_costs_default_to_zero() {
        let p: Pool =
            serde_json::from_str(r#"{"id":"p","token0":"A","token1":"B","reserve0":"1","reserve1":"1","fee_bps":0}"#)
                .unwrap();
        assert_eq!(p.cost_token0_out, Amount::ZERO);
        assert_eq!(p.cost_token1_out, Amount::ZERO);
    }
}
