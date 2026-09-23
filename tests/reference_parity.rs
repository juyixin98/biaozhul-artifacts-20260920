//! Differential verification of pruning correctness.
//!
//! [`best_route`] uses an optimistic bound to discard DFS branches. Here we
//! fuzz many small random graphs and compare it against
//! [`reference::best_route_exhaustive`], which enumerates *every* distinct-pool
//! edge sequence of length 1..=3 with no pruning at all. Both must agree on
//! (output amount, winning pool-id sequence) for every query.
//!
//! The RNG is a hand-rolled deterministic xorshift so the suite needs no
//! external crates and failures are reproducible from the printed seed.

use p016_quote_router::amount::Amount;
use p016_quote_router::model::{Asset, Pool, Snapshot};
use p016_quote_router::router::{best_route, reference::best_route_exhaustive};

struct Rng(u64);

impl Rng {
    fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x
    }
    fn below(&mut self, n: u64) -> usize {
        (self.next_u64() % n) as usize
    }
    fn range(&mut self, lo: u64, hi: u64) -> u64 {
        lo + self.next_u64() % (hi - lo + 1)
    }
}

struct GenConfig {
    n_tokens: usize,
    n_pools: usize,
    reserve_max: u128,
    cost_max: u128,
}

fn random_snapshot(rng: &mut Rng, cfg: &GenConfig) -> Snapshot {
    let mut assets = Vec::new();
    for i in 0..cfg.n_tokens {
        assets.push(Asset {
            id: format!("T{i}"),
            decimals: rng.below(256) as u8,
        });
    }

    let mut pools = Vec::new();
    let mut made = 0;
    let mut attempts = 0;
    while made < cfg.n_pools && attempts < cfg.n_pools * 10 {
        attempts += 1;
        let a = rng.below(cfg.n_tokens as u64);
        let b = rng.below(cfg.n_tokens as u64);
        if a == b {
            continue;
        }
        pools.push(Pool {
            id: format!("pool{made:03}"),
            token0: format!("T{a}"),
            token1: format!("T{b}"),
            reserve0: Amount::from(rng.range(1, cfg.reserve_max as u64) as u128),
            reserve1: Amount::from(rng.range(1, cfg.reserve_max as u64) as u128),
            fee_bps: [0u32, 1, 10, 30, 100, 500, 1000, 9999][rng.below(8)],
            cost_token0_out: Amount::from(rng.range(0, cfg.cost_max as u64) as u128),
            cost_token1_out: Amount::from(rng.range(0, cfg.cost_max as u64) as u128),
        });
        made += 1;
    }
    Snapshot { assets, pools }
}

fn agree(snap: &Snapshot, ain: &str, aout: &str, amt: Amount) -> bool {
    let pruned = best_route(snap, ain, aout, amt);
    let exhaustive = best_route_exhaustive(snap, ain, aout, amt);
    match (pruned, exhaustive) {
        (Err(_), None) => true,
        (Ok(r), Some((pidx, out))) => {
            if r.amount_out != out {
                eprintln!(
                    "OUTPUT MISMATCH: pruned={} exhaustive={} for {ain}->{aout} amt={amt}",
                    r.amount_out, out
                );
                return false;
            }
            let expected_ids: Vec<String> = pidx.iter().map(|i| snap.pools[*i].id.clone()).collect();
            if r.pool_path != expected_ids {
                eprintln!(
                    "TIE-BREAK MISMATCH: pruned={:?} exhaustive={:?}",
                    r.pool_path, expected_ids
                );
                return false;
            }
            // Independently re-execute the winning hops.
            let mut amount = amt;
            let mut token = ain.to_string();
            for pid in &r.pool_path {
                let pool = snap.pools.iter().find(|p| &p.id == pid).unwrap();
                let side = if token == pool.token0 {
                    p016_quote_router::swap::Side::ZeroToOne
                } else {
                    p016_quote_router::swap::Side::OneToZero
                };
                let hop = p016_quote_router::swap::swap_hop(pool, side, amount).unwrap();
                amount = hop.net_out;
                token = hop.asset_out;
            }
            if token != aout || amount != r.amount_out {
                eprintln!("winning route does not reproduce claimed output");
                return false;
            }
            true
        }
        (Err(e), Some(_)) => {
            eprintln!("pruned reported {e:?} but exhaustive found a route");
            false
        }
        (Ok(r), None) => {
            eprintln!("pruned found {} but exhaustive found nothing", r.amount_out);
            false
        }
    }
}

#[test]
fn pruned_search_matches_brute_force_on_random_small_graphs() {
    let seed = 0x1234_5678_9abc_def0u64;
    let mut rng = Rng(seed);
    let mut runs = 0usize;
    let mut prunes_observed = 0usize;

    for _ in 0..600 {
        let cfg = GenConfig {
            n_tokens: 3 + rng.below(5), // 3..7 tokens
            n_pools: 2 + rng.below(7),  // 2..8 pools
            reserve_max: 5_000,
            cost_max: 50,
        };
        let snap = random_snapshot(&mut rng, &cfg);
        // Query a handful of ordered pairs.
        for _ in 0..6 {
            let a = rng.below(cfg.n_tokens as u64);
            let b = rng.below(cfg.n_tokens as u64);
            let amt = Amount::from(rng.range(1, 2_000) as u128);
            let ain = format!("T{a}");
            let aout = format!("T{b}");
            runs += 1;
            if let Ok(r) = best_route(&snap, &ain, &aout, amt) {
                if r.stats.branches_pruned > 0 {
                    prunes_observed += 1;
                }
            }
            assert!(agree(&snap, &ain, &aout, amt), "seed={seed:#x}");
        }
    }
    // Sanity: the guard is actually doing work, otherwise this suite would be
    // vacuous.
    assert!(prunes_observed > 50, "expected many queries to prune, got {prunes_observed}");
    eprintln!("parity: {runs} queries against exhaustive enumeration; {prunes_observed} pruned");
}

/// Hand-built adversarial graph: a tempting short dead-ish path must not let
/// the bound prune a deeper route that is actually superior.
#[test]
fn pruning_keeps_deep_optimal_when_short_path_looks_good() {
    fn p(id: &str, t0: &str, t1: &str, r0: u128, r1: u128, fee: u32) -> Pool {
        Pool {
            id: id.into(),
            token0: t0.into(),
            token1: t1.into(),
            reserve0: Amount::from(r0),
            reserve1: Amount::from(r1),
            fee_bps: fee,
            cost_token0_out: Amount::ZERO,
            cost_token1_out: Amount::ZERO,
        }
    }
    // A->B direct looks okay: in 100, fee 0, 1000/1000 -> 90.
    // Deep: A->X (1000/100000 -> 9900 X), X->Y (100000/100000 -> 9900/2? )
    // construct deep output > 90 of B:
    //   A->X: reserves (1000 A, 1_000_000 X), in 100 -> floor(100*1e6/1100)=90909
    //   X->Y: (1_000_000 X, 1_000_000 Y), in 90909 -> 83333
    //   Y->B: (100_000 Y, 100_000 B), in 83333 -> floor(83333*1e5/183333)=45454
    let snap = Snapshot {
        assets: ["A", "B", "X", "Y"].map(|t| Asset { id: t.into(), decimals: 18 }).to_vec(),
        pools: vec![
            p("pAB", "A", "B", 1000, 1000, 0),
            p("pAX", "A", "X", 1000, 1_000_000, 0),
            p("pXY", "X", "Y", 1_000_000, 1_000_000, 0),
            p("pYB", "Y", "B", 100_000, 100_000, 0),
        ],
    };
    let r = best_route(&snap, "A", "B", Amount::from(100u64)).unwrap();
    let reference = best_route_exhaustive(&snap, "A", "B", Amount::from(100u64)).unwrap();
    assert_eq!(r.pool_path, vec!["pAX", "pXY", "pYB"]);
    assert_eq!(r.amount_out.value(), 45_454);
    assert_eq!(r.amount_out.value(), reference.1.value());
    assert!(r.stats.branches_pruned >= 1, "deep graph should prune at least a branch");
}

/// A near-limit query where every real-fraction bound is optimistic and only
/// floor rounding decides — pruning must still never drop the rounded winner.
#[test]
fn parity_with_tiny_reserves_and_fees() {
    let cfg = GenConfig {
        n_tokens: 5,
        n_pools: 7,
        reserve_max: 30,
        cost_max: 2,
    };
    let mut rng = Rng(0xdead_beef_cafe_babe);
    for _ in 0..60 {
        let snap = random_snapshot(&mut rng, &cfg);
        for a in 0..5u64 {
            for b in 0..5u64 {
                let amt = Amount::from(rng.range(1, 40) as u128);
                assert!(
                    agree(&snap, &format!("T{a}"), &format!("T{b}"), amt),
                    "tiny-reserve mismatch"
                );
            }
        }
    }
}
