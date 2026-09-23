//! Batch input loading and output reporting formats.

use anyhow::{Context, Result};
use batch_median_core::Journal;
use serde::{Deserialize, Serialize};
use std::path::Path;

/// On-disk sample format, e.g. `examples/batch.json`:
/// `{"values": [9, -1, 1000000, 0, 42]}`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BatchFile {
    /// Signed integers in their original, unordered form.
    pub values: Vec<i64>,
}

impl BatchFile {
    pub fn load(path: &Path) -> Result<Self> {
        let text =
            std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
        let parsed: BatchFile = serde_json::from_str(&text)
            .with_context(|| format!("parsing JSON in {}", path.display()))?;
        Ok(parsed)
    }
}

/// Machine-readable result document (printed to stdout / written to disk).
/// Timings are kept separate so consumers can distinguish execution, proving
/// and verification cost.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ResultReport {
    pub verified: bool,
    pub image_id_hex: String,
    pub dev_mode: bool,
    pub receipt_flavor: String,
    pub input_length: usize,
    pub median: Option<i64>,
    pub input_commitment_hex: Option<String>,
    pub journal_hex: Option<String>,
    pub receipt_path: Option<String>,
    pub timings_ms: Timings,
    pub cycles: CycleStats,
    pub note: Option<String>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Timings {
    /// zkVM execution (no proof generated).
    pub execution_ms: u128,
    /// STARK proving.
    pub proving_ms: u128,
    /// Receipt verification + admission/binding checks.
    pub verification_ms: u128,
    /// End-to-end wall time.
    pub total_ms: u128,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct CycleStats {
    pub total: u64,
    pub user: u64,
    pub segments: u64,
}

/// Content of a receipt file: receipt plus the inputs it was produced for,
/// so `verify` can re-check the input binding offline.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReceiptEnvelope {
    pub schema: String,
    pub dev_mode: bool,
    pub values: Vec<i64>,
    /// Base64-independent, hex-encoded bincode-serialized Receipt.
    pub receipt_hex: String,
}

impl ReceiptEnvelope {
    pub fn pack(dev_mode: bool, values: &[i64], receipt: &risc0_zkvm::Receipt) -> Result<Self> {
        let bytes = bincode_serialize(receipt)?;
        Ok(Self {
            schema: "batch-median-receipt-v1".into(),
            dev_mode,
            values: values.to_vec(),
            receipt_hex: hex::encode(bytes),
        })
    }

    pub fn unpack(&self) -> Result<risc0_zkvm::Receipt> {
        let bytes = hex::decode(&self.receipt_hex).context("receipt hex is not valid hex")?;
        bincode_deserialize(&bytes).context("decoding receipt bytes")
    }
}

pub(crate) fn bincode_serialize<T: Serialize>(value: &T) -> Result<Vec<u8>> {
    bincode::serialize(value).context("serializing receipt")
}

pub(crate) fn bincode_deserialize<T: for<'de> Deserialize<'de>>(bytes: &[u8]) -> Result<T> {
    let value = bincode::deserialize(bytes)?;
    Ok(value)
}

/// Convert a parsed journal into the result-report fragment shared by commands.
pub fn journal_summary(j: &Journal) -> (i64, String) {
    (j.median, hex::encode(j.input_commitment.as_bytes()))
}
