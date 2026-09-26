//! Matrix construction and inversion over GF(2^8).
//!
//! The finite-field arithmetic itself comes from the mature `gf256` crate
//! (polynomial 0x11d, generator 0x2, log/exp-table based). Everything in
//! this module — the systematic Vandermonde encoding matrix, Gauss-Jordan
//! inversion, and applying coefficient rows to byte slices — is implemented
//! here, in this project.

use gf256::gf256;

/// Build the (k+m) x k systematic encoding matrix.
///
/// Construction (the classical systematic-Reed-Solomon transform):
///
/// 1. Start from the full (k+m) x k Vandermonde matrix
///    `V[r][i] = x_r^i` with distinct evaluation points `x_r = r`.
/// 2. Right-multiply by the inverse of its top k rows:
///    `E = V * V_top^{-1}`.
///
/// The top k rows of `E` are then exactly the identity (data shards pass
/// through), and the bottom m rows are the parity coefficients.
///
/// Any k rows of `E` are linearly independent: picking any k row indices
/// gives `E_sub = V_sub * V_top^{-1}`, where `V_sub` is a k x k Vandermonde
/// matrix with consecutive powers 0..k-1 of distinct points — always
/// invertible — and `V_top^{-1}` is invertible by construction. (A naive
/// `[I; V]` stacking does NOT have this property in characteristic 2:
/// arbitrary exponent subsets of a Vandermonde can be singular, which is
/// why the transform in step 2 is required.)
pub fn encoding_matrix(k: usize, m: usize) -> Vec<Vec<gf256>> {
    let n = k + m;
    // Full Vandermonde over distinct points x_r = r.
    let vand: Vec<Vec<gf256>> = (0..n)
        .map(|r| {
            let x = gf256(r as u8);
            let mut row = Vec::with_capacity(k);
            let mut power = gf256(1); // x^0
            for _ in 0..k {
                row.push(power);
                power = power * x;
            }
            row
        })
        .collect();
    let top_inv = invert(&vand[..k].to_vec()).expect("square Vandermonde is invertible");
    mat_mul(&vand, &top_inv)
}

/// (a x b) matrix product; a is n x k, b is k x l.
fn mat_mul(a: &[Vec<gf256>], b: &[Vec<gf256>]) -> Vec<Vec<gf256>> {
    let n = a.len();
    let k = b.len();
    let l = b[0].len();
    let mut out = vec![vec![gf256(0); l]; n];
    for (i, row) in out.iter_mut().enumerate() {
        for (j, cell) in row.iter_mut().enumerate() {
            let mut acc = gf256(0);
            for t in 0..k {
                acc = acc + a[i][t] * b[t][j];
            }
            *cell = acc;
        }
    }
    out
}

/// Invert a square n x n matrix over GF(2^8) by Gauss-Jordan elimination
/// with partial pivoting. Returns `None` if the matrix is singular.
pub fn invert(a: &[Vec<gf256>]) -> Option<Vec<Vec<gf256>>> {
    let n = a.len();
    if n == 0 || a.iter().any(|row| row.len() != n) {
        return None;
    }
    // Augment [A | I].
    let mut aug: Vec<Vec<gf256>> = a
        .iter()
        .enumerate()
        .map(|(i, row)| {
            let mut r = row.clone();
            r.resize(2 * n, gf256(0));
            r[n + i] = gf256(1);
            r
        })
        .collect();

    for col in 0..n {
        // Find a pivot row with a non-zero entry in this column.
        let pivot = (col..n).find(|&r| aug[r][col] != gf256(0))?;
        aug.swap(col, pivot);

        // Normalize the pivot row so the pivot becomes 1.
        let inv_pivot = gf256(1) / aug[col][col];
        if inv_pivot != gf256(1) {
            for c in col..2 * n {
                aug[col][c] = aug[col][c] * inv_pivot;
            }
        }

        // Eliminate this column from every other row.
        for r in 0..n {
            if r == col {
                continue;
            }
            let factor = aug[r][col];
            if factor == gf256(0) {
                continue;
            }
            for c in col..2 * n {
                aug[r][c] = aug[r][c] - factor * aug[col][c];
            }
        }
    }

    Some(aug.into_iter().map(|row| row[n..].to_vec()).collect())
}

/// out[i] ^= coeff * input[i] for every position, over GF(2^8).
pub fn mul_add_bytes(out: &mut [u8], coeff: gf256, input: &[u8]) {
    debug_assert_eq!(out.len(), input.len());
    if coeff == gf256(0) {
        return;
    }
    if coeff == gf256(1) {
        for (o, &i) in out.iter_mut().zip(input.iter()) {
            *o ^= i;
        }
        return;
    }
    for (o, &i) in out.iter_mut().zip(input.iter()) {
        let v = coeff * gf256(i);
        *o ^= u8::from(v);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mat_mul(a: &[Vec<gf256>], b: &[Vec<gf256>]) -> Vec<Vec<gf256>> {
        let n = a.len();
        let mut out = vec![vec![gf256(0); n]; n];
        for (i, row) in out.iter_mut().enumerate() {
            for (j, cell) in row.iter_mut().enumerate() {
                let mut acc = gf256(0);
                for t in 0..n {
                    acc = acc + a[i][t] * b[t][j];
                }
                *cell = acc;
            }
        }
        out
    }

    #[test]
    fn inverse_of_random_submatrix_roundtrips() {
        // For several (k, m), every choice of k rows from the (k+m) x k
        // encoding matrix must be invertible, and A * A^-1 == I.
        for &(k, m) in &[(1usize, 1usize), (2, 1), (4, 2), (6, 3), (10, 4)] {
            let matrix = encoding_matrix(k, m);
            let n_rows = k + m;
            // Enumerate all k-subsets of rows by bitmask.
            for mask in 0u64..(1 << n_rows) {
                if mask.count_ones() as usize != k {
                    continue;
                }
                let sub: Vec<Vec<gf256>> = (0..n_rows)
                    .filter(|r| mask & (1 << r) != 0)
                    .map(|r| matrix[r].clone())
                    .collect();
                let inv = invert(&sub).unwrap_or_else(|| {
                    panic!("singular submatrix for k={k} m={m} mask={mask:#x}")
                });
                let prod = mat_mul(&sub, &inv);
                for i in 0..k {
                    for j in 0..k {
                        let want = if i == j { gf256(1) } else { gf256(0) };
                        assert_eq!(prod[i][j], want, "k={k} m={m} mask={mask:#x}");
                    }
                }
            }
        }
    }

    #[test]
    fn top_rows_are_identity() {
        for &(k, m) in &[(1usize, 1usize), (4, 2), (10, 4)] {
            let matrix = encoding_matrix(k, m);
            for i in 0..k {
                for j in 0..k {
                    let want = if i == j { gf256(1) } else { gf256(0) };
                    assert_eq!(matrix[i][j], want, "k={k} m={m}");
                }
            }
        }
    }

    #[test]
    fn singular_matrix_returns_none() {
        let a = vec![vec![gf256(1), gf256(2)], vec![gf256(1), gf256(2)]];
        assert!(invert(&a).is_none());
    }
}
