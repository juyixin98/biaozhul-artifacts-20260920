//! Router tests.
//!
//! The core correctness argument is tested exhaustively on small random
//! graphs: for each graph and query, three independent engines must agree —
//!
//! 1. pruned DFS (`search`),
//! 2. unpruned DFS (`search_without_prune`),
//! 3. brute-force enumeration of every non-repeating pool sequence, each
//!    simulated independently from scratch with [`Graph::simulate`].
//!
//! They must report the same number of feasible sequences and the same
//! winner (pool-id tuple + final net output). Hand-written scenarios on top
//! cover the required cases by name: cycles, differing precision, multiple
//! equivalent paths (tie by pool id) and insufficient liquidity.

use super::*;
use crate::model::Pool;

fn pool(id: &str, t0: &str, t1: &str, r0: u128, r1: u128, fee: u32) -> Pool {
    Pool {
        id: id.to_string(),
        token0: t0.to_string(),
        token1: t1.to_string(),
        reserve0: r0,
        reserve1: r1,
        fee_bps: fee,
    }
}

fn graph(tokens: &[&str], pools: Vec<Pool>) -> Graph {
    let t: Vec<String> = tokens.iter().map(|s| s.to_string()).collect();
    Graph::build(pools, &t)
}

type Feasible = Vec<(Vec<String>, u128)>;

/// Reference: simulate every brute-force sequence independently.
/// Returns:
/// * feasible: pool-id tuples whose full chain executes with positive net,
///   with final net output;
/// * considered: syntactic target-reaching sequences whose *every proper
///   prefix* executes — exactly what the DFS is capable of counting given it
///   stops extending a dead prefix;
/// * winner tuple under the selection rules.
fn brute_force_evaluate(
    g: &Graph,
    tin: &str,
    tout: &str,
    amount: u128,
    cost: u128,
    hops: usize,
) -> (Feasible, usize, Option<Vec<String>>) {
    let start = g.token_idx(tin).unwrap();
    let seqs = g.brute_force_sequences(tin, tout, hops);
    let mut feasible = Vec::new();
    let mut considered = 0;
    for s in &seqs {
        let mut prefixes_alive = true;
        for k in 1..s.len() {
            if g.simulate(&s[..k], start, amount, cost).is_err() {
                prefixes_alive = false;
                break;
            }
        }
        if prefixes_alive {
            considered += 1;
        }
        if let Ok(hs) = g.simulate(s, start, amount, cost) {
            let key: Vec<String> = hs.iter().map(|h| h.pool_id.clone()).collect();
            let out = hs.last().unwrap().net_amount_out;
            feasible.push((key, out));
        }
    }
    let winner = feasible
        .iter()
        .reduce(|a, b| match a.1.cmp(&b.1) {
            std::cmp::Ordering::Greater => a,
            std::cmp::Ordering::Less => b,
            std::cmp::Ordering::Equal => {
                if a.0.iter().cmp(b.0.iter()) == std::cmp::Ordering::Less {
                    a
                } else {
                    b
                }
            }
        })
        .map(|(k, _)| k.clone());
    (feasible, considered, winner)
}

fn winner_key(route: &BestRoute) -> Vec<String> {
    route.hops.iter().map(|h| h.pool_id.clone()).collect()
}

/// Compare pruned DFS, unpruned DFS and brute force on one (graph, query).
fn assert_engines_agree(
    g: &Graph,
    tin: &str,
    tout: &str,
    amount: u128,
    cost: u128,
    hops: usize,
) {
    let pruned = g.search(tin, tout, amount, cost, hops);
    let unpruned = g.search_without_prune(tin, tout, amount, cost, hops);
    let (feasible, considered_ref, bf_winner) =
        brute_force_evaluate(g, tin, tout, amount, cost, hops);

    match (&pruned, &unpruned) {
        (Ok(a), Ok(b)) => {
            // Both DFS variants must count identically — the reachability
            // table only removes provably dead branches.
            assert_eq!(
                a.paths_considered, b.paths_considered,
                "pruned/unpruned considered differ for {tin}->{tout}"
            );
            assert_eq!(a.feasible_paths, b.feasible_paths);
            assert_eq!(
                a.paths_considered, considered_ref,
                "DFS considered count must equal live-prefix brute-force count for {tin}->{tout}"
            );
            assert_eq!(
                a.feasible_paths,
                feasible.len(),
                "DFS feasible count must equal brute force for {tin}->{tout}"
            );
            assert_eq!(winner_key(a), winner_key(b));
            assert_eq!(bf_winner, Some(winner_key(a)), "brute force winner mismatch");
            assert_eq!(
                a.hops.last().unwrap().net_amount_out,
                b.hops.last().unwrap().net_amount_out
            );
        }
        (Err(ea), Err(eb)) => {
            assert_eq!(ea, eb);
            assert!(bf_winner.is_none(), "brute force found a route DFS missed");
            assert!(feasible.is_empty());
        }
        _ => panic!("pruned/unpruned disagree: {pruned:?} vs {unpruned:?}"),
    }
}

#[test]
fn direct_one_hop_uses_pool_math() {
    let g = graph(
        &["A", "B"],
        vec![pool("p1", "A", "B", 1_000_000, 2_000_000, 30)],
    );
    let r = g.search("A", "B", 100_000, 0, 3).unwrap();
    assert_eq!(r.hops.len(), 1);
    assert_eq!(r.hops[0].pool_id, "p1");
    // floor(100_000*9970*2_000_000 / (1_000_000*10_000 + 100_000*9970))
    let expected = 100_000u128
        .checked_mul(9970).unwrap()
        .checked_mul(2_000_000).unwrap()
        / (1_000_000u128 * 10_000 + 100_000 * 9970);
    assert_eq!(r.hops[0].net_amount_out, expected);
    assert_eq!(r.paths_considered, 1);
    assert_eq!(r.feasible_paths, 1);
}

#[test]
fn two_hop_feeds_floored_output_into_next_hop() {
    let g = graph(
        &["A", "B", "C"],
        vec![
            pool("pAB", "A", "B", 1_000_000, 2_000_000, 30),
            pool("pBC", "B", "C", 3_000_000, 4_000_000, 50),
        ],
    );
    let r = g.search("A", "C", 100_000, 0, 3).unwrap();
    assert_eq!(r.hops.len(), 2);
    let mid = crate::amm::get_amount_out(100_000, 1_000_000, 2_000_000, 30).unwrap();
    let final_out =
        crate::amm::get_amount_out(mid.amount_out, 3_000_000, 4_000_000, 50).unwrap();
    assert_eq!(r.hops[0].amount_in, 100_000);
    assert_eq!(r.hops[0].net_amount_out, mid.amount_out);
    assert_eq!(r.hops[1].amount_in, mid.amount_out);
    assert_eq!(r.hops[1].net_amount_out, final_out.amount_out);
}

#[test]
fn cycles_are_explored_not_special_cased() {
    // Triangle A-B-C plus TWO parallel pools between B and C (like two
    // fee tiers). This makes a genuine token-revisiting 3-hop sequence to
    // the target possible: A->C->B->C uses distinct pools but revisits C.
    let g = graph(
        &["A", "B", "C"],
        vec![
            pool("pAB", "A", "B", 1_000_000_000, 1_000_000_000, 30),
            pool("pBC1", "B", "C", 1_000_000_000, 1_000_000_000, 30),
            pool("pBC2", "B", "C", 1_000_000_000, 1_000_000_000, 30),
            pool("pCA", "C", "A", 1_000_000_000, 1_000_000_000, 30),
        ],
    );
    let r = g.search("A", "C", 100_000, 0, 3).unwrap();
    // Direct one-fee pool beats every multi-hop cyclic route.
    assert_eq!(winner_key(&r), vec!["pCA"]);
    // Enumerator and DFS must agree — including cyclic sequences that
    // revisit the target token mid-path.
    assert_engines_agree(&g, "A", "C", 100_000, 0, 3);
    let seqs = g.brute_force_sequences("A", "C", 3);
    // [pCA], [pAB,pBC1], [pAB,pBC2], [pCA,pBC1,pBC2], [pCA,pBC2,pBC1]
    assert_eq!(seqs.len(), 5, "cyclic sequences must be enumerated");
}

#[test]
fn differing_precision_is_just_metadata() {
    // 6-decimal stable vs 18-decimal token: reserves live in raw units and
    // no decimal scaling is invented. The router trusts the snapshot.
    let g = graph(
        &["USDC", "WETH"],
        vec![pool(
            "uni-v2",
            "USDC",
            "WETH",
            5_000_000_000_000, // 5,000,000 USDC (6 dp)
            25_000_000_000_000_000_000_000, // 25,000 WETH (18 dp)
            5,
        )],
    );
    let r = g.search("USDC", "WETH", 1_000_000_000, 0, 1).unwrap();
    // Cross-check the exact integer by calling the pool math directly.
    let direct = crate::amm::get_amount_out(
        1_000_000_000,
        5_000_000_000_000,
        25_000_000_000_000_000_000_000,
        5,
    )
    .unwrap();
    assert_eq!(r.hops[0].gross_amount_out, direct.amount_out);
}

#[test]
fn equivalent_paths_tie_break_on_pool_id_tuple() {
    // Two disjoint 2-hop routes A->X->B and A->Y->B with symmetric pools
    // that produce byte-identical integer outputs.
    let big = 1_000_000_000_000u128;
    let g = graph(
        &["A", "X", "Y", "B"],
        vec![
            pool("pAX", "A", "X", big, big, 0),
            pool("pXB", "X", "B", big, big, 0),
            pool("pAY", "A", "Y", big, big, 0),
            pool("pYB", "Y", "B", big, big, 0),
        ],
    );
    let r = g.search("A", "B", 123_456, 0, 2).unwrap();
    // pAX,pXB vs pAY,pYB: equal output, lexicographically smaller wins.
    assert_eq!(winner_key(&r), vec!["pAX", "pXB"]);
    assert_eq!(r.feasible_paths, 2);
}

#[test]
fn tie_break_compares_first_pool_id_first() {
    // One-hop pZ vs two-hop [pA, pB]: same final output possible? Hard to
    // force; instead verify the comparator logic directly via crafted
    // RouteHop values.
    let mk = |ids: &[&str], out: u128| -> Vec<RouteHop> {
        ids.iter()
            .map(|id| RouteHop {
                pool_id: id.to_string(),
                token_in: String::new(),
                token_out: String::new(),
                amount_in: 0,
                reserve_in: 0,
                reserve_out: 0,
                fee_bps: 0,
                gross_amount_out: out,
                explicit_cost: 0,
                net_amount_out: out,
            })
            .collect()
    };
    let a = mk(&["pA", "pZ"], 100);
    let b = mk(&["pB", "pA"], 100);
    assert!(route_better(&a, &b));
    let c = mk(&["pC"], 99);
    assert!(route_better(&a, &c)); // output dominates pool-id order
}

#[test]
fn insufficient_liquidity_dust_gives_zero_output() {
    // Tiny input into a huge pool floors to zero -> no feasible route.
    let g = graph(
        &["A", "B"],
        vec![pool("p1", "A", "B", 1_000_000_000_000_000_000, 1_000_000_000_000_000_000, 30)],
    );
    let err = g.search("A", "B", 1, 0, 3).unwrap_err();
    assert_eq!(err, RouteError::NoFeasiblePath);
}

#[test]
fn no_path_within_hop_limit() {
    let g = graph(
        &["A", "B", "C", "D"],
        vec![
            pool("p1", "A", "B", 100, 100, 0),
            pool("p2", "B", "C", 100, 100, 0),
            pool("p3", "C", "D", 100, 100, 0),
        ],
    );
    // A->D needs 3 hops and is feasible at 3.
    assert!(g.search("A", "D", 10, 0, 3).is_ok());
    // At 2 hops only paths up to C exist; D unreachable -> NoPath.
    assert_eq!(
        g.search("A", "D", 10, 0, 2).unwrap_err(),
        RouteError::NoPath
    );
    // Disconnected token.
    assert_eq!(
        g.search("A", "ZZ", 10, 0, 3).unwrap_err(),
        RouteError::UnknownToken("ZZ".to_string())
    );
}

#[test]
fn explicit_cost_per_hop_reduces_carry_and_can_kill_route() {
    let g = graph(
        &["A", "B", "C"],
        vec![
            pool("pAB", "A", "B", 1_000_000, 1_000_000, 0),
            pool("pBC", "B", "C", 1_000_000, 1_000_000, 0),
        ],
    );
    // 1000 A -> 999 B (floor) -> with cost 999 on hop 1 the carry is 0,
    // route dies; with smaller cost it survives.
    let no_cost = g.search("A", "C", 1000, 0, 3).unwrap();
    let mid = no_cost.hops[0].gross_amount_out;
    assert_eq!(mid, 999);

    let killed = g.search("A", "C", 1000, mid, 3).unwrap_err();
    assert_eq!(killed, RouteError::NoFeasiblePath);

    let with_cost = g.search("A", "C", 1000, 100, 3).unwrap();
    assert_eq!(with_cost.hops[0].net_amount_out, mid - 100);
    // Hop 2 trades exactly the deducted carry.
    let expected2 =
        crate::amm::get_amount_out(mid - 100, 1_000_000, 1_000_000, 0).unwrap();
    assert_eq!(with_cost.hops[1].amount_in, mid - 100);
    assert_eq!(
        with_cost.hops[1].gross_amount_out,
        expected2.amount_out
    );
    assert_eq!(
        with_cost.hops[1].net_amount_out,
        expected2.amount_out - 100
    );
}

#[test]
fn cost_routing_prefers_route_with_larger_net_output() {
    // Direct 1-hop vs 2-hop where the second hop is cheap and liquidity is
    // deeper; with a per-hop cost the 2-hop pays cost twice and must lose
    // once cost is large enough.
    let g = graph(
        &["A", "B", "C"],
        vec![
            pool("pDirect", "A", "C", 1_000_000, 1_000_000, 100),
            pool("pAB", "A", "B", 1_000_000, 1_000_000, 0),
            pool("pBC", "B", "C", 1_000_000, 1_000_000, 0),
        ],
    );
    // Zero fee on the 2-hop path tends to beat the 1% direct path... verify
    // both engines simply agree across a range of costs.
    for cost in [0u128, 1, 10, 100, 1000] {
        assert_engines_agree(&g, "A", "C", 50_000, cost, 3);
    }
    let cheap = g.search("A", "C", 50_000, 0, 3).unwrap();
    let expensive = g.search("A", "C", 50_000, 1000, 3).unwrap();
    assert!(
        expensive.hops.last().unwrap().net_amount_out
            <= cheap.hops.last().unwrap().net_amount_out
    );
}

#[test]
fn overflowing_amounts_are_explicitly_rejected() {
    // Reserves near u128::MAX: input*reserve products overflow the 256-bit
    // intermediate. The only path structurally exists, so the router must
    // surface OVERFLOW rather than pretend there is no liquidity.
    let big = u128::MAX - 1;
    let g = graph(
        &["A", "B"],
        vec![pool("p1", "A", "B", big, big, 30)],
    );
    assert_eq!(
        g.search("A", "B", big, 0, 3).unwrap_err(),
        RouteError::Overflow
    );
    // Unpruned search must agree.
    assert_eq!(
        g.search_without_prune("A", "B", big, 0, 3).unwrap_err(),
        RouteError::Overflow
    );
}

#[test]
fn same_token_and_bad_args_rejected() {
    let g = graph(&["A"], vec![]);
    assert_eq!(g.search("A", "A", 1, 0, 3).unwrap_err(), RouteError::SameToken);
    let g2 = graph(
        &["A", "B"],
        vec![pool("p1", "A", "B", 100, 100, 0)],
    );
    assert_eq!(g2.search("A", "B", 0, 0, 3).unwrap_err(), RouteError::ZeroAmount);
    assert_eq!(g2.search("A", "B", 1, 0, 0).unwrap_err(), RouteError::BadMaxHops);
    assert_eq!(g2.search("A", "B", 1, 0, 4).unwrap_err(), RouteError::BadMaxHops);
}

// ---------- randomized exhaustive equivalence ----------

/// Tiny deterministic xorshift64* — no dev-dependency needed.
struct Rng(u64);
impl Rng {
    fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x >> 12;
        x ^= x << 25;
        x ^= x >> 27;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }
    fn below(&mut self, n: u64) -> usize {
        (self.next_u64() % n) as usize
    }
}

#[test]
fn randomized_small_graphs_prune_matches_brute_force() {
    // 400 seed cases. Each graph: <=5 tokens, <=7 pools (random pairs,
    // reserves, fees); queries over every ordered token pair, costs and
    // inputs varied; hop limits 1..=3.
    for seed in 0..400u64 {
        let mut rng = Rng(seed.wrapping_mul(6364136223846793005).wrapping_add(1));
        let ntokens = 3 + rng.below(3); // 3..5
        let tokens: Vec<String> = (0..ntokens).map(|i| format!("T{i}")).collect();
        // A simple graph has at most ntokens*(ntokens-1)/2 distinct pairs.
        let max_pairs = ntokens * (ntokens - 1) / 2;
        let npools = 1 + rng.below(max_pairs as u64);
        let mut pools = Vec::new();
        let mut used_pairs = std::collections::HashSet::new();
        let mut pid = 0;
        while pools.len() < npools {
            let a = rng.below(ntokens as u64);
            let mut b = rng.below(ntokens as u64);
            if a == b {
                b = (b + 1) % ntokens;
            }
            let key = if a < b { (a, b) } else { (b, a) };
            if !used_pairs.insert(key) {
                continue;
            }
            // Reserves span magnitudes so some routes floor to zero.
            let mag0 = 1 + rng.below(20) as u32;
            let mag1 = 1 + rng.below(20) as u32;
            let r0 = 10u128.saturating_pow(mag0) * (1 + rng.below(9) as u128);
            let r1 = 10u128.saturating_pow(mag1) * (1 + rng.below(9) as u128);
            let fee = (rng.below(6) as u32) * 10; // 0..=50 bps
            pools.push(pool(
                &format!("pool-{pid:02}"),
                &tokens[a],
                &tokens[b],
                r0,
                r1,
                fee,
            ));
            pid += 1;
        }
        let g = Graph::build(pools, &tokens);

        for i in 0..ntokens {
            for j in 0..ntokens {
                if i == j {
                    continue;
                }
                for &amount in &[1u128, 1_000, 1_000_000, 1_000_000_000] {
                    for &cost in &[0u128, 1, 50] {
                        for hops in 1..=3usize {
                            assert_engines_agree(
                                &g,
                                &tokens[i],
                                &tokens[j],
                                amount,
                                cost,
                                hops,
                            );
                        }
                    }
                }
            }
        }
    }
}
