//! Offline block-building helpers.
//!
//! These construct protocol-valid blocks and compute *real* hashes and
//! signatures, so the examples and tests exercise the same cryptographic path
//! the server verifies. Nothing here is mocked.

use std::collections::HashMap;

use crate::crypto::{self, HASH_LEN};
use crate::types::{Block, OutPoint, Transaction, TxIn, TxOut};
use crate::wallet::Wallet;

/// A reference to an output a builder wallet can spend.
#[derive(Clone, Debug)]
pub struct UtxoRef {
    pub txid: [u8; HASH_LEN],
    pub vout: u32,
    pub value: u64,
}

/// A wallet plus the live UTXOs it can spend inside one built chain.
pub struct BuilderWallet {
    pub wallet: Wallet,
    pub utxos: Vec<UtxoRef>,
}

impl BuilderWallet {
    pub fn new(wallet: Wallet) -> Self {
        BuilderWallet {
            wallet,
            utxos: Vec::new(),
        }
    }

    pub fn address(&self) -> String {
        self.wallet.address_hex()
    }

    pub fn balance(&self) -> u64 {
        self.utxos.iter().map(|u| u.value).sum()
    }
}

/// Build a genesis block whose coinbase pays `reward` (must equal the fixed
/// subsidy at height 0) to the given addresses, split any way.
pub fn genesis(coinbase_outputs: Vec<TxOut>) -> Block {
    build_block(0, [0u8; 32], 0, coinbase_outputs, vec![])
}

/// Assemble and finalize a block: caller supplies the coinbase outputs
/// (subsidy + fees must be exactly covered) and fully-formed other txs.
/// Computes the real txids/merkle root.
pub fn build_block(
    height: u64,
    prev_hash: [u8; 32],
    timestamp: i64,
    coinbase_outputs: Vec<TxOut>,
    mut txs: Vec<Transaction>,
) -> Block {
    assert!(!coinbase_outputs.is_empty(), "coinbase needs outputs");
    let coinbase = Transaction {
        inputs: vec![TxIn {
            prev: None,
            // Bitcoin-style height tag, 8-byte little-endian, hex.
            coinbase_tag: hex::encode(height.to_le_bytes()),
            pubkey: String::new(),
            signature: String::new(),
        }],
        outputs: coinbase_outputs,
    };
    let mut all = vec![coinbase];
    all.append(&mut txs);

    let txids: Vec<[u8; 32]> = all
        .iter()
        .map(crypto::txid)
        .collect::<Result<_, _>>()
        .expect("txid");
    let merkle = crypto::merkle_root(&txids);

    Block {
        height,
        prev_hash: hex::encode(prev_hash),
        timestamp,
        txs: all,
        merkle_root: hex::encode(merkle),
    }
}

/// Build a spend transaction from `sender`, consuming `inputs` (each with an
/// output index in the sender's wallet), paying `outputs`. A change output is
/// appended when `change` is `Some(value)` (value must be > 0).
/// Returns the unsigned tx first so it can be signed deterministically; use
/// [`sign_spend`] to attach real signatures.
pub fn unsigned_spend(
    inputs: &[UtxoRef],
    outputs: Vec<TxOut>,
) -> Transaction {
    let tx = Transaction {
        inputs: inputs
            .iter()
            .map(|u| TxIn {
                prev: Some(OutPoint {
                    txid: hex::encode(u.txid),
                    vout: u.vout,
                }),
                coinbase_tag: String::new(),
                pubkey: String::new(),
                signature: String::new(),
            })
            .collect(),
        outputs,
    };
    tx
}

/// Attach real Ed25519 signatures (over the txid) and pubkeys to every input.
pub fn sign_spend(mut tx: Transaction, signer: &Wallet) -> Transaction {
    let id = crypto::txid(&tx).expect("txid");
    let sig = signer.sign_txid(&id);
    let pk = signer.address_hex();
    for input in &mut tx.inputs {
        input.pubkey = pk.clone();
        input.signature = sig.clone();
    }
    tx
}

/// Build a one-input spend with an explicit `fee` and an optional change
/// output, signed. Change = `spend.value - amount - fee`.
pub fn simple_transfer(
    sender: &BuilderWallet,
    spend: &UtxoRef,
    to_address: &str,
    amount: u64,
    fee: u64,
) -> (Transaction, [u8; 32]) {
    assert!(
        amount + fee <= spend.value,
        "amount + fee must not exceed the input value"
    );
    let mut outs = vec![TxOut {
        value: amount,
        address: to_address.to_string(),
    }];
    let change = spend.value - amount - fee;
    if change > 0 {
        outs.push(TxOut {
            value: change,
            address: sender.address(),
        });
    }
    let tx = sign_spend(unsigned_spend(std::slice::from_ref(spend), outs), &sender.wallet);
    let id = crypto::txid(&tx).unwrap();
    (tx, id)
}

/// Add the outputs a transaction grants to the matching wallets.
pub fn track_outputs(
    txid: [u8; 32],
    tx: &Transaction,
    wallets: &mut HashMap<String, BuilderWallet>,
) {
    for (v, o) in tx.outputs.iter().enumerate() {
        if let Some(w) = wallets.get_mut(&o.address) {
            w.utxos.push(UtxoRef {
                txid,
                vout: v as u32,
                value: o.value,
            });
        }
    }
}

/// Remove `refs` (by txid/vout) from a wallet's spendable set.
pub fn consume(w: &mut BuilderWallet, refs: &[UtxoRef]) {
    w.utxos
        .retain(|u| !refs.iter().any(|r| r.txid == u.txid && r.vout == u.vout));
}
