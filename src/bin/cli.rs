//! Command-line utility.
//!
//! Subcommands:
//!   keygen                       generate a fresh Ed25519 keypair (JSON)
//!   gen-examples [DIR]           write signed example blocks into DIR
//!                                (default ./examples/generated)
//!   demo --base-url URL          end-to-end demo against a running server:
//!                                valid blocks, in-block double spend (must be
//!                                rejected), reorg via an alternative block,
//!                                rollback, historical queries, replay check.

use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;

use serde_json::json;
use utxo_rollback::builder::{
    build_block, genesis, simple_transfer, track_outputs, BuilderWallet, UtxoRef,
};
use utxo_rollback::crypto;
use utxo_rollback::types::{Block, OutPoint, Transaction, TxIn, TxOut};
use utxo_rollback::wallet::Wallet;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    match args.get(1).map(String::as_str) {
        Some("keygen") => cmd_keygen(),
        Some("gen-examples") => {
            let dir = args.get(2).cloned().unwrap_or_else(|| "examples/generated".into());
            cmd_gen_examples(&dir);
        }
        Some("demo") => {
            let mut base = "http://127.0.0.1:3000".to_string();
            if let Some(pos) = args.iter().position(|a| a == "--base-url") {
                base = args[pos + 1].clone();
            }
            if let Err(e) = cmd_demo(&base) {
                eprintln!("demo FAILED: {e}");
                std::process::exit(1);
            }
        }
        _ => {
            println!(
                "usage:\n  utxo-cli keygen\n  utxo-cli gen-examples [DIR]\n  \
                 utxo-cli demo [--base-url URL]"
            );
            std::process::exit(2);
        }
    }
}

fn write_json(path: &PathBuf, value: &serde_json::Value) {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).expect("create dir");
    }
    let pretty = serde_json::to_string_pretty(value).expect("serialize");
    fs::write(path, pretty).expect("write file");
    println!("wrote {}", path.display());
}

fn block_json(b: &Block) -> serde_json::Value {
    serde_json::to_value(b).expect("block to json")
}

fn cmd_keygen() {
    let w = Wallet::generate();
    println!(
        "{}",
        json!({
            "address": w.address_hex(),
            "secret": w.secret_hex(),
            "note": "secret is the 64-byte Ed25519 keypair (hex); keep it private",
        })
    );
}

// ---------------------------------------------------------------------------
// Offline example generation — all signatures and hashes are real.
// ---------------------------------------------------------------------------

fn cmd_gen_examples(dir: &str) {
    let dir = PathBuf::from(dir);
    fs::create_dir_all(&dir).expect("create examples dir");

    let miner = Wallet::generate();
    let alice = Wallet::generate();
    let bob = Wallet::generate();

    write_json(
        &dir.join("wallets.json"),
        &json!({
            "miner": { "address": miner.address_hex(), "secret": miner.secret_hex() },
            "alice": { "address": alice.address_hex(), "secret": alice.secret_hex() },
            "bob":   { "address": bob.address_hex(),   "secret": bob.secret_hex() },
        }),
    );

    // Wallet registry tracking each actor's spendable outputs.
    let mut wallets: HashMap<String, BuilderWallet> = HashMap::new();
    wallets.insert(
        miner.address_hex(),
        BuilderWallet::new(unsafe_wallet_clone(&miner)),
    );
    wallets.insert(
        alice.address_hex(),
        BuilderWallet::new(unsafe_wallet_clone(&alice)),
    );
    wallets.insert(
        bob.address_hex(),
        BuilderWallet::new(unsafe_wallet_clone(&bob)),
    );

    // Block 0: genesis, 50 to miner.
    let b0 = genesis(vec![TxOut {
        value: 50,
        address: miner.address_hex(),
    }]);
    let gtx = crypto::txid(&b0.txs[0]).unwrap();
    wallets
        .get_mut(&miner.address_hex())
        .unwrap()
        .utxos
        .push(UtxoRef {
            txid: gtx,
            vout: 0,
            value: 50,
        });
    let b0_hash = block_hash_of(&b0);
    write_json(&dir.join("block0-genesis.json"), &block_json(&b0));

    // Block 1: miner -> alice 30, change 15, fee 5. Coinbase = 55 to miner.
    let coin0 = wallets.get(&miner.address_hex()).unwrap().utxos[0].clone();
    let (pay, pay_id) = simple_transfer(
        wallets.get(&miner.address_hex()).unwrap(),
        &coin0,
        &alice.address_hex(),
        30,
        5,
    );
    wallets.get_mut(&miner.address_hex()).unwrap().utxos.clear();
    track_outputs(pay_id, &pay, &mut wallets);

    let b1 = build_block(
        1,
        b0_hash,
        1,
        vec![TxOut {
            value: 55,
            address: miner.address_hex(),
        }],
        vec![pay],
    );
    let b1_hash = block_hash_of(&b1);
    track_outputs(
        crypto::txid(&b1.txs[0]).unwrap(),
        &b1.txs[0],
        &mut wallets,
    );
    write_json(&dir.join("block1-transfer.json"), &block_json(&b1));

    // Block 2: alice -> bob 10, change 19, fee 1. Coinbase 51 to bob.
    let ac = wallets.get(&alice.address_hex()).unwrap().utxos[0].clone();
    let (pay2, pay2_id) = simple_transfer(
        wallets.get(&alice.address_hex()).unwrap(),
        &ac,
        &bob.address_hex(),
        10,
        1,
    );
    wallets.get_mut(&alice.address_hex()).unwrap().utxos.clear();
    track_outputs(pay2_id, &pay2, &mut wallets);

    let b2 = build_block(
        2,
        b1_hash,
        2,
        vec![TxOut {
            value: 51,
            address: bob.address_hex(),
        }],
        vec![pay2],
    );
    write_json(&dir.join("block2-transfer.json"), &block_json(&b2));

    // --- invalid examples (valid signatures/hashes, one rule broken each) --
    let bad = build_double_spend_block(
        b0_hash,
        &miner.address_hex(),
        gtx,
        &wallets.get(&miner.address_hex()).unwrap().wallet,
    );
    write_json(
        &dir.join("invalid-block1-double-spend.json"),
        &json!({
            "block": block_json(&bad.block),
            "expect_status": 409,
            "expect_error_contains": "double spend",
        }),
    );
    write_json(
        &dir.join("invalid-block1-bad-signature.json"),
        &json!({
            "block": block_json(&bad.bad_sig_block),
            "expect_status": 409,
            "expect_error_contains": "invalid Ed25519 signature",
        }),
    );
    let greedy = build_greedy_coinbase_block(
        b0_hash,
        &miner.address_hex(),
        gtx,
        &wallets.get(&miner.address_hex()).unwrap().wallet,
    );
    write_json(
        &dir.join("invalid-block1-greedy-coinbase.json"),
        &json!({
            "block": block_json(&greedy),
            "expect_status": 409,
            "expect_error_contains": "coinbase creates",
        }),
    );
    let ins = build_insufficient_block(
        b0_hash,
        &miner.address_hex(),
        gtx,
        &wallets.get(&miner.address_hex()).unwrap().wallet,
    );
    write_json(
        &dir.join("invalid-block1-insufficient.json"),
        &json!({
            "block": block_json(&ins),
            "expect_status": 409,
            "expect_error_contains": "input insufficient",
        }),
    );

    println!("example generation complete");
}

// Keys are only used in this short-lived offline generator; reconstructing a
// Keypair from its own secret bytes is a local copy, not key exfiltration.
fn unsafe_wallet_clone(w: &Wallet) -> Wallet {
    utxo_rollback::wallet::wallet_from_secret(&w.secret_hex()).expect("clone wallet")
}

fn block_hash_of(b: &Block) -> [u8; 32] {
    let merkle: [u8; 32] = hex::decode(&b.merkle_root).unwrap().try_into().unwrap();
    let prev: [u8; 32] = hex::decode(&b.prev_hash).unwrap().try_into().unwrap();
    crypto::hash256(&crypto::serialize_header(
        b.height, &prev, b.timestamp, &merkle,
    ))
}

fn req_err<E: std::fmt::Display>(e: E) -> String {
    e.to_string()
}

struct BadBlocks {
    block: Block,
    bad_sig_block: Block,
}

/// A block whose single non-coinbase tx spends the genesis coin twice. The
/// coinbase is made exact (50, fee 0) so the *only* reason for rejection is
/// the double spend; a second variant flips the signature byte.
fn build_double_spend_block(
    prev: [u8; 32],
    coinbase_to: &str,
    genesis_txid: [u8; 32],
    spender: &Wallet,
) -> BadBlocks {
    let mk_input = |vout: u32| TxIn {
        prev: Some(OutPoint {
            txid: hex::encode(genesis_txid),
            vout,
        }),
        coinbase_tag: String::new(),
        pubkey: spender.address_hex(),
        signature: String::new(),
    };
    let mut tx = Transaction {
        inputs: vec![mk_input(0), mk_input(0)],
        outputs: vec![TxOut {
            value: 50,
            address: spender.address_hex(),
        }],
    };
    let id = crypto::txid(&tx).unwrap();
    let sig = spender.sign_txid(&id);
    for i in &mut tx.inputs {
        i.signature = sig.clone();
    }

    let block = build_block(
        1,
        prev,
        1,
        vec![TxOut {
            value: 50,
            address: coinbase_to.to_string(),
        }],
        vec![tx.clone()],
    );

    // Flip the last byte of both signatures.
    let mut bad_sig = tx;
    for i in &mut bad_sig.inputs {
        let mut raw = hex::decode(&i.signature).unwrap();
        let last = raw.len() - 1;
        raw[last] ^= 0x01;
        i.signature = hex::encode(raw);
    }
    let bad_sig_block = build_block(
        1,
        prev,
        1,
        vec![TxOut {
            value: 50,
            address: coinbase_to.to_string(),
        }],
        vec![bad_sig],
    );

    BadBlocks {
        block,
        bad_sig_block,
    }
}

/// Coinbase claims 56 while fees are only 5 (inputs total 50, outputs 45).
fn build_greedy_coinbase_block(
    prev: [u8; 32],
    coinbase_to: &str,
    genesis_txid: [u8; 32],
    spender: &Wallet,
) -> Block {
    let coin = UtxoRef {
        txid: genesis_txid,
        vout: 0,
        value: 50,
    };
    let w = BuilderWallet {
        wallet: unsafe_wallet_clone(spender),
        utxos: vec![coin.clone()],
    };
    let (tx, _) = simple_transfer(&w, &coin, &spender.address_hex(), 45, 5);
    build_block(
        1,
        prev,
        1,
        vec![TxOut {
            value: 56,
            address: coinbase_to.to_string(),
        }],
        vec![tx],
    )
}

/// Spends 50 but creates 51: input insufficient (the signature over the
/// inflated tx is still valid because the txid commits to the outputs).
fn build_insufficient_block(
    prev: [u8; 32],
    coinbase_to: &str,
    genesis_txid: [u8; 32],
    spender: &Wallet,
) -> Block {
    let mut tx = Transaction {
        inputs: vec![TxIn {
            prev: Some(OutPoint {
                txid: hex::encode(genesis_txid),
                vout: 0,
            }),
            coinbase_tag: String::new(),
            pubkey: String::new(),
            signature: String::new(),
        }],
        outputs: vec![TxOut {
            value: 51,
            address: spender.address_hex(),
        }],
    };
    let id = crypto::txid(&tx).unwrap();
    tx.inputs[0].pubkey = spender.address_hex();
    tx.inputs[0].signature = spender.sign_txid(&id);

    build_block(
        1,
        prev,
        1,
        // coinbase exactly 50 — the spend tx itself is the failure.
        vec![TxOut {
            value: 50,
            address: coinbase_to.to_string(),
        }],
        vec![tx],
    )
}

// ---------------------------------------------------------------------------
// Live demo against a running server
// ---------------------------------------------------------------------------

fn post(client: &reqwest::blocking::Client, base: &str, path: &str, body: &Block) -> reqwest::Result<reqwest::blocking::Response> {
    client.post(format!("{base}{path}")).json(body).send()
}

fn cmd_demo(base: &str) -> Result<(), String> {
    let client = reqwest::blocking::Client::new();

    let health: serde_json::Value = client
        .get(format!("{base}/health"))
        .send()
        .map_err(|e| format!("connecting to {base}: {e}"))?
        .json()
        .map_err(|e| e.to_string())?;
    println!("[1] health: {health}");

    // Fresh-chain precondition.
    let tip: serde_json::Value = client
        .get(format!("{base}/chain/tip"))
        .send().map_err(req_err)?
        .json().map_err(req_err)?;
    if tip.get("empty") != Some(&json!(true)) {
        return Err(format!("server at {base} is not on an empty chain; use a fresh --db"));
    }

    let miner = Wallet::generate();
    let alice = Wallet::generate();
    let bob = Wallet::generate();

    // ---- block 0 ---------------------------------------------------------
    let b0 = genesis(vec![TxOut {
        value: 50,
        address: miner.address_hex(),
    }]);
    let b0_hash = block_hash_of(&b0);
    let r = post(&client, base, "/blocks", &b0).map_err(req_err)?.error_for_status().map_err(|e| format!("genesis rejected: {e}"))?;
    println!("[2] genesis accepted: {}", r.text().unwrap_or_default());

    // ---- block 1: miner -> alice 30, fee 5, coinbase 55 ------------------
    let coin0 = UtxoRef {
        txid: crypto::txid(&b0.txs[0]).unwrap(),
        vout: 0,
        value: 50,
    };
    let mut miner_w = BuilderWallet::new(miner);
    miner_w.utxos.push(coin0.clone());
    let (pay, pay_id) = simple_transfer(&miner_w, &coin0, &alice.address_hex(), 30, 5);
    let b1 = build_block(
        1,
        b0_hash,
        100,
        vec![TxOut {
            value: 55,
            address: miner_w.address(),
        }],
        vec![pay],
    );
    let r = post(&client, base, "/blocks", &b1).map_err(req_err)?.error_for_status().map_err(|e| format!("b1 rejected: {e}"))?;
    let v: serde_json::Value = r.json().map_err(req_err)?;
    println!("[3] block1 accepted: state_root={}", v["state_root"]);
    let b1_root_a = v["state_root"].as_str().unwrap().to_string();

    // ---- in-block double spend must be rejected --------------------------
    let genesis_txid = crypto::txid(&b0.txs[0]).unwrap();
    let bad = build_double_spend_block(b0_hash, &miner_w.address(), genesis_txid, &miner_w.wallet);
    let r = post(&client, base, "/blocks", &bad.block).map_err(req_err)?;
    if r.status() != reqwest::StatusCode::CONFLICT {
        return Err(format!("expected 409 for double spend, got {}", r.status()));
    }
    let body: serde_json::Value = r.json().map_err(req_err)?;
    let err = body["error"].as_str().unwrap_or("");
    if !err.contains("double spend") {
        return Err(format!("double-spend error text unexpected: {err}"));
    }
    println!("[4] in-block double spend rejected (409): {err}");

    // Tip must be unchanged at height 1.
    let tip: serde_json::Value = client.get(format!("{base}/chain/tip")).send().map_err(req_err)?.json().map_err(req_err)?;
    if tip["tip"]["height"] != json!(1) {
        return Err("tip moved after rejected block".into());
    }

    // ---- alternative block at height 1 (reorg, one block deep) -----------
    // alice never gets paid; miner pays bob 20 with fee 5 instead.
    let alt_pay_input = coin0;
    let (alt_tx, _alt_id) = simple_transfer(
        &BuilderWallet::new(unsafe_wallet_clone(&miner_w.wallet)),
        &alt_pay_input,
        &bob.address_hex(),
        20,
        5,
    );
    let alt1 = build_block(
        1,
        b0_hash,
        101,
        vec![TxOut {
            value: 55,
            address: miner_w.address(),
        }],
        vec![alt_tx],
    );
    let r = post(&client, base, "/blocks", &alt1).map_err(req_err)?.error_for_status().map_err(|e| format!("alt1 rejected: {e}"))?;
    let v: serde_json::Value = r.json().map_err(req_err)?;
    assert_eq!(v["reorged"], json!(true));
    println!(
        "[5] alternative block1 connected via reorg: {} (rolled back {:?})",
        v["hash"], v["disconnected_heights"]
    );

    // alice's old output must no longer exist; historical height 1 root is new.
    let hist: serde_json::Value = client
        .get(format!("{}/utxos/at/1", base))
        .send().map_err(req_err)?
        .json()
        .map_err(|e| e.to_string())?;
    if hist["state_root"] == json!(b1_root_a) {
        return Err("state root at height 1 did not change after reorg".into());
    }
    let has_alice = hist["utxos"]
        .as_array()
        .unwrap()
        .iter()
        .any(|u| u["address"] == json!(alice.address_hex()));
    if has_alice {
        return Err("reorg did not remove alice's old-chain output".into());
    }
    println!("[6] history at height 1 reflects the new chain, old UTXO gone");

    // ---- illegal alternative block: unknown parent -----------------------
    let bad_parent = build_block(
        1,
        [0x11u8; 32],
        200,
        vec![TxOut {
            value: 50,
            address: miner_w.address(),
        }],
        vec![],
    );
    let r = post(&client, base, "/blocks", &bad_parent).map_err(req_err)?;
    if r.status() != reqwest::StatusCode::CONFLICT {
        return Err(format!("expected 409 for unknown parent, got {}", r.status()));
    }
    println!(
        "[7] alternative block with unknown parent rejected: {}",
        r.json::<serde_json::Value>().map_err(req_err)?["error"]
    );

    // ---- explicit rollback ------------------------------------------------
    let r = client.post(format!("{base}/blocks/disconnect")).send().map_err(req_err)?;
    let v: serde_json::Value = r.json().map_err(|e| e.to_string())?;
    assert_eq!(v["new_tip_height"], json!(0));
    println!("[8] disconnected alt tip; new tip height 0: {}", v["state_root"]);

    // Reconnect canonical block1.
    let r = post(&client, base, "/blocks", &b1).map_err(req_err)?.error_for_status().map_err(|e| format!("reconnect b1: {e}"))?;
    let v: serde_json::Value = r.json().map_err(req_err)?;
    assert_eq!(v["state_root"], json!(b1_root_a), "reapplying block1 must reproduce the identical state root");
    println!("[9] reconnected canonical block1; state root reproduced exactly");

    // ---- block 2: alice -> bob 10, fee 1, coinbase 51 --------------------
    let mut all = HashMap::new();
    all.insert(
        alice.address_hex(),
        BuilderWallet {
            wallet: unsafe_wallet_clone(&alice),
            utxos: vec![UtxoRef {
                txid: pay_id,
                vout: 0,
                value: 30,
            }],
        },
    );
    let ac = all.get(&alice.address_hex()).unwrap().utxos[0].clone();
    let (pay2, _) = simple_transfer(all.get(&alice.address_hex()).unwrap(), &ac, &bob.address_hex(), 10, 1);
    let b2 = build_block(
        2,
        hex::decode(v["hash"].as_str().unwrap()).unwrap().try_into().unwrap(),
        102,
        vec![TxOut {
            value: 51,
            address: miner_w.address(),
        }],
        vec![pay2],
    );
    let _ = post(&client, base, "/blocks", &b2).map_err(req_err)?.error_for_status().map_err(|e| format!("b2 rejected: {e}"))?;
    println!("[10] block2 accepted");

    // ---- historical query must be explicit and correct -------------------
    let cur: serde_json::Value = client.get(format!("{base}/utxos")).send().map_err(req_err)?.json().map_err(req_err)?;
    let at1: serde_json::Value = client.get(format!("{base}/utxos/at/1")).send().map_err(req_err)?.json().map_err(req_err)?;
    if cur["state_root"] == at1["state_root"] {
        return Err("current and height-1 roots unexpectedly equal after block2".into());
    }
    println!(
        "[11] current root {} differs from explicit historical root at 1 {}",
        cur["state_root"], at1["state_root"]
    );

    // ---- naive replay cross-check ----------------------------------------
    let report: serde_json::Value = client
        .get(format!("{base}/debug/replay"))
        .send().map_err(req_err)?
        .json()
        .map_err(|e| e.to_string())?;
    if report["roots_match"] != json!(true) {
        return Err(format!("naive replay mismatch: {report}"));
    }
    println!(
        "[12] naive replay matches storage at every height: {} blocks, {} utxos, supply {}",
        report["blocks_replayed"], report["utxo_count"], report["total_supply"]
    );

    println!("DEMO OK");
    Ok(())
}
