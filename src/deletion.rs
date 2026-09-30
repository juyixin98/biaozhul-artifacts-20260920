//! Versioned row deletions (tombstones).
//!
//! The table has a fixed row-id space `[0, n)`. A deletion is an
//! append-only event `(row, deleted_at)`; versions are monotonically
//! increasing. The **live universe at version v** is the set of rows with no
//! tombstone at `deleted_at <= v`. Predicates operate on that universe, so a
//! deleted row is excluded from TRUE / FALSE / UNKNOWN alike — it is not
//! "unknown", it no longer belongs to the document set.

use crate::bitset::Bitmap;
use crate::error::Result;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Tombstone {
    pub row: usize,
    pub deleted_at: u64,
}

#[derive(Debug, Clone)]
pub struct DeletionLog {
    n: usize,
    /// Append-only tombstones in call order; `deleted_at` strictly advances.
    entries: Vec<Tombstone>,
    current_version: u64,
}

impl DeletionLog {
    pub fn new(n: usize, initial_version: u64) -> Self {
        Self {
            n,
            entries: Vec::new(),
            current_version: initial_version,
        }
    }

    pub fn from_tombstones(
        n: usize,
        initial_version: u64,
        tombstones: Vec<Tombstone>,
    ) -> Result<Self> {
        let mut log = Self::new(n, initial_version);
        for t in tombstones {
            log.delete(t.row, t.deleted_at)?;
        }
        Ok(log)
    }

    pub fn row_space_len(&self) -> usize {
        self.n
    }

    pub fn current_version(&self) -> u64 {
        self.current_version
    }

    pub fn tombstones(&self) -> &[Tombstone] {
        &self.entries
    }

    /// Record a deletion at a strictly newer version.
    ///
    /// Errors categorically:
    /// * [`TritmapError::VersionOutOfOrder`] when versions do not advance,
    /// * [`TritmapError::RowNotFound`] when the row id is outside the space,
    /// * [`TritmapError::RowAlreadyDeleted`] when re-deleting a dead row.
    pub fn delete(&mut self, row: usize, version: u64) -> Result<()> {
        if row >= self.n {
            return Err(crate::error::TritmapError::RowNotFound {
                table: String::new(),
                row,
                n: self.n,
            });
        }
        if version <= self.current_version {
            return Err(crate::error::TritmapError::VersionOutOfOrder {
                current: self.current_version,
                got: version,
            });
        }
        if let Some(ts) = self.entries.iter().find(|t| t.row == row) {
            return Err(crate::error::TritmapError::RowAlreadyDeleted {
                row,
                deleted_at: ts.deleted_at,
                version,
            });
        }
        self.entries.push(Tombstone {
            row,
            deleted_at: version,
        });
        self.current_version = version;
        Ok(())
    }

    /// Whether `row` is live at version `version`.
    pub fn is_alive_at(&self, row: usize, version: u64) -> bool {
        self.entries
            .iter()
            .all(|t| t.row != row || t.deleted_at > version)
    }

    /// Live-row bitmap at `version`: rows with no tombstone `<= version`.
    pub fn alive_at(&self, version: u64) -> Bitmap {
        let mut alive = Bitmap::all(self.n);
        for t in &self.entries {
            if t.deleted_at <= version {
                alive.unset(t.row);
            }
        }
        alive
    }

    /// Live-row bitmap at the current version.
    pub fn current_alive(&self) -> Bitmap {
        self.alive_at(self.current_version)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::error::TritmapError;

    #[test]
    fn alive_sets_tracking_versions() {
        // n = 65 exercises a two-word universe with tail bits.
        let mut log = DeletionLog::new(65, 1);
        log.delete(0, 2).unwrap();
        log.delete(63, 3).unwrap();
        log.delete(64, 5).unwrap();

        assert_eq!(log.alive_at(1).count_ones(), 65);
        assert_eq!(log.alive_at(2).count_ones(), 64);
        assert_eq!(log.alive_at(3).count_ones(), 63);
        assert_eq!(log.alive_at(4).count_ones(), 63);
        assert_eq!(log.alive_at(5).count_ones(), 62);

        // Tail validity: max set bit is within universe.
        let a = log.alive_at(5);
        a.debug_assert_valid();
        assert!(!a.get(64) && a.get(62));

        assert!(!log.is_alive_at(0, 2));
        assert!(log.is_alive_at(0, 1));
    }

    #[test]
    fn reject_bad_deletions_categorically() {
        let mut log = DeletionLog::new(10, 1);
        assert!(matches!(
            log.delete(10, 2),
            Err(TritmapError::RowNotFound { .. })
        ));
        log.delete(1, 5).unwrap();
        assert!(matches!(
            log.delete(2, 5),
            Err(TritmapError::VersionOutOfOrder { .. })
        ));
        assert!(matches!(
            log.delete(1, 6),
            Err(TritmapError::RowAlreadyDeleted { .. })
        ));
    }
}
