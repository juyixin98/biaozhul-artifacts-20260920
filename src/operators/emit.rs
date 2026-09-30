//! Output emitter: column-wise accumulator that builds an arrow2 [`Batch`].
//! Shared by the interpreter and the decorrelating executor so result batches
//! are constructed identically regardless of execution strategy.

use crate::batch::{Batch, ColumnBuilder, ScalarType, Value};
use crate::error::{EngineError, EngineResult};
use crate::semantic::ResolvedOutput;

pub struct Emitter {
    cols: Vec<Vec<Option<Value>>>,
    types: Vec<ScalarType>,
    names: Vec<String>,
}

impl Emitter {
    pub fn new(outputs: &[ResolvedOutput]) -> Self {
        Emitter {
            cols: vec![Vec::new(); outputs.len()],
            types: outputs.iter().map(|o| o.ty).collect(),
            names: outputs.iter().map(|o| o.name.clone()).collect(),
        }
    }

    pub fn push_row(&mut self, row: &[Value]) -> EngineResult<()> {
        if row.len() != self.cols.len() {
            return Err(EngineError::internal(format!(
                "emitter got {} values for {} columns",
                row.len(),
                self.cols.len()
            )));
        }
        for (i, v) in row.iter().enumerate() {
            self.cols[i].push(Some(v.clone()));
        }
        Ok(())
    }

    pub fn into_batch(self) -> EngineResult<Batch> {
        let mut built = Vec::with_capacity(self.cols.len());
        for (i, vals) in self.cols.into_iter().enumerate() {
            let name = &self.names[i];
            let col = match self.types[i] {
                ScalarType::Bool => {
                    let vs: Vec<Option<bool>> = vals
                        .into_iter()
                        .map(|v| match v.unwrap() {
                            Value::Null => None,
                            Value::Bool(b) => Some(b),
                            other => panic!("type drift in emitter: {other:?}"),
                        })
                        .collect();
                    ColumnBuilder::bools(name, &vs)
                }
                ScalarType::Int => {
                    let vs: Vec<Option<i64>> = vals
                        .into_iter()
                        .map(|v| match v.unwrap() {
                            Value::Null => None,
                            Value::Int(x) => Some(x),
                            other => panic!("type drift in emitter: {other:?}"),
                        })
                        .collect();
                    ColumnBuilder::ints(name, &vs)
                }
                ScalarType::Float => {
                    // SUM(int) widens to int in the validated schema; float
                    // outputs accept ints too for robustness.
                    let vs: Vec<Option<f64>> = vals
                        .into_iter()
                        .map(|v| match v.unwrap() {
                            Value::Null => None,
                            Value::Float(x) => Some(x),
                            Value::Int(x) => Some(x as f64),
                            other => panic!("type drift in emitter: {other:?}"),
                        })
                        .collect();
                    ColumnBuilder::floats(name, &vs)
                }
                ScalarType::Str => {
                    let owned: Vec<Option<String>> = vals
                        .into_iter()
                        .map(|v| match v.unwrap() {
                            Value::Null => None,
                            Value::Str(s) => Some(s),
                            other => panic!("type drift in emitter: {other:?}"),
                        })
                        .collect();
                    let refs: Vec<Option<&str>> = owned.iter().map(|o| o.as_deref()).collect();
                    ColumnBuilder::strs(name, &refs)
                }
            };
            built.push(col);
        }
        Batch::new(built)
    }
}
