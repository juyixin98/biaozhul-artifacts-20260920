//! Column-value bitmap indexes.
//!
//! * [`ValueIndex`] maps each distinct non-NULL value of one column to the
//!   bitmap of rows holding it, plus a separate NULL bitmap. Range predicates
//!   are served by unioning the bitmaps of matching keys, so every predicate
//!   — equality or range — is answered with bitmap algebra, never by
//!   short-circuiting row scans inside the engine.
//! * [`CompositeIndex`] maps tuples of several columns to row bitmaps and can
//!   serve conjunctions of equalities over a *prefix* of its key columns.
//!
//! Indexes are built over the physical row space `[0, row_count)`. Deletions
//! are applied at query time by intersecting with the versioned universe, so
//! one index build serves every read version.

use std::collections::HashMap;

use crate::batch::ColumnType;
use crate::bits::WordBitmap;
use crate::error::{ErrorKind, IndexError, Result};
use crate::scalar::{CompareOp, Scalar};

/// Bitmap index over one column.
#[derive(Debug, Clone)]
pub struct ValueIndex {
    column: String,
    ty: ColumnType,
    len: usize,
    /// Rows where the column IS NULL.
    nulls: WordBitmap,
    /// Distinct non-NULL value -> rows holding it.
    values: HashMap<Scalar, WordBitmap>,
}

impl ValueIndex {
    pub fn build(column: &str, ty: ColumnType, len: usize, cells: &[Option<Scalar>]) -> Result<Self> {
        if cells.len() != len {
            return Err(IndexError::new(
                ErrorKind::InvalidBitmap,
                format!("ValueIndex::build({column}): {} cells for {len} rows", cells.len()),
            ));
        }
        let mut nulls = WordBitmap::zeros(len);
        let mut values: HashMap<Scalar, WordBitmap> = HashMap::new();
        for (row, cell) in cells.iter().enumerate() {
            match cell {
                None => nulls.set(row)?,
                Some(v) => {
                    if !ty.accepts(v) {
                        return Err(IndexError::new(
                            ErrorKind::TypeMismatch,
                            format!(
                                "ValueIndex::build({column}): row {row} value {v:?} is {}, column is {}",
                                v.type_name(),
                                ty.as_str()
                            ),
                        ));
                    }
                    values.entry(v.clone()).or_insert_with(|| WordBitmap::zeros(len)).set(row)?;
                }
            }
        }
        let idx = Self { column: column.to_string(), ty, len, nulls, values };
        idx.validate()?;
        Ok(idx)
    }

    pub fn column(&self) -> &str {
        &self.column
    }

    pub fn column_type(&self) -> ColumnType {
        self.ty
    }

    pub fn nulls(&self) -> &WordBitmap {
        &self.nulls
    }

    /// Rows holding any non-NULL value.
    pub fn present(&self) -> Result<WordBitmap> {
        WordBitmap::ones(self.len).and_not(&self.nulls)
    }

    /// Every distinct value bitmap must be disjoint from every other and from
    /// the NULL set; together they must cover the universe exactly.
    pub fn validate(&self) -> Result<()> {
        self.nulls.validate()?;
        let mut covered = self.nulls.clone();
        for (v, bm) in &self.values {
            bm.validate()?;
            if !bm.is_disjoint(&covered)? {
                return Err(IndexError::new(
                    ErrorKind::InvalidBitmap,
                    format!("ValueIndex({}): value {v:?} overlaps another value or NULL set", self.column),
                ));
            }
            covered = covered.union(bm)?;
        }
        if covered != WordBitmap::ones(self.len) {
            return Err(IndexError::new(
                ErrorKind::InvalidBitmap,
                format!("ValueIndex({}): value bitmaps + NULLs do not cover the universe", self.column),
            ));
        }
        Ok(())
    }

    /// Evaluate `column <op> literal` as (true_rows, false_rows) over the
    /// physical row space. UNKNOWN rows are exactly `nulls`.
    pub fn compare(&self, op: CompareOp, literal: &Scalar) -> Result<(WordBitmap, WordBitmap)> {
        if !self.ty.accepts(literal) {
            return Err(IndexError::new(
                ErrorKind::TypeMismatch,
                format!(
                    "cannot compare column {:?} ({}) with literal {literal:?} ({})",
                    self.column,
                    self.ty.as_str(),
                    literal.type_name()
                ),
            ));
        }
        let mut t = WordBitmap::zeros(self.len);
        for (v, bm) in &self.values {
            if op.compare(v, literal)? {
                t = t.union(bm)?;
            }
        }
        // FALSE = present rows that did not match. Derived from the present
        // set, never from a machine complement, so NULL rows stay UNKNOWN.
        let f = self.present()?.and_not(&t)?;
        Ok((t, f))
    }
}

/// Bitmap index over an ordered tuple of columns, serving equality
/// conjunctions on a prefix of the key.
#[derive(Debug, Clone)]
pub struct CompositeIndex {
    columns: Vec<String>,
    types: Vec<ColumnType>,
    len: usize,
    /// Per key column: rows where that column IS NULL.
    nulls: Vec<WordBitmap>,
    /// Fully non-NULL key tuple -> rows.
    entries: HashMap<Vec<Scalar>, WordBitmap>,
}

impl CompositeIndex {
    pub fn build(
        columns: &[String],
        types: &[ColumnType],
        len: usize,
        column_cells: &[Vec<Option<Scalar>>],
    ) -> Result<Self> {
        if columns.is_empty() {
            return Err(IndexError::new(ErrorKind::InvalidPredicate, "composite index needs at least one column"));
        }
        if columns.len() != column_cells.len() || columns.len() != types.len() {
            return Err(IndexError::new(
                ErrorKind::InvalidBitmap,
                "CompositeIndex::build: columns/types/cells length mismatch",
            ));
        }
        let mut nulls = Vec::with_capacity(columns.len());
        let mut entries: HashMap<Vec<Scalar>, WordBitmap> = HashMap::new();
        for (ty, cells) in types.iter().zip(column_cells) {
            if cells.len() != len {
                return Err(IndexError::new(
                    ErrorKind::InvalidBitmap,
                    format!("CompositeIndex::build: {} cells for {len} rows", cells.len()),
                ));
            }
            let mut col_nulls = WordBitmap::zeros(len);
            for (row, cell) in cells.iter().enumerate() {
                match cell {
                    None => col_nulls.set(row)?,
                    Some(v) if !ty.accepts(v) => {
                        return Err(IndexError::new(
                            ErrorKind::TypeMismatch,
                            format!("CompositeIndex:: value {v:?} does not match column type {}", ty.as_str()),
                        ));
                    }
                    Some(_) => {}
                }
            }
            nulls.push(col_nulls);
        }
        for row in 0..len {
            let mut key = Vec::with_capacity(columns.len());
            let mut has_null = false;
            for cells in column_cells {
                match &cells[row] {
                    Some(v) => key.push(v.clone()),
                    None => {
                        has_null = true;
                        break;
                    }
                }
            }
            // Rows with any NULL key component are not in `entries`; they are
            // UNKNOWN for any equality conjunction on this key.
            if !has_null {
                entries.entry(key).or_insert_with(|| WordBitmap::zeros(len)).set(row)?;
            }
        }
        Ok(Self { columns: columns.to_vec(), types: types.to_vec(), len, nulls, entries })
    }

    pub fn columns(&self) -> &[String] {
        &self.columns
    }

    /// Can this index serve equality conjunctions on exactly `cols` (order-free)?
    /// The required columns must form a prefix of the key.
    pub fn serves(&self, cols: &[&str]) -> bool {
        cols.len() <= self.columns.len()
            && cols.iter().enumerate().all(|(i, c)| self.columns.get(i).map(|k| k == c).unwrap_or(false))
    }

    /// Evaluate `key[0]=v[0] AND ... AND key[k-1]=v[k-1]` as (true, false, unknown)
    /// bitmaps over the physical row space.
    pub fn lookup(&self, values: &[Scalar]) -> Result<(WordBitmap, WordBitmap, WordBitmap)> {
        if values.is_empty() || values.len() > self.columns.len() {
            return Err(IndexError::new(
                ErrorKind::InvalidPredicate,
                format!(
                    "composite lookup needs 1..={} values, got {}",
                    self.columns.len(),
                    values.len()
                ),
            ));
        }
        for (i, v) in values.iter().enumerate() {
            if !self.types[i].accepts(v) {
                return Err(IndexError::new(
                    ErrorKind::TypeMismatch,
                    format!(
                        "composite key column {:?} is {}, literal {v:?} is {}",
                        self.columns[i],
                        self.types[i].as_str(),
                        v.type_name()
                    ),
                ));
            }
        }
        let mut t = WordBitmap::zeros(self.len);
        for (key, bm) in &self.entries {
            if key[..values.len()] == values[..] {
                t = t.union(bm)?;
            }
        }
        // UNKNOWN = rows with NULL in any of the constrained key columns.
        let mut u = WordBitmap::zeros(self.len);
        for n in &self.nulls[..values.len()] {
            u = u.union(n)?;
        }
        // FALSE = fully specified rows that do not match.
        let specified = WordBitmap::ones(self.len).and_not(&u)?;
        let f = specified.and_not(&t)?;
        Ok((t, f, u))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::batch::ColumnType::*;

    fn cells(v: &[Option<i64>]) -> Vec<Option<Scalar>> {
        v.iter().map(|x| x.map(Scalar::Int)).collect()
    }

    #[test]
    fn value_index_eq_and_range() {
        // 65 rows: non-word universe; value 7 at rows 0 and 64 (tail row).
        let mut raw: Vec<Option<i64>> = vec![None; 65];
        raw[0] = Some(7);
        raw[64] = Some(7);
        raw[1] = Some(8);
        let idx = ValueIndex::build("c", Int, 65, &cells(&raw)).unwrap();
        idx.validate().unwrap();

        let (t, f) = idx.compare(CompareOp::Eq, &Scalar::Int(7)).unwrap();
        assert_eq!(t.set_indices(), vec![0, 64]);
        assert_eq!(f.set_indices(), vec![1]);
        assert_eq!(idx.nulls().count(), 62, "65 rows minus 3 present values");

        let (t, f) = idx.compare(CompareOp::Gt, &Scalar::Int(7)).unwrap();
        assert_eq!(t.set_indices(), vec![1]);
        assert_eq!(f.set_indices(), vec![0, 64]);

        // NULL literal type mismatch is a typed error, not an empty result.
        let err = idx.compare(CompareOp::Eq, &Scalar::Text("7".into())).unwrap_err();
        assert_eq!(err.kind, ErrorKind::TypeMismatch);
    }

    #[test]
    fn composite_prefix_lookup() {
        let a = cells(&[Some(1), Some(1), None, Some(2)]);
        let b = vec![
            Some(Scalar::Text("x".into())),
            Some(Scalar::Text("y".into())),
            Some(Scalar::Text("x".into())),
            Some(Scalar::Text("x".into())),
        ];
        let idx = CompositeIndex::build(
            &["a".to_string(), "b".to_string()],
            &[Int, Text],
            4,
            &[a, b],
        )
        .unwrap();
        assert!(idx.serves(&["a"]));
        assert!(idx.serves(&["a", "b"]));
        assert!(!idx.serves(&["b"]), "non-prefix column cannot be served");

        let (t, f, u) = idx.lookup(&[Scalar::Int(1)]).unwrap();
        assert_eq!(t.set_indices(), vec![0, 1]);
        assert_eq!(f.set_indices(), vec![3]);
        assert_eq!(u.set_indices(), vec![2], "NULL key component -> UNKNOWN");

        let (t, f, u) = idx.lookup(&[Scalar::Int(1), Scalar::Text("x".into())]).unwrap();
        assert_eq!(t.set_indices(), vec![0]);
        assert_eq!(f.set_indices(), vec![1, 3]);
        assert_eq!(u.set_indices(), vec![2]);
    }
}
