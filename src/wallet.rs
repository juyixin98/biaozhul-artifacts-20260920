//! Key generation and signing helpers for the CLI, examples and tests.

use ed25519_dalek::{Signer, SigningKey, KEYPAIR_LENGTH};
use rand::rngs::OsRng;
use sha2::{Digest, Sha256};

use crate::types::Transaction;

/// A freshly generated Ed25519 keypair (the signing key, from which the
/// verifying key is derived).
pub struct Wallet {
    pub signing: SigningKey,
}

impl Wallet {
    pub fn generate() -> Self {
        Wallet {
            signing: SigningKey::generate(&mut OsRng),
        }
    }

    /// 32-byte hex verifying key, used both as `pubkey` on spends and as
    /// `address` on outputs.
    pub fn address_hex(&self) -> String {
        hex::encode(self.signing.verifying_key().to_bytes())
    }

    /// Sign the txid (double-SHA256 of the unsigned tx) — the exact message
    /// the validator checks against.
    pub fn sign_txid(&self, txid: &[u8; 32]) -> String {
        hex::encode(self.signing.sign(txid).to_bytes())
    }

    /// Sign an arbitrary message (used for ad-hoc demo signing).
    pub fn sign_message(&self, msg: &[u8]) -> String {
        hex::encode(self.signing.sign(msg).to_bytes())
    }

    /// 64-byte hex keypair (32 secret + 32 public) for `wallet_from_secret`.
    pub fn secret_hex(&self) -> String {
        hex::encode(self.signing.to_keypair_bytes())
    }
}

/// Reconstruct a wallet from the 64-byte hex keypair emitted by `secret_hex`.
pub fn wallet_from_secret(s: &str) -> Result<Wallet, String> {
    let raw = hex::decode(s).map_err(|e| e.to_string())?;
    if raw.len() != KEYPAIR_LENGTH {
        return Err(format!("expected {KEYPAIR_LENGTH} secret bytes, got {}", raw.len()));
    }
    let mut bytes = [0u8; KEYPAIR_LENGTH];
    bytes.copy_from_slice(&raw);
    let signing = SigningKey::from_keypair_bytes(&bytes).map_err(|e| e.to_string())?;
    Ok(Wallet { signing })
}

/// Build the exact 32-byte message signed for a transaction.
pub fn tx_signing_message(tx: &Transaction) -> [u8; 32] {
    let first = Sha256::digest(crate::crypto::serialize_transaction_unsigned(tx).unwrap());
    Sha256::digest(first).into()
}
