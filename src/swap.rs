//! Exact single-hop swap math on a constant-product pool.
//!
//! One hop is evaluated exactly the way an on-chain pool would:
//!
//! ```text
//! inAfterFee = amountIn * (10_000 - feeBps)
//! grossOut   = floor( inAfterFee * reserveOut / (reserveIn*10_000 + inAfterFee) )
//! netOut     = grossOut - costOut
//! ```
//!
//! All operands are integers and `grossOut` is rounded **down per hop**.
//! Multi-hop routes chain these rounded integer results; marginal prices are
//! never multiplied across hops (that would drop the floors and over-report).

use serde::Serialize;

use crate::amount::{Amount, U256};
use crate::model::Pool;

/// Fee denominator: 1 bps = 1/10_000.
pub const BPS_DEN: u128 = 10_000;

/// Which side of a pool is being taken.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side {
    /// input is token0, output is token1.
    ZeroToOne,
    /// input is token1, output is token0.
    OneToZero,
}

/// Why a single hop cannot be executed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SwapError {
    /// Input amount is zero (nothing to swap).
    ZeroInput,
    /// A pool reserve on the used side is zero — no liquidity.
    ZeroReserve,
    /// Fee is outside 0..=10_000.
    BadFee(u32),
    /// Intermediate value exceeded the u128 amount domain.
    Overflow,
    /// Gross output is smaller than the explicit cost of the hop.
    CostExceedsOutput { gross_out: Amount, cost_out: Amount },
}

impl std::fmt::Display for SwapError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SwapError::ZeroInput => f.write_str("input amount is zero"),
            SwapError::ZeroReserve => f.write_str("pool reserve is zero"),
            SwapError::BadFee(bps) => write!(f, "fee_bps={bps} outside 0..=10000"),
            SwapError::Overflow => f.write_str("swap arithmetic exceeds u128"),
            SwapError::CostExceedsOutput { gross_out, cost_out } => write!(
                f,
                "explicit cost {cost_out} exceeds gross output {gross_out}"
            ),
        }
    }
}

impl std::error::Error for SwapError {}

/// Fully evidenced result of one hop, suitable for quoting.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct SwapHop {
    pub pool_id: String,
    pub asset_in: String,
    pub asset_out: String,
    pub amount_in: Amount,
    pub reserve_in: Amount,
    pub reserve_out: Amount,
    pub fee_bps: u32,
    /// Input after fee: `amountIn * (10000 - feeBps)` (numerator units).
    pub in_after_fee: Amount,
    pub gross_out: Amount,
    /// Explicit fixed cost taken out of `gross_out`, denominated in the
    /// output asset.
    pub cost_out: Amount,
    /// What actually leaves the pool and enters the next hop.
    pub net_out: Amount,
    /// Human-checkable formula with the concrete values substituted.
    pub basis: String,
}

fn basis_string(
    amount_in: Amount,
    fee_bps: u32,
    reserve_in: Amount,
    reserve_out: Amount,
    in_after_fee: Amount,
    gross_out: Amount,
    cost_out: Amount,
) -> String {
    format!(
        "floor({in_after_fee} * {reserve_out} / ({reserve_in} * {BPS_DEN} + {in_after_fee})) = {gross_out}; \
         in_after_fee = {amount_in} * ({BPS_DEN} - {fee_bps}); net_out = {gross_out} - {cost_out}"
    )
}

/// Execute one hop exactly. Returns [`SwapError`] rather than panicking on
/// zero liquidity or domain overflow.
pub fn swap_hop(pool: &Pool, side: Side, amount_in: Amount) -> Result<SwapHop, SwapError> {
    if amount_in.is_zero() {
        return Err(SwapError::ZeroInput);
    }
    if pool.fee_bps > 10_000 {
        return Err(SwapError::BadFee(pool.fee_bps));
    }

    let (reserve_in, reserve_out, cost_out, asset_in, asset_out) = match side {
        Side::ZeroToOne => (
            pool.reserve0,
            pool.reserve1,
            pool.cost_token1_out,
            pool.token0.clone(),
            pool.token1.clone(),
        ),
        Side::OneToZero => (
            pool.reserve1,
            pool.reserve0,
            pool.cost_token0_out,
            pool.token1.clone(),
            pool.token0.clone(),
        ),
    };
    if reserve_in.is_zero() || reserve_out.is_zero() {
        return Err(SwapError::ZeroReserve);
    }

    // inAfterFee = amountIn * (10_000 - feeBps) — bounded to u128 explicitly.
    let fee_num = BPS_DEN - u128::from(pool.fee_bps);
    let in_after_fee = amount_in
        .value()
        .checked_mul(fee_num)
        .ok_or(SwapError::Overflow)?;

    let numerator = U256::from(in_after_fee) * U256::from(reserve_out.value());
    let denominator = U256::from(reserve_in.value()) * U256::from(BPS_DEN) + U256::from(in_after_fee);
    debug_assert!(!denominator.is_zero());
    let gross_u256 = numerator / denominator;
    let gross_out = Amount::from_u256(gross_u256).ok_or(SwapError::Overflow)?;

    // The floor division above always gives grossOut <= reserveOut, so this
    // subtraction is the only remaining bound to check.
    if gross_out < cost_out {
        return Err(SwapError::CostExceedsOutput {
            gross_out,
            cost_out,
        });
    }
    let net_out = Amount::from(gross_out.value() - cost_out.value());

    let basis = basis_string(
        amount_in,
        pool.fee_bps,
        reserve_in,
        reserve_out,
        Amount::from(in_after_fee),
        gross_out,
        cost_out,
    );

    Ok(SwapHop {
        pool_id: pool.id.clone(),
        asset_in,
        asset_out,
        amount_in,
        reserve_in,
        reserve_out,
        fee_bps: pool.fee_bps,
        in_after_fee: Amount::from(in_after_fee),
        gross_out,
        cost_out,
        net_out,
        basis,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pool(fee: u32, r0: u128, r1: u128) -> Pool {
        Pool {
            id: "p".into(),
            token0: "A".into(),
            token1: "B".into(),
            reserve0: Amount::from(r0),
            reserve1: Amount::from(r1),
            fee_bps: fee,
            cost_token0_out: Amount::ZERO,
            cost_token1_out: Amount::ZERO,
        }
    }

    #[test]
    fn uniswap_v2_known_vector_30bps() {
        // reserveIn=1000, reserveOut=1000, amountIn=100, fee 0.30%:
        // inAfterFee = 100*9970 = 997000; 997000*1000/(1000*10000+997000) = 90 (floor)
        let p = pool(30, 1000, 1000);
        let h = swap_hop(&p, Side::ZeroToOne, Amount::from(100u64)).unwrap();
        assert_eq!(h.in_after_fee.value(), 997_000);
        assert_eq!(h.gross_out.value(), 90);
        assert_eq!(h.net_out.value(), 90);
    }

    #[test]
    fn no_fee_equals_floor_of_cp_formula() {
        // 100 into 1000/1000 with zero fee -> floor(100*1000/1100) = 90
        let p = pool(0, 1000, 1000);
        let h = swap_hop(&p, Side::ZeroToOne, Amount::from(100u64)).unwrap();
        assert_eq!(h.gross_out.value(), 90);
    }

    #[test]
    fn output_never_exceeds_reserve() {
        // reserveIn=1, reserveOut=100: even a huge input can only drain the
        // 100 units of output, never more.
        let p = pool(0, 1, 100);
        let h = swap_hop(&p, Side::ZeroToOne, Amount::from(1_000_000_000_000u64)).unwrap();
        assert!(h.gross_out.value() <= 100);
        assert_eq!(h.gross_out.value(), 99);
        assert_eq!(h.net_out.value(), 99);
    }

    #[test]
    fn in_after_fee_overflow_is_rejected() {
        let p = pool(0, 1, 1);
        // amountIn * 10_000 overflows u128.
        let err = swap_hop(&p, Side::ZeroToOne, Amount::from(u128::MAX)).unwrap_err();
        assert_eq!(err, SwapError::Overflow);
    }

    #[test]
    fn overflow_is_reachable_inside_u128_domain() {
        // Any input >= ceil(2^128 / 10_000) makes inAfterFee overflow u128
        // even though the input itself is a perfectly valid u128 amount.
        let p = pool(0, 1_000, 1_000);
        let huge = Amount::from(10u128.pow(35)); // 1e35 > 3.4028e34
        assert_eq!(
            swap_hop(&p, Side::ZeroToOne, huge).unwrap_err(),
            SwapError::Overflow
        );
        // Just below the threshold still computes.
        let near = Amount::from(10u128.pow(33));
        assert!(swap_hop(&p, Side::ZeroToOne, near).is_ok());
    }

    #[test]
    fn explicit_cost_is_subtracted_after_fee_swap() {
        let mut p = pool(30, 1_000, 1_000);
        p.cost_token1_out = Amount::from(7u64);
        let h = swap_hop(&p, Side::ZeroToOne, Amount::from(100u64)).unwrap();
        // gross 90, net 83
        assert_eq!(h.gross_out.value(), 90);
        assert_eq!(h.cost_out.value(), 7);
        assert_eq!(h.net_out.value(), 83);
    }

    #[test]
    fn cost_larger_than_output_is_refused() {
        let mut p = pool(30, 1_000, 1_000);
        p.cost_token1_out = Amount::from(10_000u64);
        let err = swap_hop(&p, Side::ZeroToOne, Amount::from(1u64)).unwrap_err();
        assert!(matches!(err, SwapError::CostExceedsOutput { .. }));
    }

    #[test]
    fn zero_input_and_zero_reserve_are_refused() {
        let p = pool(0, 10, 10);
        assert_eq!(
            swap_hop(&p, Side::ZeroToOne, Amount::ZERO).unwrap_err(),
            SwapError::ZeroInput
        );
        let p = pool(0, 0, 10);
        assert_eq!(
            swap_hop(&p, Side::ZeroToOne, Amount::from(1u64)).unwrap_err(),
            SwapError::ZeroReserve
        );
    }

    #[test]
    fn full_fee_gives_zero_output() {
        // 100% fee -> inAfterFee=0 -> gross 0; no explicit cost -> net 0.
        let p = pool(10_000, 1000, 1000);
        let h = swap_hop(&p, Side::ZeroToOne, Amount::from(100u64)).unwrap();
        assert_eq!(h.net_out.value(), 0);
    }
}
