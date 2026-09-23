//! Quote orchestration: load a snapshot, build the graph, route, and apply
//! the slippage-based minimum output.

use crate::db::Store;
use crate::model::{
    parse_amount, HopView, QuoteRequest, QuoteResponse, Snapshot, MAX_HOPS,
};
use crate::router::{Graph, RouteError};
use crate::uint256::U256;

const BPS: u128 = 10_000;

#[derive(Debug)]
pub enum QuoteError {
    BadRequest(String),
    SnapshotNotFound(i64),
    Route(RouteError),
    OutputBelowMinimum { got: u128, required: u128 },
    Overflow(String),
    Storage(String),
}

impl std::fmt::Display for QuoteError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            QuoteError::BadRequest(m) => write!(f, "{m}"),
            QuoteError::SnapshotNotFound(id) => write!(f, "snapshot {id} not found"),
            QuoteError::Route(e) => write!(f, "{e}"),
            QuoteError::OutputBelowMinimum { got, required } => write!(
                f,
                "net output {got} is below the requested minimum output {required}"
            ),
            QuoteError::Overflow(m) => write!(f, "overflow: {m}"),
            QuoteError::Storage(m) => write!(f, "storage error: {m}"),
        }
    }
}

pub async fn quote(store: &Store, req: &QuoteRequest) -> Result<QuoteResponse, QuoteError> {
    if !(1..=MAX_HOPS).contains(&req.max_hops) {
        return Err(QuoteError::BadRequest(format!(
            "max_hops must be between 1 and {MAX_HOPS}"
        )));
    }
    let slippage_bps = req.slippage_bps.unwrap_or(0);
    if slippage_bps > BPS as u32 {
        return Err(QuoteError::BadRequest(
            "slippage_bps must be between 0 and 10000".to_string(),
        ));
    }
    let amount_in = parse_amount(&req.amount_in, "amount_in").map_err(QuoteError::BadRequest)?;
    let cost_per_hop =
        parse_amount(&req.cost_per_hop, "cost_per_hop").map_err(QuoteError::BadRequest)?;
    let client_min = match &req.min_output {
        Some(s) => Some(parse_amount(s, "min_output").map_err(QuoteError::BadRequest)?),
        None => None,
    };

    let snapshot: Snapshot = match store.load_snapshot(req.snapshot_id).await {
        Ok(Some(s)) => s,
        Ok(None) => match req.snapshot_id {
            Some(id) => return Err(QuoteError::SnapshotNotFound(id)),
            None => {
                return Err(QuoteError::BadRequest(
                    "no snapshots exist yet; POST one to /snapshots".to_string(),
                ))
            }
        },
        Err(e) => return Err(QuoteError::Storage(e.to_string())),
    };

    // Every pool token must have a known precision — ingestion guarantees
    // this, but re-check defensively against hand-edited databases.
    for p in &snapshot.pools {
        for t in [&p.token0, &p.token1] {
            if !snapshot.assets.iter().any(|a| &a.id == t) {
                return Err(QuoteError::BadRequest(format!(
                    "snapshot {} pool {} references token {t} with unknown precision",
                    snapshot.id, p.id
                )));
            }
        }
        if p.reserve0 == 0 || p.reserve1 == 0 {
            return Err(QuoteError::BadRequest(format!(
                "snapshot {} pool {} has a zero reserve",
                snapshot.id, p.id
            )));
        }
    }

    let token_ids: Vec<String> = snapshot.assets.iter().map(|a| a.id.clone()).collect();
    let graph = Graph::build(snapshot.pools.clone(), &token_ids);

    let route = graph
        .search(
            &req.token_in,
            &req.token_out,
            amount_in,
            cost_per_hop,
            req.max_hops,
        )
        .map_err(QuoteError::Route)?;

    let gross = route.hops.last().unwrap().gross_amount_out;
    let net = route.hops.last().unwrap().net_amount_out;
    let final_hop_explicit_cost = route.hops.last().unwrap().explicit_cost;

    // min_output = floor(net * (10000 - slippage_bps) / 10000), checked 256-bit.
    let min_output = apply_slippage(net, slippage_bps)
        .ok_or_else(|| QuoteError::Overflow("computing min_output".to_string()))?;

    if let Some(required) = client_min {
        if net < required {
            return Err(QuoteError::OutputBelowMinimum {
                got: net,
                required,
            });
        }
    }

    let hops = route
        .hops
        .iter()
        .enumerate()
        .map(|(i, h)| HopView {
            hop: i + 1,
            pool_id: h.pool_id.clone(),
            token_in: h.token_in.clone(),
            token_out: h.token_out.clone(),
            amount_in: h.amount_in.to_string(),
            reserve_in: h.reserve_in.to_string(),
            reserve_out: h.reserve_out.to_string(),
            fee_bps: h.fee_bps,
            gross_amount_out: h.gross_amount_out.to_string(),
            explicit_cost: h.explicit_cost.to_string(),
            net_amount_out: h.net_amount_out.to_string(),
        })
        .collect();

    Ok(QuoteResponse {
        snapshot_id: snapshot.id,
        content_hash: snapshot.content_hash,
        token_in: req.token_in.clone(),
        token_out: req.token_out.clone(),
        amount_in: amount_in.to_string(),
        hops,
        gross_amount_out: gross.to_string(),
        final_hop_explicit_cost: final_hop_explicit_cost.to_string(),
        net_amount_out: net.to_string(),
        min_output: min_output.to_string(),
        paths_considered: route.paths_considered,
        feasible_paths: route.feasible_paths,
    })
}

fn apply_slippage(net: u128, slippage_bps: u32) -> Option<u128> {
    if slippage_bps == 0 {
        return Some(net);
    }
    let factor = BPS.checked_sub(slippage_bps as u128)?;
    U256::from_u128(net)
        .checked_mul_u128(factor)?
        .checked_div(U256::from_u128(BPS))?
        .to_u128()
}
