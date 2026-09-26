//! Reed-Solomon stripe coding over GF(2^8).
//!
//! The finite-field arithmetic (addition as XOR, multiplication modulo the
//! irreducible polynomial x^8+x^4+x^3+x^2+1, i.e. `0x11d`, with primitive
//! element `0x02`) is provided by the mature [`gf256`] crate.
//!
//! Everything else in this module is implemented from scratch:
//!
//! * the **systematic coding matrix** `C` — an identity block for the `k` data
//!   shards followed by a Cauchy parity block;
//! * **Gauss-Jordan inversion** of the survivor sub-matrix;
//! * **stripe encoding** (parity generation) and **stripe recovery**
//!   (reconstruction of missing data and parity shards).
//!
//! # Why a Cauchy parity block
//!
//! Parity entry `C[k+p][j] = 1 / (x_p + y_j)` uses disjoint point sets
//! `y_j = j` (`0..k-1`) and `x_p = k + p` (`k..k+m-1`), so every
//! `x_p + y_j` is nonzero and the reciprocal exists. Every square submatrix
//! of a Cauchy matrix is itself Cauchy and nonsingular; consequently
//! `C = [I; Cauchy]` is MDS over GF(2^8) and **any** selection of `k` rows
//! (surviving shards) is invertible. (A naive Vandermonde parity block does
//! not give this guarantee on small fields: submatrices with non-consecutive
//! exponents can be singular.)
//!
//! # Recovery guarantee
//!
//! Any `k` out of `n = k + m` shards suffice to reconstruct the stripe.
//! Recovery is therefore promised **only when at most `m` shards are
//! missing**. With more than `m` missing shards
//! [`Error::NotEnoughShards`] is returned.
//!
//! # Silent corruption is out of scope
//!
//! This is an *erasure* code, not an error-correcting decoder: a present but
//! corrupted shard is treated as authoritative and the reconstructed bytes
//! will be silently wrong. Integrity must be established externally (the
//! container layer uses a SHA-256 of the plaintext for this).

use std::collections::BTreeMap;

use gf256::gf256;

use crate::error::{Error, Result};

/// Maximum total number of shards. Parity evaluation points are distinct
/// nonzero field elements, of which GF(2^8) has exactly 255; data rows use
/// the identity block and do not consume evaluation points.
pub const MAX_SHARDS: usize = 256;

/// Build the systematic `n x k` coding matrix `C` (`n = k + m`), row-major.
///
/// Rows `0..k` form the identity matrix (a data shard row selects itself).
/// Row `k + p` is the Cauchy parity row with entries
/// `1 / (x_p + y_j)` for `y_j = j` and `x_p = k + p`, where field addition
/// is XOR. The point sets `{0, …, k-1}` and `{k, …, k+m-1}` are disjoint, so
/// no denominator vanishes and any `k` selected rows form an invertible
/// matrix (the MDS property).
pub(crate) fn coding_matrix(k: usize, m: usize) -> Vec<Vec<gf256>> {
    let n = k + m;
    let mut matrix = vec![vec![gf256(0); k]; n];
    for (row, diag) in matrix.iter_mut().take(k).zip(0..k) {
        row[diag] = gf256(1);
    }
    for (p, row) in matrix.iter_mut().skip(k).enumerate() {
        let x = gf256((k + p) as u8);
        for (j, cell) in row.iter_mut().enumerate() {
            let y = gf256(j as u8);
            // x != y by construction, so the reciprocal exists.
            *cell = x.add(y).recip();
        }
    }
    matrix
}

/// Invert a square matrix over GF(2^8) with Gauss-Jordan elimination.
///
/// Returns `None` if the matrix is singular.
pub(crate) fn invert(matrix: &[Vec<gf256>]) -> Option<Vec<Vec<gf256>>> {
    let k = matrix.len();
    if k == 0 || matrix.iter().any(|row| row.len() != k) {
        return None;
    }

    // Augmented matrix [M | I].
    let mut aug = vec![vec![gf256(0); 2 * k]; k];
    for i in 0..k {
        aug[i][..k].copy_from_slice(&matrix[i]);
        aug[i][k + i] = gf256(1);
    }

    for col in 0..k {
        // Partial pivoting: find a row with a nonzero coefficient in `col`.
        let pivot = (col..k).find(|&r| aug[r][col] != gf256(0))?;
        aug.swap(col, pivot);

        // Scale the pivot row so its pivot coefficient becomes 1.
        let inv = aug[col][col].recip();
        for cell in aug[col].iter_mut() {
            *cell = cell.mul(inv);
        }

        // Eliminate the column from every other row (addition is XOR).
        // Split around the pivot row so its immutable borrow can coexist with
        // mutable borrows of all other rows.
        let (above, rest) = aug.split_at_mut(col);
        let (pivot_row, below) = rest.split_first_mut().expect("pivot row exists");
        for row in above.iter_mut().chain(below.iter_mut()) {
            let factor = row[col];
            if factor == gf256(0) {
                continue;
            }
            for (target, &pivot_cell) in row.iter_mut().zip(pivot_row.iter()) {
                *target = target.add(pivot_cell.mul(factor));
            }
        }
    }

    Some(aug.into_iter().map(|row| row[k..].to_vec()).collect())
}

/// `dst[t] ^= c * src[t]` over the field, one stripe column at a time.
fn add_mul(dst: &mut [u8], src: &[u8], coefficient: gf256) {
    if coefficient == gf256(0) {
        return;
    }
    for (d, &s) in dst.iter_mut().zip(src.iter()) {
        *d ^= gf256(s).mul(coefficient).get();
    }
}

/// Validate `(data_shards, parity_shards, stripe_size)` coding parameters.
pub(crate) fn validate_params(k: usize, m: usize, stripe_size: usize) -> Result<()> {
    if k == 0 {
        return Err(Error::InvalidParams("data_shards must be >= 1".into()));
    }
    if m == 0 {
        return Err(Error::InvalidParams(
            "parity_shards must be >= 1 (no redundancy otherwise)".into(),
        ));
    }
    if k + m > MAX_SHARDS {
        return Err(Error::InvalidParams(format!(
            "data_shards + parity_shards must be <= {MAX_SHARDS}"
        )));
    }
    if stripe_size == 0 {
        return Err(Error::InvalidParams("stripe_size must be >= 1".into()));
    }
    Ok(())
}

/// Encode one equal-length stripe into parity shards.
///
/// `data` must contain exactly `k` slices of identical length; returns `m`
/// parity shards of that length.
pub fn encode_stripe(data: &[&[u8]], m: usize) -> Result<Vec<Vec<u8>>> {
    let k = data.len();
    if k == 0 || k + m > MAX_SHARDS || m == 0 {
        return Err(Error::InvalidParams(format!(
            "require 1 <= data_shards and 1 <= parity_shards and k+m <= {MAX_SHARDS}"
        )));
    }
    let len = data[0].len();
    if data.iter().any(|d| d.len() != len) {
        return Err(Error::InvalidParams(
            "all data shards in a stripe must have equal length".into(),
        ));
    }

    let matrix = coding_matrix(k, m);
    let mut parity = vec![vec![0u8; len]; m];
    for p in 0..m {
        for j in 0..k {
            add_mul(&mut parity[p], data[j], matrix[k + p][j]);
        }
    }
    Ok(parity)
}

/// Reconstruct every missing shard of one stripe.
///
/// * `k` / `m`: data / parity shard counts (`n = k + m`);
/// * `stripe`: stripe index, used only in error reporting;
/// * `stripe_len`: length in bytes of every shard in this (padded) stripe;
/// * `present`: shard id -> shard bytes for the shards that survived.
///
/// Returns shard id -> reconstructed bytes for each missing shard. Fails with
/// [`Error::NotEnoughShards`] when fewer than `k` shards are present and with
/// [`Error::LengthMismatch`] when a present shard has the wrong length.
pub fn recover_stripe(
    k: usize,
    m: usize,
    stripe: u32,
    stripe_len: usize,
    present: &BTreeMap<u8, Vec<u8>>,
) -> Result<BTreeMap<u8, Vec<u8>>> {
    validate_params(k, m, stripe_len)?;
    let n = k + m;

    if let Some(&id) = present.keys().find(|&&id| id as usize >= n) {
        return Err(Error::BadRecord(format!(
            "shard id {id} out of range 0..{n}"
        )));
    }
    if present.len() < k {
        return Err(Error::NotEnoughShards {
            stripe,
            present: present.len(),
            needed: k,
        });
    }
    for (&id, bytes) in present {
        if bytes.len() != stripe_len {
            return Err(Error::LengthMismatch {
                stripe,
                shard: id,
                got: bytes.len() as u64,
                expected: stripe_len as u64,
            });
        }
    }

    // Any k survivors suffice; take the k smallest ids deterministically.
    let chosen: Vec<u8> = present.keys().copied().take(k).collect();
    let coding = coding_matrix(k, m);
    let survivor: Vec<Vec<gf256>> = chosen
        .iter()
        .map(|&id| coding[id as usize].clone())
        .collect();
    let inverse = invert(&survivor).ok_or(Error::SingularMatrix)?;

    let present_slice: Vec<&[u8]> = chosen.iter().map(|id| present[id].as_slice()).collect();

    let mut recovered: BTreeMap<u8, Vec<u8>> = BTreeMap::new();
    let chosen_set: std::collections::BTreeSet<u8> = chosen.iter().copied().collect();

    // Reconstruct missing data shards: D = A^{-1} * P_survivors.
    let mut full_data: Vec<Vec<u8>> = Vec::with_capacity(k);
    for (data_id, inverse_row) in inverse.iter().enumerate() {
        if let Some(bytes) = present.get(&(data_id as u8)) {
            full_data.push(bytes.clone());
        } else {
            let mut out = vec![0u8; stripe_len];
            for (survivor_bytes, &coefficient) in present_slice.iter().zip(inverse_row.iter()) {
                add_mul(&mut out, survivor_bytes, coefficient);
            }
            recovered.insert(data_id as u8, out.clone());
            full_data.push(out);
        }
    }

    // Reconstruct any missing parity shards by re-encoding the full data.
    let data_refs: Vec<&[u8]> = full_data.iter().map(Vec::as_slice).collect();
    let parity = encode_stripe(&data_refs, m)?;
    for (p, parity_shard) in parity.iter().enumerate() {
        let id = (k + p) as u8;
        if !chosen_set.contains(&id) && !present.contains_key(&id) {
            recovered.insert(id, parity_shard.clone());
        }
    }

    Ok(recovered)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn coding_matrix_is_systematic_cauchy() {
        let c = coding_matrix(3, 2);
        assert_eq!(c[0], vec![gf256(1), gf256(0), gf256(0)]);
        assert_eq!(c[1], vec![gf256(0), gf256(1), gf256(0)]);
        assert_eq!(c[2], vec![gf256(0), gf256(0), gf256(1)]);
        // Parity row p=0: x = k + 0 = 3, entries 1/(3 XOR y_j).
        assert_eq!(c[3][0], gf256(3).add(gf256(0)).recip());
        assert_eq!(c[3][1], gf256(3).add(gf256(1)).recip());
        assert_eq!(c[3][2], gf256(3).add(gf256(2)).recip());
        // Parity row p=1: x = 4.
        assert_eq!(c[4][0], gf256(4).recip());
        assert_eq!(c[4][1], gf256(4).add(gf256(1)).recip());
        assert_eq!(c[4][2], gf256(4).add(gf256(2)).recip());
    }

    #[test]
    fn every_k_row_selection_is_invertible() {
        // Enumerate small parameters and every survivor set of size k.
        // This is the MDS property of the systematic Cauchy matrix: any k of
        // the n rows form a nonsingular k x k matrix over GF(2^8).
        for k in 1..=5usize {
            for m in 1..=3usize {
                let n = k + m;
                let c = coding_matrix(k, m);
                for mask in 0u32..(1u32 << n) {
                    if mask.count_ones() as usize != k {
                        continue;
                    }
                    let rows: Vec<Vec<gf256>> = (0..n)
                        .filter(|i| (mask >> i) & 1 == 1)
                        .map(|i| c[i].clone())
                        .collect();
                    assert!(
                        invert(&rows).is_some(),
                        "singular selection k={k} m={m} mask={mask:b}"
                    );
                }
            }
        }
    }

    fn sample_data(k: usize, len: usize, seed: u8) -> Vec<Vec<u8>> {
        (0..k)
            .map(|j| (0..len).map(|t| seed ^ j as u8 ^ t as u8).collect())
            .collect()
    }

    #[test]
    fn mds_at_larger_parameters() {
        // k=8, m=4: enumerate every C(12,8)=495 survivor combination.
        let (k, m) = (8usize, 4usize);
        let n = k + m;
        let c = coding_matrix(k, m);
        let mut combinations: u32 = 0;
        for mask in 0u32..(1u32 << n) {
            if mask.count_ones() as usize != k {
                continue;
            }
            let rows: Vec<Vec<gf256>> = (0..n)
                .filter(|i| (mask >> i) & 1 == 1)
                .map(|i| c[i].clone())
                .collect();
            assert!(invert(&rows).is_some(), "singular mask {mask:b}");
            combinations += 1;
        }
        assert_eq!(combinations, 495);
    }

    #[test]
    fn recover_enumerated_small_loss_patterns() {
        let k = 3;
        let m = 2;
        let n = k + m;
        let len = 64;
        let data = sample_data(k, len, 0x5a);
        let data_refs: Vec<&[u8]> = data.iter().map(|v| v.as_slice()).collect();
        let parity = encode_stripe(&data_refs, m).unwrap();
        let mut all: Vec<Vec<u8>> = data.clone();
        all.extend(parity);

        // Every loss pattern with at most m = 2 missing shards recovers.
        for missing_mask in 0u32..(1u32 << n) {
            if missing_mask.count_ones() > m as u32 {
                continue;
            }
            let mut present = BTreeMap::new();
            for (id, shard_bytes) in all.iter().enumerate() {
                if missing_mask & (1 << id) == 0 {
                    present.insert(id as u8, shard_bytes.clone());
                }
            }
            let recovered = recover_stripe(k, m, 7, len, &present).unwrap();
            for (id, shard_bytes) in all.iter().enumerate() {
                if missing_mask & (1 << id) != 0 {
                    assert_eq!(
                        &recovered[&(id as u8)],
                        shard_bytes,
                        "mask {missing_mask:b} shard {id}"
                    );
                }
            }
        }
    }

    #[test]
    fn too_many_missing_is_rejected() {
        let k = 3;
        let m = 2;
        let len = 16;
        let data = sample_data(k, len, 0);
        let data_refs: Vec<&[u8]> = data.iter().map(|v| v.as_slice()).collect();
        let parity = encode_stripe(&data_refs, m).unwrap();

        // 3 missing > m = 2, only 2 survivors.
        let mut present = BTreeMap::new();
        present.insert(0u8, data[0].clone());
        present.insert(4u8, parity[1].clone());
        let err = recover_stripe(k, m, 0, len, &present).unwrap_err();
        assert!(matches!(
            err,
            Error::NotEnoughShards {
                present: 2,
                needed: 3,
                ..
            }
        ));
    }

    #[test]
    fn length_mismatch_is_rejected() {
        let k = 2;
        let m = 1;
        let len = 16;
        let data = sample_data(k, len, 9);
        let mut present = BTreeMap::new();
        present.insert(0u8, data[0].clone());
        present.insert(2u8, vec![0u8; len - 1]); // parity one byte short
        let err = recover_stripe(k, m, 3, len, &present).unwrap_err();
        assert!(matches!(
            err,
            Error::LengthMismatch {
                stripe: 3,
                shard: 2,
                ..
            }
        ));
    }
}
