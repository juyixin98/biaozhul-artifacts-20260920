//! Per-stripe Reed-Solomon encode / erasure reconstruct, built on the
//! self-implemented matrix routines in [`crate::gfmat`].

use crate::error::{Error, Result};
use crate::gfmat;
use gf256::gf256;

/// A reusable coder for one (k, m) parameter set. Holds the encoding matrix.
pub struct StripeCoder {
    pub k: usize,
    pub m: usize,
    matrix: Vec<Vec<gf256>>,
}

impl StripeCoder {
    pub fn new(k: usize, m: usize) -> Result<Self> {
        if k == 0 || m == 0 || k + m > 255 {
            return Err(Error::InvalidParams(format!(
                "need 1 <= k, 1 <= m, k+m <= 255, got k={k} m={m}"
            )));
        }
        Ok(StripeCoder {
            k,
            m,
            matrix: gfmat::encoding_matrix(k, m),
        })
    }

    /// Compute the m parity blocks from the k data blocks of one stripe.
    /// All blocks must be `stripe_size` bytes.
    pub fn encode_parity(&self, data: &[&[u8]], parity: &mut [&mut [u8]]) {
        debug_assert_eq!(data.len(), self.k);
        debug_assert_eq!(parity.len(), self.m);
        for p in parity.iter_mut() {
            p.fill(0);
        }
        for (j, p) in parity.iter_mut().enumerate() {
            for (i, d) in data.iter().enumerate() {
                let coeff = self.matrix[self.k + j][i];
                gfmat::mul_add_bytes(p, coeff, d);
            }
        }
    }

    /// Reconstruct the missing DATA shards of one stripe in place.
    ///
    /// `blocks` has k+m entries in shard order; `Some` for shards that are
    /// present, `None` for shards marked as known erasures. On success every
    /// missing data shard is filled in. Missing parity shards are left as
    /// `None` (they are not needed to emit the original data; callers that
    /// want them can re-run [`Self::encode_parity`]).
    ///
    /// Only erasure recovery is supported: the caller must know WHICH shards
    /// are missing. Recovery is guaranteed for at most m erasures; more
    /// erasures return [`Error::TooManyErasures`].
    pub fn reconstruct_data(&self, blocks: &mut [Option<Vec<u8>>]) -> Result<()> {
        debug_assert_eq!(blocks.len(), self.k + self.m);
        let missing_total = blocks.iter().filter(|b| b.is_none()).count();
        if missing_total > self.m {
            return Err(Error::TooManyErasures {
                missing: missing_total,
                parity: self.m,
            });
        }
        let missing_data: Vec<usize> = (0..self.k).filter(|&i| blocks[i].is_none()).collect();
        if missing_data.is_empty() {
            return Ok(());
        }

        // Pick any k present shards and solve A * data = present for the
        // original data vector, where A is the corresponding row submatrix
        // of the encoding matrix.
        let present_idx: Vec<usize> = (0..self.k + self.m).filter(|&i| blocks[i].is_some()).collect();
        if present_idx.len() < self.k {
            return Err(Error::TooManyErasures {
                missing: missing_total,
                parity: self.m,
            });
        }
        let chosen = &present_idx[..self.k];
        let sub: Vec<Vec<gf256>> = chosen.iter().map(|&r| self.matrix[r].clone()).collect();
        let inv = gfmat::invert(&sub).ok_or(Error::SingularMatrix)?;

        let stripe_size = blocks[chosen[0]].as_ref().unwrap().len();
        for &d in &missing_data {
            let mut out = vec![0u8; stripe_size];
            for (c, &src) in chosen.iter().enumerate() {
                let coeff = inv[d][c];
                gfmat::mul_add_bytes(&mut out, coeff, blocks[src].as_ref().unwrap());
            }
            blocks[d] = Some(out);
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_stripe(k: usize, m: usize, size: usize, seed: u8) -> Vec<Vec<u8>> {
        // Deterministic pseudo-random data blocks + computed parity.
        let mut blocks: Vec<Vec<u8>> = (0..k)
            .map(|i| {
                (0..size)
                    .map(|j| seed.wrapping_mul(31).wrapping_add((i * 7 + j) as u8))
                    .collect()
            })
            .collect();
        let coder = StripeCoder::new(k, m).unwrap();
        let data_refs: Vec<&[u8]> = blocks.iter().map(|b| b.as_slice()).collect();
        let mut parity: Vec<Vec<u8>> = vec![vec![0u8; size]; m];
        {
            let mut parity_refs: Vec<&mut [u8]> = parity.iter_mut().map(|p| p.as_mut_slice()).collect();
            coder.encode_parity(&data_refs, &mut parity_refs);
        }
        blocks.extend(parity);
        blocks
    }

    #[test]
    fn every_erasure_pattern_up_to_m_recovers() {
        for &(k, m) in &[(1usize, 1usize), (2, 1), (4, 2), (5, 3)] {
            let coder = StripeCoder::new(k, m).unwrap();
            let original = make_stripe(k, m, 64, 3);
            let n = k + m;
            for mask in 0u64..(1 << n) {
                let erased = mask.count_ones() as usize;
                if erased > m {
                    continue;
                }
                let mut blocks: Vec<Option<Vec<u8>>> = original
                    .iter()
                    .enumerate()
                    .map(|(i, b)| {
                        if mask & (1 << i) != 0 {
                            None
                        } else {
                            Some(b.clone())
                        }
                    })
                    .collect();
                coder.reconstruct_data(&mut blocks).unwrap();
                for i in 0..k {
                    assert_eq!(
                        blocks[i].as_ref().unwrap(),
                        &original[i],
                        "k={k} m={m} mask={mask:#x} shard={i}"
                    );
                }
            }
        }
    }

    #[test]
    fn more_than_m_erasures_fail() {
        let coder = StripeCoder::new(3, 2).unwrap();
        let mut blocks: Vec<Option<Vec<u8>>> = vec![None, None, None, Some(vec![1; 8]), Some(vec![2; 8])];
        assert!(matches!(
            coder.reconstruct_data(&mut blocks),
            Err(Error::TooManyErasures { .. })
        ));
    }
}
