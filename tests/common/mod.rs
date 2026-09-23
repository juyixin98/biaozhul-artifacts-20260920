//! Shared chain-building helpers for integration tests. Every hash and
//! signature here is computed for real through the library.

#![allow(dead_code)]

use std::collections::HashMap;

use utxo_rollback::builder::{
    genesis, sign_spend, track_outputs, unsigned_spend, BuilderWallet, UtxoRef,
};
pub use utxo_rollback::builder::build_block;
pub use utxo_rollback::wallet::Wallet;
use utxo_rollback::crypto;
use utxo_rollback::types::{Block, OutPoint, Transaction, TxIn, TxOut};

pub fn wallets() -> (Wallet, Wallet, Wallet) {
    (Wallet::generate(), Wallet::generate(), Wallet::generate())
}

pub fn block_hash(b: &Block) -> [u8; 32] {
    let merkle: [u8; 32] = hex::decode(&b.merkle_root).unwrap().try_into().unwrap();
    let prev: [u8; 32] = hex::decode(&b.prev_hash).unwrap().try_into().unwrap();
    crypto::hash256(&crypto::serialize_header(
        b.height, &prev, b.timestamp, &merkle,
    ))
}

/// Genesis paying the 50 subsidy to `miner`.
pub fn block0(miner: &Wallet) -> Block {
    genesis(vec![TxOut {
        value: 50,
        address: miner.address_hex(),
    }])
}

/// Block 1: the miner spends their genesis 50, paying `amount` to `recipient`,
/// keeping `50 - amount - fee` as change; coinbase pays `50 + fee` to miner.
pub fn block1(
    miner: &Wallet,
    prev: [u8; 32],
    recipient: &str,
    amount: u64,
    fee: u64,
) -> (Block, [u8; 32]) {
    let b0_coin = UtxoRef {
        txid: crypto::txid(&block0(miner).txs[0]).unwrap(),
        vout: 0,
        value: 50,
    };
    let mut sender = BuilderWallet::new(clone_wallet(miner));
    sender.utxos.push(b0_coin.clone());
    let change = 50 - amount - fee;
    let mut outs = vec![TxOut {
        value: amount,
        address: recipient.to_string(),
    }];
    if change > 0 {
        outs.push(TxOut {
            value: change,
            address: miner.address_hex(),
        });
    }
    let tx = sign_spend(unsigned_spend(&[b0_coin], outs), miner);
    let id = crypto::txid(&tx).unwrap();
    let block = build_block(
        1,
        prev,
        1,
        vec![TxOut {
            value: 50 + fee,
            address: miner.address_hex(),
        }],
        vec![tx],
    );
    (block, id)
}

/// Block 2: `owner` (who has a 30 output from block1's payment) spends it.
pub fn block2(
    owner: &Wallet,
    owner_coin: UtxoRef,
    prev: [u8; 32],
    miner: &Wallet,
    recipient: &str,
    amount: u64,
    fee: u64,
) -> (Block, [u8; 32]) {
    let _sender = BuilderWallet::new(clone_wallet(owner));
    let change = owner_coin.value - amount - fee;
    let mut outs = vec![TxOut {
        value: amount,
        address: recipient.to_string(),
    }];
    if change > 0 {
        outs.push(TxOut {
            value: change,
            address: owner.address_hex(),
        });
    }
    let tx = sign_spend(unsigned_spend(&[owner_coin], outs), owner);
    let id = crypto::txid(&tx).unwrap();
    let block = build_block(
        2,
        prev,
        2,
        vec![TxOut {
            value: 50 + fee,
            address: miner.address_hex(),
        }],
        vec![tx],
    );
    (block, id)
}

/// A block-1 candidate that spends the genesis output twice in one tx.
/// The coinbase is exact (50, zero fee) so the double spend is the sole error.
/// `genesis_txid` identifies the real genesis coin being spent.
pub fn double_spend_block1(
    prev: [u8; 32],
    coinbase_to: &str,
    genesis_txid: [u8; 32],
    spender: &Wallet,
) -> Block {
    let mk = || TxIn {
        prev: Some(OutPoint {
            txid: hex::encode(genesis_txid),
            vout: 0,
        }),
        coinbase_tag: String::new(),
        pubkey: String::new(),
        signature: String::new(),
    };
    let mut tx = Transaction {
        inputs: vec![mk(), mk()],
        outputs: vec![TxOut {
            value: 50,
            address: spender.address_hex(),
        }],
    };
    let id = crypto::txid(&tx).unwrap();
    let sig = spender.sign_txid(&id);
    for i in &mut tx.inputs {
        i.pubkey = spender.address_hex();
        i.signature = sig.clone();
    }
    build_block(
        1,
        prev,
        1,
        vec![TxOut {
            value: 50,
            address: coinbase_to.to_string(),
        }],
        vec![tx],
    )
}

pub fn clone_wallet(w: &Wallet) -> Wallet {
    utxo_rollback::wallet::wallet_from_secret(&w.secret_hex()).unwrap()
}

/// Wallet registry helper: record outputs paid to known wallets after a tx.
pub fn credit(
    wallets: &mut HashMap<String, BuilderWallet>,
    txid: [u8; 32],
    tx: &Transaction,
) {
    track_outputs(txid, tx, wallets);
}
