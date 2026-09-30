//! Local, deterministic synthetic fixtures and CSV loading through arrow2.
//!
//! No external services or real business data are used. Fixture sets are pure
//! functions of a seed/scale so tests are reproducible. CSV ingestion goes
//! through arrow2's CSV reader (`arrow2::io::csv::read`) but uses an *explicit*
//! per-column schema (int|text) so null handling and types stay deterministic
//! instead of relying on inference.

use std::collections::BTreeMap;
use std::io::{Cursor, Read};

use arrow2::io::csv::read;

use crate::batch::{Column, ColumnType, NamedColumn, RecordBatch};
use crate::error::{ErrorCode, JoinError, Result};
use crate::types::Datum;

/// A named fixture set: a catalog of relations.
#[derive(Debug, Clone)]
pub struct FixtureSet {
    pub name: String,
    pub relations: BTreeMap<String, RecordBatch>,
}

impl FixtureSet {
    /// Look up a built-in fixture by name.
    pub fn by_name(name: &str) -> Result<FixtureSet> {
        match name {
            "triangle" => Ok(triangle()),
            "skew" => Ok(highly_skewed(1_000)),
            "skew_large" => Ok(highly_skewed(100_000)),
            "sparse" => Ok(sparse_intersection()),
            "all" => Ok(combined_catalog()),
            other => Err(JoinError::new(
                ErrorCode::UnknownRelation,
                format!("unknown fixture `{other}` (triangle|skew|skew_large|sparse|all)"),
            )),
        }
    }
}

/// Build an int column from i64 options.
fn int_col(name: &str, vals: Vec<Option<i64>>) -> NamedColumn {
    NamedColumn {
        name: name.into(),
        column: Column::Int(vals),
    }
}

/// The classic triangle query `R(a,b) ▷◁ S(b,c) ▷◁ T(a,c)` on a handful of
/// edges, including a duplicate edge (multiplicity) and an isolated edge that
/// must not appear. Hand-computed so expected answers are fixed literals.
pub fn triangle() -> FixtureSet {
    let mut relations = BTreeMap::new();
    // R(a,b)
    relations.insert(
        "R".into(),
        RecordBatch::new(vec![
            int_col("a", vec![Some(1), Some(1), Some(2), Some(2), Some(4)]),
            int_col("b", vec![Some(2), Some(3), Some(3), Some(4), Some(5)]),
        ])
        .unwrap(),
    );
    // S(b,c): edge (1,3)->(b=3,c=3) duplicated once => multiplicity 2
    relations.insert(
        "S".into(),
        RecordBatch::new(vec![
            int_col("b", vec![Some(2), Some(3), Some(3), Some(4), Some(9)]),
            int_col("c", vec![Some(3), Some(1), Some(1), Some(1), Some(9)]),
        ])
        .unwrap(),
    );
    // T(a,c)
    relations.insert(
        "T".into(),
        RecordBatch::new(vec![
            int_col("a", vec![Some(1), Some(2), Some(2), Some(7)]),
            int_col("c", vec![Some(3), Some(1), Some(1), Some(7)]),
        ])
        .unwrap(),
    );
    FixtureSet {
        name: "triangle".into(),
        relations,
    }
}

/// Highly skewed, sparse-intersection stress data.
///
/// `R` repeats a single hot key `0` for almost all rows (plus one rare key),
/// while `S` contains the rare key and a long tail that shares almost nothing
/// with R. A naive pairwise plan first builds a huge `R × S` candidate set; the
/// Leapfrog intersection on the join key touches only a handful of seeks.
pub fn highly_skewed(scale: u64) -> FixtureSet {
    // R(k, v): `scale` rows, every key = 0 except the last key = 1.
    let mut rk: Vec<Option<i64>> = vec![Some(0); scale as usize];
    let mut rv: Vec<Option<i64>> = (0..scale as i64).map(Some).collect();
    rk[scale as usize - 1] = Some(1);
    rv[scale as usize - 1] = Some(-1);

    // S(k, w): one row at hot key 0, one at rare key 1, and a disjoint tail
    // of keys that R never has (2..scale).
    let mut sk: Vec<Option<i64>> = vec![Some(0), Some(1)];
    let mut sw: Vec<Option<i64>> = vec![Some(10_000), Some(10_001)];
    for k in 2..scale as i64 {
        sk.push(Some(k));
        sw.push(Some(10_000 + k));
    }

    let mut relations = BTreeMap::new();
    relations.insert(
        "R".into(),
        RecordBatch::new(vec![int_col("k", rk), int_col("v", rv)]).unwrap(),
    );
    relations.insert(
        "S".into(),
        RecordBatch::new(vec![int_col("k", sk), int_col("w", sw)]).unwrap(),
    );
    FixtureSet {
        name: format!("skew{scale}"),
        relations,
    }
}

/// Two relations whose join-key domains are individually large but intersect
/// in exactly one value. Exercises "leap without scanning".
pub fn sparse_intersection() -> FixtureSet {
    // R.k = 0..999 (even bias) ; S.k = 500..1499 ; intersection at 500..999
    // Make the true intersection a single key for a crisp assertion: R.k in
    // 0..999, S.k = {999} ∪ 2000..2999.
    let rk: Vec<Option<i64>> = (0..1000).map(Some).collect();
    let rv: Vec<Option<i64>> = (0..1000).map(|i| Some(i + 5_000)).collect();
    let mut sk: Vec<Option<i64>> = vec![Some(999)];
    let mut sw: Vec<Option<i64>> = vec![Some(-999)];
    for k in 2000..3000 {
        sk.push(Some(k));
        sw.push(Some(k));
    }
    let mut relations = BTreeMap::new();
    relations.insert(
        "R".into(),
        RecordBatch::new(vec![int_col("k", rk), int_col("v", rv)]).unwrap(),
    );
    relations.insert(
        "S".into(),
        RecordBatch::new(vec![int_col("k", sk), int_col("w", sw)]).unwrap(),
    );
    FixtureSet {
        name: "sparse".into(),
        relations,
    }
}

/// Union of the above, used by the running service so all named relations are
/// queryable. Relation names are namespaced to avoid clashes.
pub fn combined_catalog() -> FixtureSet {
    let tri = triangle();
    let mut relations = tri.relations;

    let sk = highly_skewed(1_000).relations;
    for (k, v) in sk {
        relations.insert(format!("SK_{k}"), rename_relation_batch(v));
    }
    let sp = sparse_intersection().relations;
    for (k, v) in sp {
        relations.insert(format!("SP_{k}"), rename_relation_batch(v));
    }
    FixtureSet {
        name: "all".into(),
        relations,
    }
}

fn rename_relation_batch(b: RecordBatch) -> RecordBatch {
    // Column names are intrinsic; this is a passthrough kept for clarity.
    b
}

/// Explicit column schema entry for CSV loading.
#[derive(Debug, Clone)]
pub struct CsvColumn {
    pub name: String,
    pub ty: ColumnType,
}

/// Parse a CSV document into a typed [`RecordBatch`] using arrow2's CSV reader.
///
/// `schema` gives the intended column order/types; the CSV must have a header
/// whose names match. Empty fields and the literal `NULL` (unquoted) decode to
/// null. Types are enforced — a non-integer in an int column is rejected with
/// [`ErrorCode::InvalidCsv`].
///
/// Records are streamed through arrow2's `csv::Reader`; the resulting batch is
/// then converted to an arrow2 [`Chunk`] and back, exercising the canonical
/// Arrow representation and validating column/length consistency.
pub fn load_csv(name: &str, csv: &[u8], schema: &[CsvColumn]) -> Result<(String, RecordBatch)> {
    let mut reader = read::ReaderBuilder::new()
        .has_headers(true)
        .from_reader(Cursor::new(csv.to_vec()));

    let header: Vec<String> = reader
        .headers()
        .map_err(csv_err)?
        .iter()
        .map(|s| s.to_string())
        .collect();
    if header.len() != schema.len()
        || !header.iter().zip(schema.iter()).all(|(h, c)| h == &c.name)
    {
        return Err(JoinError::new(
            ErrorCode::SchemaMismatch,
            format!(
                "CSV header {header:?} does not match declared schema {:?}",
                schema.iter().map(|c| &c.name).collect::<Vec<_>>()
            ),
        ));
    }

    // Strictly parse every byte record (streamed via arrow2's csv reader).
    let mut int_cols: Vec<Vec<Option<i64>>> =
        schema.iter().map(|c| (c.ty == ColumnType::Int).then(Vec::new).unwrap_or_default()).collect();
    let mut txt_cols: Vec<Vec<Option<String>>> =
        schema.iter().map(|c| (c.ty == ColumnType::Text).then(Vec::new).unwrap_or_default()).collect();

    let mut rec = read::ByteRecord::new();
    loop {
        let more = reader.read_byte_record(&mut rec).map_err(csv_err)?;
        if !more {
            break;
        }
        for (ci, col) in schema.iter().enumerate() {
            let bytes = rec.get(ci).ok_or_else(|| {
                JoinError::new(
                    ErrorCode::InvalidCsv,
                    format!("row is short on column `{}`", col.name),
                )
            })?;
            let cell = std::str::from_utf8(bytes)
                .map_err(|_| JoinError::new(ErrorCode::InvalidCsv, "non-utf8 csv cell"))?;
            if cell.is_empty() || cell == "NULL" {
                match col.ty {
                    ColumnType::Int => int_cols[ci].push(None),
                    ColumnType::Text => txt_cols[ci].push(None),
                }
                continue;
            }
            match col.ty {
                ColumnType::Int => {
                    let v: i64 = cell.parse().map_err(|_| {
                        JoinError::new(
                            ErrorCode::InvalidCsv,
                            format!("column `{}` value `{cell}` is not an int", col.name),
                        )
                    })?;
                    int_cols[ci].push(Some(v));
                }
                ColumnType::Text => txt_cols[ci].push(Some(cell.to_string())),
            }
        }
    }

    let columns: Vec<NamedColumn> = schema
        .iter()
        .enumerate()
        .map(|(ci, col)| NamedColumn {
            name: col.name.clone(),
            column: match col.ty {
                ColumnType::Int => Column::Int(std::mem::take(&mut int_cols[ci])),
                ColumnType::Text => Column::Text(std::mem::take(&mut txt_cols[ci])),
            },
        })
        .collect();
    let batch = RecordBatch::new(columns)?;

    // Exercise the Arrow2 representation: typed batch -> (Schema, Chunk) ->
    // typed batch, asserting the round trip is lossless for the loaded data.
    let (arrow_schema, chunk) = batch.to_arrow()?;
    let back = RecordBatch::from_arrow(&arrow_schema, &chunk)?;
    if back != batch {
        return Err(JoinError::new(
            ErrorCode::Internal,
            "arrow2 chunk round-trip was not lossless",
        ));
    }
    debug_assert_eq!(chunk.arrays().len(), schema.len());

    Ok((name.into(), batch))
}

fn csv_err<E: std::fmt::Display>(e: E) -> JoinError {
    JoinError::new(ErrorCode::InvalidCsv, format!("csv parse error: {e}"))
}

/// Helper used by fixtures/tests: rows -> datums.
pub fn ints(v: &[i64]) -> Vec<Datum> {
    v.iter().map(|i| Datum::Int(*i)).collect()
}

/// Read fixture CSVs embedded under `fixtures/` at runtime (best-effort helper).
pub fn load_named_csv_file(path: &str, schema: &[CsvColumn]) -> Result<(String, RecordBatch)> {
    let mut f = std::fs::File::open(path).map_err(|e| {
        JoinError::new(
            ErrorCode::InvalidCsv,
            format!("cannot open {path}: {e}"),
        )
    })?;
    let mut buf = Vec::new();
    f.read_to_end(&mut buf)
        .map_err(|e| JoinError::new(ErrorCode::InvalidCsv, format!("read {path}: {e}")))?;
    let name = std::path::Path::new(path)
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("relation")
        .to_string();
    load_csv(&name, &buf, schema)
}
