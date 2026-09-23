//! Router-level scenarios required by the spec:
//! cycles, mixed precisions, multiple equivalent routes (tie-breaking),
//! insufficient liquidity, explicit costs and 3-hop composition.

use p016_quote_router::amount::Amount;
use p016_quote_router::model::{Asset, Pool, Snapshot};
use p016_quote_router::router::{best_route, RouteError, MAX_HOPS};
use p016_quote_router::swap::Side;

fn asset(id: &str, decimals: u8) -> Asset {
    Asset {
        id: id.into(),
        decimals,
    }
}

#[allow(clippy::too_many_arguments)]
fn pool(
    id: &str,
    t0: &str,
    t1: &str,
    r0: u128,
    r1: u128,
    fee_bps: u32,
    cost0: u128,
    cost1: u128,
) -> Pool {
    Pool {
        id: id.into(),
        token0: t0.into(),
        token1: t1.into(),
        reserve0: Amount::from(r0),
        reserve1: Amount::from(r1),
        fee_bps,
        cost_token0_out: Amount::from(cost0),
        cost_token1_out: Amount::from(cost1),
    }
}

#[test]
fn prefers_two_hop_route_with_actual_chained_math() {
    // Direct A->B is shallow and takes 3%:
    //   floor(100*9700*1000/(1000*10000 + 970000)) = 88.
    // The deep two-hop route wins with chained integer floors:
    //   A->C (1e18/1e18, 0bps): floor(100*1e18/(1e18+100)) = 99
    //   C->B (1e18/1e18, 0bps): floor(99*1e18/(1e18+99))  = 98
    let deep = 1_000_000_000_000_000_000u128;
    let snap = Snapshot {
        assets: vec![asset("A", 18), asset("B", 6), asset("C", 8)],
        pools: vec![
            pool("pAB", "A", "B", 1000, 1000, 300, 0, 0),
            pool("pAC", "A", "C", deep, deep, 0, 0, 0),
            pool("pCB", "C", "B", deep, deep, 0, 0, 0),
        ],
    };
    let r = best_route(&snap, "A", "B", Amount::from(100u64)).unwrap();
    assert_eq!(r.pool_path, vec!["pAC", "pCB"]);
    assert_eq!(r.token_path, vec!["A", "C", "B"]);
    assert_eq!(r.hops.len(), 2);
    assert_eq!(r.hops[0].net_out.value(), 99);
    assert_eq!(r.hops[1].net_out.value(), 98);
    assert_eq!(r.amount_out.value(), 98);
    assert!(r.hops[1].basis.contains("floor"));

    // Sanity-check the direct route independently: it really only gives 88.
    let direct = p016_quote_router::swap::swap_hop(
        &snap.pools[0],
        Side::ZeroToOne,
        Amount::from(100u64),
    )
    .unwrap();
    assert_eq!(direct.net_out.value(), 88);
}

#[test]
fn marginal_price_multiplication_would_overstate_integer_result() {
    // Two hops 1000/1000 and 3000/3000, zero fee:
    //   hop1: floor(100*1000/1100) = 90
    //   hop2: floor(90 *3000/3090) = 87
    // Multiplying marginal real ratios gives 100*(1000/1100)*(3000/3090)
    // = 87.93..., which rounds to 88. Chained integer floors must report 87.
    let snap = Snapshot {
        assets: vec![asset("A", 18), asset("B", 18), asset("C", 18)],
        pools: vec![
            pool("pAC", "A", "C", 1000, 1000, 0, 0, 0),
            pool("pCB", "C", "B", 3000, 3000, 0, 0, 0),
        ],
    };
    let r = best_route(&snap, "A", "B", Amount::from(100u64)).unwrap();
    assert_eq!(r.amount_out.value(), 87);
}

#[test]
fn tie_broken_by_lexicographically_smallest_pool_id_sequence() {
    // Two routes with exactly equal integer output (500):
    //  direct  pAB:           floor(1000*5500/(1000+1000)) = 2750?? -> instead
    // build reserves so both yield 500 exactly:
    //   pAB: x=y=1000, in 1000 -> 500
    //   pAC (1000/1000) -> 500 ; pCB (1000/1000) -> floor(500*1000/1500)=333
    // To make the 2-hop also 500, pick pCB with huge reserves so fee-less
    // swap of 500 returns 500 exactly... impossible with finite reserves.
    // Instead make pAB output equal the 2-hop output (333):
    //   pAB: in=1000, floor(1000*y/(x+1000)) = 333 with x=y=1000 -> 500.
    // Adjust: use x=2000,y=1000 -> floor(1000*1000/3000)=333.
    let snap = Snapshot {
        assets: vec![asset("A", 18), asset("B", 18), asset("C", 18)],
        pools: vec![
            pool("pAB", "A", "B", 2000, 1000, 0, 0, 0),
            pool("pAC", "A", "C", 1000, 1000, 0, 0, 0),
            pool("pCB", "C", "B", 1000, 1000, 0, 0, 0),
        ],
    };
    let r = best_route(&snap, "A", "B", Amount::from(1000u64)).unwrap();
    assert_eq!(r.amount_out.value(), 333);
    assert_eq!(r.pool_path, vec!["pAB"]); // vs ["pAC","pCB"], both 333
}

#[test]
fn tie_break_uses_pool_id_sequence_even_when_second_id_orders_opposite() {
    // Two equivalent 2-hop routes with identical integer output:
    //   ["p1","z"]: A->X->B
    //   ["p2","a"]: A->Y->B
    // All pools are symmetric 1000/1000 zero-fee, so in=100 -> 90 -> 82 on
    // both routes. The first pool id ("p1" < "p2") decides, even though the
    // second id "z" > "a".
    let snap = Snapshot {
        assets: vec![asset("A", 18), asset("B", 18), asset("X", 18), asset("Y", 18)],
        pools: vec![
            pool("p1", "A", "X", 1000, 1000, 0, 0, 0),
            pool("p2", "A", "Y", 1000, 1000, 0, 0, 0),
            pool("a", "Y", "B", 1000, 1000, 0, 0, 0),
            pool("z", "X", "B", 1000, 1000, 0, 0, 0),
        ],
    };
    let r = best_route(&snap, "A", "B", Amount::from(100u64)).unwrap();
    assert_eq!(r.amount_out.value(), 82);
    assert_eq!(r.pool_path, vec!["p1", "z"]);
}

#[test]
fn cycles_are_enumerated_but_cannot_beat_a_direct_path_here() {
    // Triangle with a direct pool and a looped alternative.
    let snap = Snapshot {
        assets: vec![asset("A", 18), asset("B", 18), asset("C", 18)],
        pools: vec![
            pool("pAB", "A", "B", 1_000_000, 1_000_000, 0, 0, 0),
            pool("pBC", "B", "C", 1_000_000, 1_000_000, 0, 0, 0),
            pool("pCA", "C", "A", 1_000_000, 1_000_000, 0, 0, 0),
        ],
    };
    // A->B direct with 100: floor(100*1e6/1_000_100)=99. A->B via a cycle
    // is only possible by reusing pools (forbidden), so direct wins.
    let r = best_route(&snap, "A", "B", Amount::from(100u64)).unwrap();
    assert_eq!(r.pool_path, vec!["pAB"]);
    assert_eq!(r.amount_out.value(), 99);

    // Paths that re-enter A exist: A->C->B is 2-hop; verify enumeration by
    // stats counting at least a few evaluated candidates.
    assert!(r.stats.paths_evaluated >= 2);
}

#[test]
fn different_precisions_are_preserved_and_routed() {
    // USDC (6 decimals) / WETH (18 decimals): reserves in smallest units.
    let one_eth = 1_000_000_000_000_000_000u128;
    let three_k_usdc = 3_000_000_000_000u128;
    let snap = Snapshot {
        assets: vec![asset("WETH", 18), asset("USDC", 6)],
        pools: vec![pool(
            "weth_usdc",
            "WETH",
            "USDC",
            one_eth,
            three_k_usdc,
            30,
            0,
            0,
        )],
    };
    let tenth_eth = one_eth / 10;
    let r = best_route(&snap, "WETH", "USDC", Amount::from(tenth_eth)).unwrap();
    assert_eq!(r.pool_path, vec!["weth_usdc"]);
    // Deterministic integer value, computed by the same exact formula.
    assert!(r.amount_out.value() > 0);
    // Quote endpoint decimals are wired by the API tests; here just sanity.
    assert_eq!(snap.asset("WETH").unwrap().decimals, 18);
    assert_eq!(snap.asset("USDC").unwrap().decimals, 6);
}

#[test]
fn insufficient_liquidity_reports_no_route_with_failure_detail() {
    let snap = Snapshot {
        assets: vec![asset("A", 18), asset("B", 18), asset("C", 18)],
        pools: vec![
            pool("pAC", "A", "C", 1000, 1000, 0, 0, 0),
            // C->B exists but cannot cover: it still swaps a positive amount,
            // so use an explicit cost bigger than any output to kill the hop.
            pool("pCB", "C", "B", 1000, 1000, 0, 0, 999_999),
        ],
    };
    let err = best_route(&snap, "A", "B", Amount::from(500u64)).unwrap_err();
    match err {
        RouteError::NoRoute {
            paths_considered,
            failures,
        } => {
            assert!(paths_considered >= 1);
            assert!(failures.iter().any(|f| f.pool_id == "pCB"));
        }
        other => panic!("expected NoRoute, got {other:?}"),
    }
}

#[test]
fn disconnected_assets_have_no_route() {
    let snap = Snapshot {
        assets: vec![asset("A", 18), asset("B", 18), asset("C", 18)],
        pools: vec![pool("pAB", "A", "B", 1000, 1000, 0, 0, 0)],
    };
    assert!(matches!(
        best_route(&snap, "A", "C", Amount::from(1u64)),
        Err(RouteError::NoRoute { .. })
    ));
}

#[test]
fn unknown_asset_is_refused_as_precision_unknown() {
    let snap = Snapshot {
        assets: vec![asset("A", 18)],
        pools: vec![],
    };
    assert!(matches!(
        best_route(&snap, "A", "ZZZ", Amount::from(1u64)),
        Err(RouteError::UnknownAsset(_))
    ));
}

#[test]
fn zero_input_refused() {
    let snap = Snapshot {
        assets: vec![asset("A", 18), asset("B", 18)],
        pools: vec![pool("pAB", "A", "B", 1000, 1000, 0, 0, 0)],
    };
    assert!(matches!(
        best_route(&snap, "A", "B", Amount::ZERO),
        Err(RouteError::ZeroInput)
    ));
}

#[test]
fn same_asset_quote_is_identity_without_cycles() {
    let snap = Snapshot {
        assets: vec![asset("A", 18), asset("B", 18)],
        pools: vec![pool("pAB", "A", "B", 1000, 1000, 0, 0, 0)],
    };
    let r = best_route(&snap, "A", "A", Amount::from(42u64)).unwrap();
    assert_eq!(r.amount_out.value(), 42);
    assert!(r.pool_path.is_empty());
    assert_eq!(r.stats.paths_evaluated, 1);
}

#[test]
fn explicit_costs_accumulate_per_hop() {
    // A->C fee-less 1000/1000 with cost 5 on C output: in 100 -> gross 90
    //   (floor(100*1000/1100)), net 85.
    // C->B 1000/1000 with cost 7 on B output: floor(85*1000/1085)=78, net 71.
    let snap = Snapshot {
        assets: vec![asset("A", 18), asset("B", 18), asset("C", 18)],
        pools: vec![
            pool("pAC", "A", "C", 1000, 1000, 0, 0, 5),
            pool("pCB", "C", "B", 1000, 1000, 0, 0, 7),
        ],
    };
    let r = best_route(&snap, "A", "B", Amount::from(100u64)).unwrap();
    assert_eq!(r.hops[0].gross_out.value(), 90);
    assert_eq!(r.hops[0].cost_out.value(), 5);
    assert_eq!(r.hops[0].net_out.value(), 85);
    assert_eq!(r.hops[1].gross_out.value(), 78);
    assert_eq!(r.hops[1].cost_out.value(), 7);
    assert_eq!(r.amount_out.value(), 71);
}

#[test]
fn max_three_hops_are_used() {
    // A->X->Y->B chain only; deep reserves keep the integer output at 100.
    let deep = 1_000_000_000_000_000_000u128; // 1e18
    let snap = Snapshot {
        assets: vec![
            asset("A", 18),
            asset("X", 18),
            asset("Y", 18),
            asset("B", 18),
        ],
        pools: vec![
            pool("pAX", "A", "X", deep, deep, 0, 0, 0),
            pool("pXY", "X", "Y", deep, deep, 0, 0, 0),
            pool("pYB", "Y", "B", deep, deep, 0, 0, 0),
        ],
    };
    let r = best_route(&snap, "A", "B", Amount::from(100u64)).unwrap();
    assert_eq!(r.pool_path, vec!["pAX", "pXY", "pYB"]);
    assert_eq!(r.hops.len(), 3);
    // At 1e18 depth each hop still floors 100 -> 99; CPMM always takes a
    // non-zero cut. Chaining three floors: 100 -> 99 -> 98 -> 97.
    assert_eq!(r.amount_out.value(), 97);
}

#[test]
fn pool_cannot_repeat_even_in_a_cycle() {
    // A<->B<->C triangle: path A->B->A->B would reuse pAB; ensure no route
    // longer than MAX_HOPS and no duplicate pools appear in a winner.
    let snap = Snapshot {
        assets: vec![asset("A", 18), asset("B", 18), asset("C", 18)],
        pools: vec![
            pool("pAB", "A", "B", 1_000_000, 1_000_000, 0, 0, 0),
            pool("pBC", "B", "C", 1_000_000, 1_000_000, 0, 0, 0),
            pool("pCA", "C", "A", 1_000_000, 1_000_000, 0, 0, 0),
        ],
    };
    let r = best_route(&snap, "A", "C", Amount::from(100u64)).unwrap();
    let mut ids = r.pool_path.clone();
    ids.sort();
    ids.dedup();
    assert_eq!(ids.len(), r.pool_path.len());
    assert!(r.pool_path.len() <= MAX_HOPS);
}

#[test]
fn side_selection_works_in_both_directions() {
    let snap = Snapshot {
        assets: vec![asset("A", 18), asset("B", 6)],
        pools: vec![pool("p", "A", "B", 1000, 2000, 0, 0, 0)],
    };
    let r1 = p016_quote_router::swap::swap_hop(
        &snap.pools[0],
        Side::ZeroToOne,
        Amount::from(100u64),
    )
    .unwrap();
    let r2 = p016_quote_router::swap::swap_hop(
        &snap.pools[0],
        Side::OneToZero,
        Amount::from(100u64),
    )
    .unwrap();
    assert_eq!(r1.net_out.value(), 181);
    assert_eq!(r2.net_out.value(), 47);
    assert_ne!(r1.net_out, r2.net_out);
}
