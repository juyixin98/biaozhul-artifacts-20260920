//! Sorted permutations, position maps and equal-value group boundaries.
//!
//! For every join key column we materialize **two** permutations:
//!
//! * [`SortedColumn::asc_rows`]  — matchable rows ordered by value ascending,
//!   ties broken by original row id ascending (stable);
//! * [`SortedColumn::desc_rows`] — the reverse permutation.
//!
//! and two position maps back into those permutations
//! ([`SortedColumn::asc_pos`] / [`SortedColumn::desc_pos`]; `-1` marks rows
//! that cannot match anything: SQL NULL and NaN).
//!
//! # Strict vs non-strict equal-value boundaries
//!
//! Equal values form contiguous groups in sorted order. The rank of a probe
//! value `v` has two distinct boundaries:
//!
//! * strict boundary     [`SortedColumn::lower_bound`] — rows with value `< v`
//!   stop *before* the equal group (the group is excluded);
//! * non-strict boundary [`SortedColumn::upper_bound`] — rows with value `<= v`
//!   stop *after* the equal group (the group is included).
//!
//! IEJoin enumeration ranges derive from exactly these two boundaries, which
//! is what makes `<` and `<=` (resp. `>` / `>=`) differ on duplicated values.

use crate::batch::{cmp_cells, try_cmp_cells, Cell, Column};
use crate::error::{JoinError, Result};

/// Orientation of a sorted traversal.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Orientation {
    Asc,
    Desc,
}

/// One column preprocessed for IEJoin.
#[derive(Debug)]
pub(crate) struct SortedColumn<'a> {
    col: &'a Column,
    /// Matchable row ids in ascending `(value, row_id)` order.
    asc_rows: Vec<u32>,
    /// Reverse of [`Self::asc_rows`].
    desc_rows: Vec<u32>,
    /// Position inside `asc_rows`, `-1` when non-matchable.
    asc_pos: Vec<i64>,
    /// Position inside `desc_rows`, `-1` when non-matchable.
    desc_pos: Vec<i64>,
    /// Ascending equal-group boundaries: group `g` spans
    /// `group_ends[g-1]..group_ends[g]` (index 0 implicitly starts at 0).
    group_ends: Vec<usize>,
}

impl<'a> SortedColumn<'a> {
    pub(crate) fn build(col: &'a Column) -> Self {
        let n = col.len();
        let mut rows: Vec<u32> = (0..n)
            .filter_map(|r| col.key_at(r).map(|_| r as u32))
            .collect();
        // Stable sort by value, then row id. key_at is guaranteed Some here.
        rows.sort_by(|&x, &y| {
            cmp_cells(col.key_at(x as usize).unwrap(), col.key_at(y as usize).unwrap())
                .then_with(|| x.cmp(&y))
        });

        let mut group_ends = Vec::new();
        let mut last_key: Option<Cell<'_>> = None;
        for (i, &r) in rows.iter().enumerate() {
            let key = col.key_at(r as usize).unwrap();
            match last_key {
                Some(k) if cmp_cells(k, key).is_eq() => {}
                Some(_) => group_ends.push(i),
                None => {}
            }
            last_key = Some(key);
        }
        if !rows.is_empty() {
            group_ends.push(rows.len());
        }

        let mut asc_pos = vec![-1i64; n];
        for (p, &r) in rows.iter().enumerate() {
            asc_pos[r as usize] = p as i64;
        }
        let desc_rows: Vec<u32> = rows.iter().rev().copied().collect();
        let mut desc_pos = vec![-1i64; n];
        for (p, &r) in desc_rows.iter().enumerate() {
            desc_pos[r as usize] = p as i64;
        }

        SortedColumn { col, asc_rows: rows, desc_rows, asc_pos, desc_pos, group_ends }
    }

    #[must_use]
    pub(crate) fn matchable_count(&self) -> usize {
        self.asc_rows.len()
    }

    /// Total number of original rows (including NULL / NaN rows).
    #[must_use]
    pub(crate) fn total_len(&self) -> usize {
        self.col.len()
    }

    #[must_use]
    pub(crate) fn rows(&self, orientation: Orientation) -> &[u32] {
        match orientation {
            Orientation::Asc => &self.asc_rows,
            Orientation::Desc => &self.desc_rows,
        }
    }

    /// Position of right row `r` in the given permutation; `None` if the row
    /// is non-matchable (NULL / NaN).
    #[must_use]
    pub(crate) fn position(&self, r: usize, orientation: Orientation) -> Option<usize> {
        let p = match orientation {
            Orientation::Asc => self.asc_pos[r],
            Orientation::Desc => self.desc_pos[r],
        };
        (p >= 0).then_some(p as usize)
    }

    /// Full inverse position vector for an orientation (`-1` = non-matchable).
    #[must_use]
    pub(crate) fn positions(&self, orientation: Orientation) -> &[i64] {
        match orientation {
            Orientation::Asc => &self.asc_pos,
            Orientation::Desc => &self.desc_pos,
        }
    }

    /// Ascending equal-group ranges `(start_exclusive_or_zero, end)`.
    pub(crate) fn asc_groups(&self) -> impl Iterator<Item = (usize, usize)> + '_ {
        let mut prev = 0;
        self.group_ends.iter().map(move |&end| {
            let g = (prev, end);
            prev = end;
            g
        })
    }

    /// Equal-group ranges in the requested orientation, each tagged with a
    /// representative row id (usable with [`SortedColumn::col_key`]).
    pub(crate) fn groups(
        &self,
        orientation: Orientation,
    ) -> Vec<(usize, usize, u32)> {
        let m = self.asc_rows.len();
        match orientation {
            Orientation::Asc => self
                .asc_groups()
                .map(|(lo, hi)| (lo, hi, self.asc_rows[lo]))
                .collect(),
            Orientation::Desc => self
                .asc_groups()
                .collect::<Vec<_>>()
                .into_iter()
                // Ascending group [lo,hi) occupies descending [m-hi, m-lo).
                .map(|(lo, hi)| (m - hi, m - lo, self.asc_rows[lo]))
                .rev()
                .collect(),
        }
    }

    /// Key of a representative row (representatives are always matchable).
    pub(crate) fn col_key(&self, row: u32) -> Cell<'_> {
        self.col.key_at(row as usize).expect("group representative is matchable")
    }

    /// Number of sorted rows whose value is strictly less than `v`
    /// (strict-inequality boundary).
    pub(crate) fn lower_bound(&self, v: Cell<'_>) -> Result<usize> {
        let rows = &self.asc_rows;
        let col = self.col;
        let mut lo = 0usize;
        let mut hi = rows.len();
        while lo < hi {
            let mid = lo + (hi - lo) / 2;
            let key = col.key_at(rows[mid] as usize).unwrap();
            let ord = try_cmp_cells(key, v).ok_or_else(|| {
                JoinError::compute(
                    "incomparable_cells",
                    "lower_bound probe key has a different physical type",
                )
            })?;
            if ord.is_lt() {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        Ok(lo)
    }

    /// Number of sorted rows whose value is less than or equal to `v`
    /// (non-strict-inequality boundary).
    pub(crate) fn upper_bound(&self, v: Cell<'_>) -> Result<usize> {
        let rows = &self.asc_rows;
        let col = self.col;
        let mut lo = 0usize;
        let mut hi = rows.len();
        while lo < hi {
            let mid = lo + (hi - lo) / 2;
            let key = col.key_at(rows[mid] as usize).unwrap();
            let ord = try_cmp_cells(key, v).ok_or_else(|| {
                JoinError::compute(
                    "incomparable_cells",
                    "upper_bound probe key has a different physical type",
                )
            })?;
            if ord.is_le() {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        Ok(lo)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::batch::Column;

    #[test]
    fn permutations_positions_and_group_bounds_with_dupes_and_nulls() {
        // values: NULL, 5, 5, 1, NaN, 1  (matchable rows 1,2,3,5)
        let col = Column::from_f64(vec![
            None,
            Some(5.0),
            Some(5.0),
            Some(1.0),
            Some(f64::NAN),
            Some(1.0),
        ]);
        let s = SortedColumn::build(&col);
        assert_eq!(s.matchable_count(), 4);
        assert_eq!(s.rows(Orientation::Asc), &[3u32, 5, 1, 2]);
        assert_eq!(s.rows(Orientation::Desc), &[2u32, 1, 5, 3]);
        assert_eq!(s.position(0, Orientation::Asc), None);
        assert_eq!(s.position(4, Orientation::Asc), None, "NaN excluded");
        assert_eq!(s.position(3, Orientation::Asc), Some(0));
        assert_eq!(s.position(2, Orientation::Desc), Some(0));

        let groups = s.groups(Orientation::Asc);
        assert_eq!(groups.iter().map(|(lo, hi, _)| (*lo, *hi)).collect::<Vec<_>>(),
                   vec![(0, 2), (2, 4)]);

        // Strict boundary excludes the equal group, non-strict includes it.
        assert_eq!(s.lower_bound(Cell::F64(1.0)).unwrap(), 0);
        assert_eq!(s.upper_bound(Cell::F64(1.0)).unwrap(), 2);
        assert_eq!(s.lower_bound(Cell::F64(5.0)).unwrap(), 2);
        assert_eq!(s.upper_bound(Cell::F64(5.0)).unwrap(), 4);
        // Value absent in column: boundaries coincide at insertion rank.
        assert_eq!(s.lower_bound(Cell::F64(3.0)).unwrap(), 2);
        assert_eq!(s.upper_bound(Cell::F64(3.0)).unwrap(), 2);
    }
}
