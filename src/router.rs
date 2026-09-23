//! Offline path router over a single snapshot.
//!
//! Search rules:
//! * paths use at most `max_hops` (<= 3) pools;
//! * no pool is used twice within a path (tokens may repeat — cycles are
//!   enumerated honestly, not special-cased away);
//! * every hop executes the pool's own integer swap with its own fee —
//!   floored output of hop N is the input of hop N+1;
//! * a fixed explicit cost per hop (raw units of that hop's output token)
//!   is deducted after the swap; an intermediate carry of zero, or a cost
//!   exceeding the hop output, kills the route;
//! * the route with the greatest final net integer output wins; exact ties
//!   break on the lexicographically smallest tuple of pool ids.
//!
//! The production search is depth-first with a reverse reachability prune.
//! [`Graph::brute_force_sequences`] enumerates every non-repeating pool
//! sequence on a graph; tests assert the pruned search considers exactly
//! the same valid sequences (same count, same simulation verdicts) and picks
//! the same winner as evaluating every sequence independently.

use std::collections::HashMap;

use crate::amm::get_amount_out;
use crate::model::{Pool, MAX_HOPS};

#[derive(Clone, Copy, Debug)]
struct Edge {
    pool: usize,
    to: usize,
}

#[derive(Debug)]
pub struct Graph {
    token_ids: Vec<String>,
    token_index: HashMap<String, usize>,
    pools: Vec<Pool>,
    /// adj[token_idx] holds outgoing edges sorted by pool id.
    adj: Vec<Vec<Edge>>,
}

/// One executed hop with the exact integers the quote is based on.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RouteHop {
    pub pool_id: String,
    pub token_in: String,
    pub token_out: String,
    pub amount_in: u128,
    pub reserve_in: u128,
    pub reserve_out: u128,
    pub fee_bps: u32,
    pub gross_amount_out: u128,
    pub explicit_cost: u128,
    pub net_amount_out: u128,
}

/// Why a syntactically valid pool sequence cannot be quoted.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum PathError {
    ZeroReserve,
    ZeroOutput,
    CostExceedsOutput,
    Overflow,
}

#[derive(Clone, Debug)]
pub struct BestRoute {
    pub hops: Vec<RouteHop>,
    pub paths_considered: usize,
    pub feasible_paths: usize,
}

#[derive(Default, Clone, Copy, Debug)]
struct Counters {
    considered: usize,
    feasible: usize,
    /// Any hop arithmetic overflowed 256 bits while searching. When no
    /// feasible route exists, this turns into an explicit OVERFLOW
    /// rejection instead of a misleading "no liquidity" answer.
    overflow: bool,
}

impl Graph {
    pub fn build(pools: Vec<Pool>, known_tokens: &[String]) -> Self {
        let mut token_index = HashMap::new();
        let mut token_ids: Vec<String> = Vec::new();
        for t in known_tokens {
            if !token_index.contains_key(t) {
                token_index.insert(t.clone(), token_ids.len());
                token_ids.push(t.clone());
            }
        }
        for p in &pools {
            for t in [&p.token0, &p.token1] {
                if !token_index.contains_key(t) {
                    token_index.insert(t.clone(), token_ids.len());
                    token_ids.push(t.clone());
                }
            }
        }

        let n = token_ids.len();
        let mut adj = vec![Vec::new(); n];
        for (pi, p) in pools.iter().enumerate() {
            let a = token_index[&p.token0];
            let b = token_index[&p.token1];
            adj[a].push(Edge { pool: pi, to: b });
            adj[b].push(Edge { pool: pi, to: a });
        }
        for edges in &mut adj {
            edges.sort_by(|x, y| pools[x.pool].id.cmp(&pools[y.pool].id));
        }

        Graph {
            token_ids,
            token_index,
            pools,
            adj,
        }
    }

    pub fn token_idx(&self, id: &str) -> Option<usize> {
        self.token_index.get(id).copied()
    }

    pub fn pool_count(&self) -> usize {
        self.pools.len()
    }

    /// Execute one concrete pool chain from `start`. Returns the per-hop
    /// evidence or the first reason the chain is not quotable. No marginal
    /// prices: each hop calls the AMM with the carry of the previous hop.
    pub fn simulate(
        &self,
        pool_path: &[usize],
        start: usize,
        amount_in: u128,
        cost_per_hop: u128,
    ) -> Result<Vec<RouteHop>, PathError> {
        let mut hops = Vec::with_capacity(pool_path.len());
        let mut cur = start;
        let mut amount = amount_in;

        for &pi in pool_path {
            let pool = &self.pools[pi];
            let (reserve_in, reserve_out, out_token) =
                if self.token_index[&pool.token0] == cur {
                    (pool.reserve0, pool.reserve1, pool.token1.clone())
                } else if self.token_index[&pool.token1] == cur {
                    (pool.reserve1, pool.reserve0, pool.token0.clone())
                } else {
                    return Err(PathError::Overflow); // malformed chain
                };

            let swap = get_amount_out(amount, reserve_in, reserve_out, pool.fee_bps).map_err(
                |e| match e {
                    crate::amm::SwapError::ZeroReserve => PathError::ZeroReserve,
                    crate::amm::SwapError::ZeroInput => PathError::ZeroOutput,
                    crate::amm::SwapError::Overflow => PathError::Overflow,
                },
            )?;

            if cost_per_hop > swap.amount_out {
                return Err(PathError::CostExceedsOutput);
            }
            let net = swap.amount_out - cost_per_hop;
            if net == 0 {
                // A zero carry cannot execute another hop, and a zero final
                // output is not a quote.
                return Err(PathError::ZeroOutput);
            }

            hops.push(RouteHop {
                pool_id: pool.id.clone(),
                token_in: self.token_ids[cur].clone(),
                token_out: out_token,
                amount_in: amount,
                reserve_in,
                reserve_out,
                fee_bps: pool.fee_bps,
                gross_amount_out: swap.amount_out,
                explicit_cost: cost_per_hop,
                net_amount_out: net,
            });

            cur = self
                .token_index[if self.token_index[&pool.token0] == cur {
                    &pool.token1
                } else {
                    &pool.token0
                }];
            amount = net;
        }
        Ok(hops)
    }

    /// Find the best route with reverse-reachability-pruned DFS.
    pub fn search(
        &self,
        token_in: &str,
        token_out: &str,
        amount_in: u128,
        cost_per_hop: u128,
        max_hops: usize,
    ) -> Result<BestRoute, RouteError> {
        self.search_inner(
            token_in, token_out, amount_in, cost_per_hop, max_hops, true,
        )
    }

    /// Same as [`search`] but with the reverse-reachability prune disabled.
    /// Exists so tests can prove pruning neither loses nor invents routes
    /// versus the brute-force enumerator.
    #[doc(hidden)]
    pub fn search_without_prune(
        &self,
        token_in: &str,
        token_out: &str,
        amount_in: u128,
        cost_per_hop: u128,
        max_hops: usize,
    ) -> Result<BestRoute, RouteError> {
        self.search_inner(
            token_in, token_out, amount_in, cost_per_hop, max_hops, false,
        )
    }

    fn search_inner(
        &self,
        token_in: &str,
        token_out: &str,
        amount_in: u128,
        cost_per_hop: u128,
        max_hops: usize,
        prune: bool,
    ) -> Result<BestRoute, RouteError> {
        let start = self
            .token_idx(token_in)
            .ok_or_else(|| RouteError::UnknownToken(token_in.to_string()))?;
        let target = self
            .token_idx(token_out)
            .ok_or_else(|| RouteError::UnknownToken(token_out.to_string()))?;
        if start == target {
            return Err(RouteError::SameToken);
        }
        if amount_in == 0 {
            return Err(RouteError::ZeroAmount);
        }
        if !(1..=MAX_HOPS).contains(&max_hops) {
            return Err(RouteError::BadMaxHops);
        }

        let reach = if prune {
            self.reverse_reachability(target, max_hops)
        } else {
            // Neutral table: every token considered reachable at every depth.
            vec![vec![true; self.token_ids.len()]; max_hops + 1]
        };
        let mut used = vec![false; self.pools.len()];
        let mut prefix: Vec<RouteHop> = Vec::with_capacity(max_hops);
        let mut counters = Counters::default();
        let mut best: Option<Vec<RouteHop>> = None;

        self.dfs(
            DfsCtx {
                target,
                amount_in,
                cost_per_hop,
                max_hops,
                reach: &reach,
            },
            start,
            &mut used,
            &mut prefix,
            &mut counters,
            &mut best,
        );

        match best {
            Some(hops) => Ok(BestRoute {
                hops,
                paths_considered: counters.considered,
                feasible_paths: counters.feasible,
            }),
            None => Err(if !self.has_structural_path(start, target, max_hops) {
                RouteError::NoPath
            } else if counters.overflow {
                RouteError::Overflow
            } else {
                RouteError::NoFeasiblePath
            }),
        }
    }

    /// Does *any* non-repeating pool chain from `start` to `target` exist
    /// within `max_hops`, ignoring amounts/fees/costs? Used only to choose
    /// the precise error on routing failure.
    fn has_structural_path(&self, start: usize, target: usize, max_hops: usize) -> bool {
        fn rec(
            g: &Graph,
            cur: usize,
            target: usize,
            depth_left: usize,
            used: &mut [bool],
        ) -> bool {
            if cur == target {
                return true;
            }
            if depth_left == 0 {
                return false;
            }
            for edge in &g.adj[cur] {
                if used[edge.pool] {
                    continue;
                }
                used[edge.pool] = true;
                if rec(g, edge.to, target, depth_left - 1, used) {
                    used[edge.pool] = false;
                    return true;
                }
                used[edge.pool] = false;
            }
            false
        }
        let mut used = vec![false; self.pools.len()];
        rec(self, start, target, max_hops, &mut used)
    }

    #[allow(clippy::too_many_arguments)]
    fn dfs(
        &self,
        ctx: DfsCtx<'_>,
        cur: usize,
        used: &mut [bool],
        prefix: &mut Vec<RouteHop>,
        counters: &mut Counters,
        best: &mut Option<Vec<RouteHop>>,
    ) {
        if prefix.len() == ctx.max_hops {
            return;
        }
        let remaining_after_edge = ctx.max_hops - (prefix.len() + 1);

        for edge in &self.adj[cur] {
            if used[edge.pool] {
                continue;
            }
            // Reverse-reachability prune: after taking this edge the target
            // must be reachable within the hops remaining. The reachability
            // table allows pool reuse, so it is a superset of real options
            // and can only prune provably dead branches.
            if !ctx.reach[remaining_after_edge][edge.to] {
                continue;
            }

            let pool = &self.pools[edge.pool];
            let (reserve_in, reserve_out, out_token) =
                if self.token_index[&pool.token0] == cur {
                    (pool.reserve0, pool.reserve1, pool.token1.clone())
                } else {
                    (pool.reserve1, pool.reserve0, pool.token0.clone())
                };
            let carry = prefix
                .last()
                .map(|h| h.net_amount_out)
                .unwrap_or(ctx.amount_in);

            let (hop, overflowed) = match get_amount_out(carry, reserve_in, reserve_out, pool.fee_bps)
            {
                Ok(swap) if swap.amount_out >= ctx.cost_per_hop
                    && swap.amount_out - ctx.cost_per_hop > 0 =>
                {
                    (
                        Some(RouteHop {
                            pool_id: pool.id.clone(),
                            token_in: self.token_ids[cur].clone(),
                            token_out: out_token,
                            amount_in: carry,
                            reserve_in,
                            reserve_out,
                            fee_bps: pool.fee_bps,
                            gross_amount_out: swap.amount_out,
                            explicit_cost: ctx.cost_per_hop,
                            net_amount_out: swap.amount_out - ctx.cost_per_hop,
                        }),
                        false,
                    )
                }
                Ok(_) => (None, false), // cost not payable / dust output
                Err(crate::amm::SwapError::Overflow) => (None, true),
                Err(_) => (None, false), // zero reserve / zero input
            };

            used[edge.pool] = true;

            if let Some(hop) = hop {
                prefix.push(hop);

                if edge.to == ctx.target {
                    counters.considered += 1;
                    counters.feasible += 1;
                    if best
                        .as_ref()
                        .map(|b| route_better(prefix, b))
                        .unwrap_or(true)
                    {
                        *best = Some(prefix.clone());
                    }
                }

                // Expand further (cycles included — only pool reuse is
                // forbidden). The prefix is known to carry positive value.
                self.dfs(ctx, edge.to, used, prefix, counters, best);

                prefix.pop();
            } else {
                if overflowed {
                    counters.overflow = true;
                }
                if edge.to == ctx.target {
                    // The sequence reaches the target but cannot execute:
                    // counted as considered, never as feasible.
                    counters.considered += 1;
                }
            }

            used[edge.pool] = false;
        }
    }

    /// reach[k][v] == true iff `target` is reachable from token v in <= k
    /// edges (pools may repeat — a superset, keeping the prune sound).
    fn reverse_reachability(&self, target: usize, max_hops: usize) -> Vec<Vec<bool>> {
        let n = self.token_ids.len();
        let mut reach = vec![vec![false; n]; max_hops + 1];
        reach[0][target] = true;
        for k in 1..=max_hops {
            let mut cur = reach[k - 1].clone();
            for (v, reachable) in cur.iter_mut().enumerate() {
                if !*reachable {
                    *reachable = self.adj[v].iter().any(|e| reach[k - 1][e.to]);
                }
            }
            reach[k] = cur;
        }
        reach
    }

    /// Exhaustive reference: every pool sequence of length 1..=max_hops with
    /// no repeated pool that forms a valid chain from `token_in` to
    /// `token_out`, in lexicographic pool-index order. Tests simulate every
    /// returned sequence independently and compare with the pruned DFS.
    pub fn brute_force_sequences(
        &self,
        token_in: &str,
        token_out: &str,
        max_hops: usize,
    ) -> Vec<Vec<usize>> {
        let start = match self.token_idx(token_in) {
            Some(v) => v,
            None => return vec![],
        };
        let target = match self.token_idx(token_out) {
            Some(v) => v,
            None => return vec![],
        };
        if start == target || !(1..=MAX_HOPS).contains(&max_hops) {
            return vec![];
        }

        let mut out = Vec::new();
        let mut used = vec![false; self.pools.len()];
        let mut path = Vec::new();

        fn rec(
            g: &Graph,
            cur: usize,
            target: usize,
            max_hops: usize,
            used: &mut [bool],
            path: &mut Vec<usize>,
            out: &mut Vec<Vec<usize>>,
        ) {
            if cur == target && !path.is_empty() {
                out.push(path.clone());
            }
            if path.len() == max_hops {
                return;
            }
            for pi in 0..g.pools.len() {
                if used[pi] {
                    continue;
                }
                let p = &g.pools[pi];
                let a = g.token_index[&p.token0];
                let b = g.token_index[&p.token1];
                let next = if a == cur {
                    b
                } else if b == cur {
                    a
                } else {
                    continue;
                };
                used[pi] = true;
                path.push(pi);
                rec(g, next, target, max_hops, used, path, out);
                path.pop();
                used[pi] = false;
            }
        }

        rec(
            self,
            start,
            target,
            max_hops,
            &mut used,
            &mut path,
            &mut out,
        );
        out
    }
}

#[derive(Clone, Copy)]
struct DfsCtx<'a> {
    target: usize,
    amount_in: u128,
    cost_per_hop: u128,
    max_hops: usize,
    reach: &'a [Vec<bool>],
}

/// Higher final net output wins; exact ties break on the lexicographically
/// smallest pool-id tuple (so the result is deterministic and matches the
/// requirement "ties by pool id").
fn route_better(cand: &[RouteHop], best: &[RouteHop]) -> bool {
    let co = cand.last().unwrap().net_amount_out;
    let bo = best.last().unwrap().net_amount_out;
    match co.cmp(&bo) {
        std::cmp::Ordering::Greater => true,
        std::cmp::Ordering::Less => false,
        std::cmp::Ordering::Equal => {
            for (c, b) in cand.iter().zip(best.iter()) {
                match c.pool_id.cmp(&b.pool_id) {
                    std::cmp::Ordering::Less => return true,
                    std::cmp::Ordering::Greater => return false,
                    std::cmp::Ordering::Equal => {}
                }
            }
            false // identical sequences: enumeration is unique
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub enum RouteError {
    UnknownToken(String),
    SameToken,
    ZeroAmount,
    BadMaxHops,
    NoPath,
    NoFeasiblePath,
    Overflow,
}

impl std::fmt::Display for RouteError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RouteError::UnknownToken(t) => write!(f, "unknown token: {t}"),
            RouteError::SameToken => write!(f, "token_in and token_out must differ"),
            RouteError::ZeroAmount => write!(f, "amount_in must be positive"),
            RouteError::BadMaxHops => write!(f, "max_hops must be between 1 and {MAX_HOPS}"),
            RouteError::NoPath => write!(f, "no pool path exists within the hop limit"),
            RouteError::NoFeasiblePath => write!(
                f,
                "paths exist but none yield a positive output after fees, explicit costs and overflow checks"
            ),
            RouteError::Overflow => write!(
                f,
                "256-bit integer overflow while simulating swaps against these reserves; route rejected"
            ),
        }
    }
}

#[cfg(test)]
#[path = "router_tests.rs"]
mod tests;
