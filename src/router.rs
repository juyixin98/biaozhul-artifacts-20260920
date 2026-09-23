//! Offline single-chain router.
//!
//! Enumerates swap paths of at most [`MAX_HOPS`] hops through **distinct
//! pools** (tokens may repeat, so cycles are considered) and selects the path
//! whose final *integer net output* is greatest. Ties are broken by the
//! lexicographically smallest sequence of pool ids.
//!
//! ### Per-hop evaluation
//! Every hop is executed by [`crate::swap::swap_hop`] with its own fee, floor
//! rounding and explicit cost. There is no multiplication of marginal prices.
//!
//! ### Pruning (and why it is safe)
//! At a DFS node holding integer amount `I` of token `t` with `h` hops left,
//! an optimistic bound is computed over *real-number* ratios:
//!
//! ```text
//! cap_e(I) = I * (10_000 - fee_e) * reserveOut_e / (reserveIn_e * 10_000)
//! ```
//!
//! The bound allows pool reuse, ignores explicit costs and skips floor
//! rounding, so for every feasible route below the node:
//!
//! ```text
//! net_integer_output ≤ I * Π cap_ratios
//! ```
//!
//! The bound also includes the current amount if `t` is already the target
//! (stopping is allowed at any depth). A branch is pruned **only** when the
//! bound is strictly smaller than the best output already found; equality is
//! never pruned because a lexicographically smaller pool-id sequence could
//! still win the tie. See [`reference::best_route_exhaustive`] and the
//! differential tests in `tests/reference_parity.rs` for verification.

use std::collections::HashMap;

use serde::Serialize;

use crate::amount::{Amount, U1024};
use crate::model::Snapshot;
use crate::swap::{swap_hop, Side, SwapError, SwapHop};

/// Maximum route length supported (and tested exhaustively).
pub const MAX_HOPS: usize = 3;

/// One directed side of a pool in the adjacency graph.
#[derive(Debug, Clone)]
struct Edge {
    pool_idx: usize,
    side: Side,
    to: String,
}

#[derive(Debug)]
struct Graph {
    pools: Snapshot,
    adj: HashMap<String, Vec<Edge>>,
}

impl Graph {
    fn from_snapshot(snap: Snapshot) -> Self {
        let mut adj: HashMap<String, Vec<Edge>> = HashMap::new();
        for (pool_idx, p) in snap.pools.iter().enumerate() {
            adj.entry(p.token0.clone()).or_default().push(Edge {
                pool_idx,
                side: Side::ZeroToOne,
                to: p.token1.clone(),
            });
            adj.entry(p.token1.clone()).or_default().push(Edge {
                pool_idx,
                side: Side::OneToZero,
                to: p.token0.clone(),
            });
        }
        // Deterministic exploration order (pool ids are unique).
        for edges in adj.values_mut() {
            edges.sort_by(|a, b| {
                snap.pools[a.pool_idx]
                    .id
                    .cmp(&snap.pools[b.pool_idx].id)
            });
        }
        Graph { pools: snap, adj }
    }
}

/// Why a quote could not be produced.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RouteError {
    UnknownAsset(String),
    ZeroInput,
    /// No feasible route: graph disconnected, all hops failed (zero liquidity
    /// encountered during simulation, costs exceeded output, or overflow).
    NoRoute {
        paths_considered: usize,
        failures: Vec<EdgeFailure>,
    },
    /// Slippage parameter outside 0..=10000 bps.
    BadSlippage(u32),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct EdgeFailure {
    pub pool_id: String,
    pub reason: String,
}

impl std::fmt::Display for RouteError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RouteError::UnknownAsset(a) => write!(f, "asset {a:?} is not present in the snapshot (precision unknown)"),
            RouteError::ZeroInput => f.write_str("amount_in must be greater than zero"),
            RouteError::NoRoute {
                paths_considered,
                failures,
            } => write!(
                f,
                "no feasible route within {MAX_HOPS} hops; {paths_considered} candidate paths examined, {} edge-level failures recorded",
                failures.len()
            ),
            RouteError::BadSlippage(b) => write!(f, "slippage_bps={b} outside 0..=10000"),
        }
    }
}

impl std::error::Error for RouteError {}

/// Search statistics — the pruning savings are observable for verification.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct SearchStats {
    /// Candidate routes (pool sequences reaching the target) that were fully
    /// simulated, including the zero-hop identity route when applicable.
    pub paths_evaluated: usize,
    /// Hop attempts that failed (zero reserve, cost > output, overflow).
    pub edges_failed: usize,
    /// DFS branches discarded by the optimistic bound.
    pub branches_pruned: usize,
    /// Pool sequences enumerated by the search before pruning/evaluation.
    pub edges_attempted: usize,
}

/// A winning route.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct RouteResult {
    pub asset_in: String,
    pub asset_out: String,
    pub amount_in: Amount,
    pub amount_out: Amount,
    /// `"A"` or `"A,P1,B,..."` token sequence including endpoints.
    pub token_path: Vec<String>,
    pub pool_path: Vec<String>,
    pub hops: Vec<SwapHop>,
    pub stats: SearchStats,
}

/// Comparator selecting `(greater net output, smaller pool-id sequence)`.
fn strictly_better(out: Amount, pools: &[usize], cur: &Candidate) -> bool {
    if out != cur.out {
        return out > cur.out;
    }
    // Lexicographic pool-id comparison.
    for (a, b) in pools.iter().zip(cur.pools.iter()) {
        let ai = a;
        let bi = b;
        if ai != bi {
            return ai < bi;
        }
    }
    pools.len() < cur.pools.len()
}

#[derive(Clone)]
struct Candidate {
    out: Amount,
    pools: Vec<usize>,
}

/// Best feasible output for the current DFS node, represented as the fraction
/// `num/den` (real number, ignoring costs/visited/floors) plus an optional
/// "stop here" integer (used when the current token is the target).
#[derive(Clone, Copy)]
struct Frac {
    num: U1024,
    den: U1024,
}

impl Frac {
    fn of_amount(v: u128) -> Self {
        Frac {
            num: U1024::from(v),
            den: U1024::from(1u64),
        }
    }

    /// Multiply by `n/d` (exact, no cancellation needed: ≤ ~600 bits).
    fn mul(self, n: u128, d: u128) -> Self {
        Frac {
            num: self.num * U1024::from(n),
            den: self.den * U1024::from(d),
        }
    }

    /// Strictly less than an integer `x`:  num/den < x  <=>  num < x*den.
    fn lt_int(self, x: u128) -> bool {
        self.num < U1024::from(x) * self.den
    }
}

struct Searcher<'a> {
    g: &'a Graph,
    target: String,
    used: Vec<bool>,
    stats: SearchStats,
    best: Option<Candidate>,
    failures: Vec<EdgeFailure>,
}

impl<'a> Searcher<'a> {
    /// Optimistic, admissible bound on the target amount attainable from
    /// `token` holding real fraction `cur` within `h` hops.
    fn bound(&self, token: &str, h: usize, cur: Frac) -> Frac {
        let mut best = if token == self.target {
            // Stopping here is feasible...
            cur
        } else if h == 0 {
            // Wrong token, no hops remain: the only safe bound is zero.
            return Frac::of_amount(0);
        } else {
            Frac::of_amount(0)
        };
        if h == 0 {
            return best;
        }
        if let Some(edges) = self.g.adj.get(token) {
            for e in edges {
                let p = &self.g.pools.pools[e.pool_idx];
                let (r_in, r_out, fee_bps) = match e.side {
                    Side::ZeroToOne => (p.reserve0.value(), p.reserve1.value(), p.fee_bps),
                    Side::OneToZero => (p.reserve1.value(), p.reserve0.value(), p.fee_bps),
                };
                if r_in == 0 || r_out == 0 {
                    continue;
                }
                let n = u128::from(10_000u32 - fee_bps).saturating_mul(r_out);
                let d = r_in.saturating_mul(10_000);
                // d cannot be zero: r_in > 0.
                let nxt = cur.mul(n, d);
                let via = self.bound(&e.to, h - 1, nxt);
                if via.num * best.den > best.num * via.den {
                    best = via;
                }
            }
        }
        best
    }

    fn record_failure(&mut self, pool_id: &str, err: &SwapError) {
        self.stats.edges_failed += 1;
        if self.failures.len() < 32 {
            self.failures.push(EdgeFailure {
                pool_id: pool_id.to_string(),
                reason: err.to_string(),
            });
        }
    }

    /// DFS. `token` is the asset currently held (`amount`), `depth` hops used,
    /// `cur_frac` is the real-valued optimistic fraction used for bounds.
    fn dfs(&mut self, token: &str, amount: Amount, depth: usize, pool_path: &mut Vec<usize>, cur_frac: Frac) {
        // Prune first, using a bound that includes stopping here when the
        // current token is the target (see `bound`), so the candidate record
        // below can never be dropped by pruning.
        let hops_left = MAX_HOPS - depth;
        let best_int = self.best.as_ref().map(|c| c.out.value()).unwrap_or(0);
        let optimistic = self.bound(token, hops_left, cur_frac);
        if optimistic.lt_int(best_int) {
            self.stats.branches_pruned += 1;
            return;
        }

        // Record the "stop here" candidate if at target.
        if token == self.target {
            self.stats.paths_evaluated += 1;
            let better = match &self.best {
                None => true,
                Some(c) => strictly_better(amount, pool_path, c),
            };
            if better {
                self.best = Some(Candidate {
                    out: amount,
                    pools: pool_path.clone(),
                });
            }
        }

        if depth == MAX_HOPS {
            return;
        }

        let Some(edges) = self.g.adj.get(token) else {
            return;
        };
        for e in edges.clone() {
            if self.used[e.pool_idx] {
                continue;
            }
            self.stats.edges_attempted += 1;
            let pool = &self.g.pools.pools[e.pool_idx];
            match swap_hop(pool, e.side, amount) {
                Ok(hop) => {
                    if hop.net_out.is_zero() {
                        // A zero amount cannot produce any positive output in
                        // later hops (monotonicity), so expansion stops here;
                        // record it as an edge-level failure for observability.
                        self.stats.edges_failed += 1;
                        if self.failures.len() < 32 {
                            self.failures.push(EdgeFailure {
                                pool_id: pool.id.clone(),
                                reason: "hop yielded zero integer output (fee/rounding consumed it)"
                                    .to_string(),
                            });
                        }
                        continue;
                    }
                    self.used[e.pool_idx] = true;
                    pool_path.push(e.pool_idx);
                    let nxt_frac = {
                        let (r_in, r_out) = match e.side {
                            Side::ZeroToOne => (pool.reserve0.value(), pool.reserve1.value()),
                            Side::OneToZero => (pool.reserve1.value(), pool.reserve0.value()),
                        };
                        let n = u128::from(10_000u32 - pool.fee_bps).saturating_mul(r_out);
                        let d = r_in.saturating_mul(10_000);
                        cur_frac.mul(n, d)
                    };
                    self.dfs(&e.to, hop.net_out, depth + 1, pool_path, nxt_frac);
                    pool_path.pop();
                    self.used[e.pool_idx] = false;
                }
                Err(err) => self.record_failure(&pool.id, &err),
            }
        }
    }
}

/// Floor `amount * (10_000 - slippage_bps) / 10_000` in 256-bit intermediates.
pub fn minimum_output(amount: Amount, slippage_bps: u32) -> Result<Amount, RouteError> {
    if slippage_bps > 10_000 {
        return Err(RouteError::BadSlippage(slippage_bps));
    }
    let num = crate::amount::U256::from(amount.value())
        * crate::amount::U256::from(10_000u32 - slippage_bps);
    let v = num / crate::amount::U256::from(10_000u64);
    // Multiplying by (10_000 - bps) ≤ 10_000 and then dividing by 10_000
    // cannot produce a value larger than the original u128 amount.
    Ok(Amount::from_u256(v).expect("floor by 10_000 never exceeds original"))
}

/// Find the best route on the given snapshot.
pub fn best_route(
    snap: &Snapshot,
    asset_in: &str,
    asset_out: &str,
    amount_in: Amount,
) -> Result<RouteResult, RouteError> {
    if snap.asset(asset_in).is_none() {
        return Err(RouteError::UnknownAsset(asset_in.to_string()));
    }
    if snap.asset(asset_out).is_none() {
        return Err(RouteError::UnknownAsset(asset_out.to_string()));
    }
    if amount_in.is_zero() {
        return Err(RouteError::ZeroInput);
    }

    let g = Graph::from_snapshot(snap.clone());

    // Identity: same asset, no hops. Cycles are deliberately not explored for
    // same-asset quotes (they only ever destroy value after fees/costs).
    if asset_in == asset_out {
        return Ok(RouteResult {
            asset_in: asset_in.to_string(),
            asset_out: asset_out.to_string(),
            amount_in,
            amount_out: amount_in,
            token_path: vec![asset_in.to_string()],
            pool_path: vec![],
            hops: vec![],
            stats: SearchStats {
                paths_evaluated: 1,
                ..SearchStats::default()
            },
        });
    }

    let n_pools = snap.pools.len();
    let mut searcher = Searcher {
        g: &g,
        target: asset_out.to_string(),
        used: vec![false; n_pools],
        stats: SearchStats::default(),
        best: None,
        failures: vec![],
    };
    let mut path = Vec::new();
    searcher.dfs(
        asset_in,
        amount_in,
        0,
        &mut path,
        Frac::of_amount(amount_in.value()),
    );

    let Searcher {
        best,
        stats,
        failures,
        ..
    } = searcher;

    let Some(best) = best else {
        return Err(RouteError::NoRoute {
            paths_considered: stats.edges_attempted,
            failures,
        });
    };

    // Re-simulate the winning sequence to materialize per-hop evidence.
    let mut hops = Vec::with_capacity(best.pools.len());
    let mut token = asset_in.to_string();
    let mut amount = amount_in;
    let mut token_path = vec![asset_in.to_string()];
    for &pi in &best.pools {
        let pool = &snap.pools[pi];
        let side = if token == pool.token0 {
            Side::ZeroToOne
        } else {
            Side::OneToZero
        };
        let hop = swap_hop(pool, side, amount).expect("winning route re-simulates");
        amount = hop.net_out;
        token = hop.asset_out.clone();
        token_path.push(token.clone());
        hops.push(hop);
    }
    assert_eq!(token, asset_out);

    Ok(RouteResult {
        asset_in: asset_in.to_string(),
        asset_out: asset_out.to_string(),
        amount_in,
        amount_out: best.out,
        token_path,
        pool_path: best.pools.iter().map(|i| snap.pools[*i].id.clone()).collect(),
        hops,
        stats,
    })
}

/// Brute-force reference: enumerate every distinct-pool edge sequence of
/// length 1..=[`MAX_HOPS`] with no pruning whatsoever. Used by differential
/// tests to prove the pruned searcher never drops the true optimum.
pub mod reference {
    use super::*;

    /// Returns the best route (winning pool indices and output), or `None`.
    pub fn best_route_exhaustive(
        snap: &Snapshot,
        asset_in: &str,
        asset_out: &str,
        amount_in: Amount,
    ) -> Option<(Vec<usize>, Amount)> {
        if asset_in == asset_out {
            return Some((vec![], amount_in));
        }
        let g = Graph::from_snapshot(snap.clone());
        let mut used = vec![false; snap.pools.len()];
        let mut path = Vec::new();
        let mut best: Option<Candidate> = None;
        dfs_ref(
            &g,
            snap,
            asset_in,
            asset_out,
            amount_in,
            0,
            &mut used,
            &mut path,
            &mut best,
        );
        best.map(|c| (c.pools, c.out))
    }

    #[allow(clippy::too_many_arguments)]
    fn dfs_ref(
        g: &Graph,
        snap: &Snapshot,
        token: &str,
        target: &str,
        amount: Amount,
        depth: usize,
        used: &mut [bool],
        path: &mut Vec<usize>,
        best: &mut Option<Candidate>,
    ) {
        if token == target {
            let better = match best {
                None => true,
                Some(c) => strictly_better(amount, path, c),
            };
            if better {
                *best = Some(Candidate {
                    out: amount,
                    pools: path.clone(),
                });
            }
        }
        if depth == MAX_HOPS || amount.is_zero() {
            return;
        }
        if let Some(edges) = g.adj.get(token) {
            for e in edges.clone() {
                if used[e.pool_idx] {
                    continue;
                }
                let pool = &snap.pools[e.pool_idx];
                if let Ok(hop) = swap_hop(pool, e.side, amount) {
                    if hop.net_out.is_zero() {
                        continue;
                    }
                    used[e.pool_idx] = true;
                    path.push(e.pool_idx);
                    dfs_ref(g, snap, &e.to, target, hop.net_out, depth + 1, used, path, best);
                    path.pop();
                    used[e.pool_idx] = false;
                }
            }
        }
    }
}
