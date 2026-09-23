//! Constant-product (x*y=k) single-pool swap math.
//!
//! Each hop is evaluated independently with the pool's own fee in basis
//! points and integer (floor) rounding — exactly the way an on-chain router
//! would execute it. Multi-hop paths never multiply marginal prices: the
//! floored output of hop N becomes the input of hop N+1.

use crate::uint256::U256;

/// Fee denominator used by Uniswap-V2-style pools: fee in basis points
/// (1 bp = 0.01%). `fee_bps = 30` means 0.30%.
pub const FEE_DEN: u128 = 10_000;

/// Error while simulating one swap.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SwapError {
    /// One of the reserves is zero — trading through this pool is impossible.
    ZeroReserve,
    /// Input amount is zero; no trade to simulate.
    ZeroInput,
    /// A 256-bit intermediate overflowed (mirrors an EVM revert).
    Overflow,
}

impl std::fmt::Display for SwapError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SwapError::ZeroReserve => write!(f, "pool has zero reserve"),
            SwapError::ZeroInput => write!(f, "zero input amount"),
            SwapError::Overflow => write!(f, "arithmetic overflow while simulating swap"),
        }
    }
}

/// Result of a single swap: exact integer output and the fee that was
/// effectively taken in input-token units.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Swap {
    pub amount_out: u128,
    pub fee_paid_in: u128,
}

/// Simulate `amount_in` of `token_in` against a pool holding the two reserves.
///
/// ```text
/// amount_in_with_fee = amount_in * (10000 - fee_bps)
/// numerator          = amount_in_with_fee * reserve_out
/// denominator        = reserve_in * 10000 + amount_in_with_fee
/// amount_out         = floor(numerator / denominator)
/// ```
///
/// All intermediates are checked 256-bit operations. A zero floored output
/// is a legal result (dust trade) — the caller decides whether the route
/// remains feasible.
pub fn get_amount_out(
    amount_in: u128,
    reserve_in: u128,
    reserve_out: u128,
    fee_bps: u32,
) -> Result<Swap, SwapError> {
    if reserve_in == 0 || reserve_out == 0 {
        return Err(SwapError::ZeroReserve);
    }
    if amount_in == 0 {
        return Err(SwapError::ZeroInput);
    }
    if fee_bps >= FEE_DEN as u32 {
        // Guard regardless of ingestion validation: a 100%+ fee makes the
        // numerator zero and the formula meaningless.
        return Err(SwapError::Overflow);
    }

    let fee_factor = FEE_DEN - fee_bps as u128;

    let amount_in_256 = U256::from_u128(amount_in);
    let in_with_fee = amount_in_256
        .checked_mul_u128(fee_factor)
        .ok_or(SwapError::Overflow)?;
    let numerator = in_with_fee
        .checked_mul_u128(reserve_out)
        .ok_or(SwapError::Overflow)?;

    let reserve_in_scaled = U256::from_u128(reserve_in)
        .checked_mul_u128(FEE_DEN)
        .ok_or(SwapError::Overflow)?;
    let denominator = reserve_in_scaled
        .checked_add(in_with_fee)
        .ok_or(SwapError::Overflow)?;

    let amount_out = numerator
        .checked_div(denominator)
        .ok_or(SwapError::Overflow)?
        .to_u128()
        .ok_or(SwapError::Overflow)?;

    let fee_paid_in = amount_in
        .checked_mul(fee_bps as u128)
        .map(|x| x / FEE_DEN)
        .ok_or(SwapError::Overflow)?;

    Ok(Swap {
        amount_out,
        fee_paid_in,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Uniswap V2 formula: 1e18 in, reserves 1e19 each, 0.30% fee.
    /// floor(1e18*9970*1e19 / (1e19*10000 + 1e18*9970)) = 906610893880149131.
    #[test]
    fn uniswap_reference_case() {
        let out = get_amount_out(1_000_000_000_000_000_000, 10_000_000_000_000_000_000, 10_000_000_000_000_000_000, 30).unwrap();
        assert_eq!(out.amount_out, 906_610_893_880_149_131);
        // 1e18 * 30 / 10000
        assert_eq!(out.fee_paid_in, 3_000_000_000_000_000);
    }

    #[test]
    fn zero_fee_rounds_down() {
        // floor(10*5/10+10) = 2
        let out = get_amount_out(10, 10, 5, 0).unwrap();
        assert_eq!(out.amount_out, 2);
    }

    #[test]
    fn zero_reserves_rejected() {
        assert_eq!(
            get_amount_out(1, 0, 100, 0).unwrap_err(),
            SwapError::ZeroReserve
        );
        assert_eq!(
            get_amount_out(1, 100, 0, 0).unwrap_err(),
            SwapError::ZeroReserve
        );
    }

    #[test]
    fn zero_input_rejected() {
        assert_eq!(
            get_amount_out(0, 100, 100, 30).unwrap_err(),
            SwapError::ZeroInput
        );
    }

    #[test]
    fn dust_trade_returns_zero_output() {
        // Tiny input against huge reserves floors to zero — not an error at
        // pool level; the router rejects the route for non-positive output.
        let out = get_amount_out(1, u128::MAX / 2, u128::MAX / 2, 30).unwrap();
        assert_eq!(out.amount_out, 0);
    }

    #[test]
    fn huge_values_overflow_is_detected() {
        // Multiplying ~u128::MAX inputs against ~u128::MAX reserves must
        // overflow the 256-bit intermediate.
        let r = get_amount_out(u128::MAX - 1, u128::MAX - 1, u128::MAX - 1, 30);
        assert_eq!(r.unwrap_err(), SwapError::Overflow);
    }

    #[test]
    fn large_but_valid_values_compute() {
        // (1e30)^2 fits well inside 256 bits (10^60 < 2^200).
        let r = 1_000_000_000_000_000_000_000_000_000_000u128; // 1e30
        let out = get_amount_out(r, r, r, 30).unwrap();
        assert!(out.amount_out > 0);
    }
}
