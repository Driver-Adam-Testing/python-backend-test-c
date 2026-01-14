//! Thread-safe blob caching for incremental tree size calculation.
//!
//! This module provides a cache for blob bytes and lines to avoid
//! re-reading blobs that appear in multiple commits.

use git2::Oid;
use std::collections::HashMap;
use std::sync::{Arc, RwLock};

/// Thread-safe blob cache for bytes and lines.
///
/// Uses separate RwLocks for bytes and lines to minimize contention.
pub struct BlobCache {
    bytes: RwLock<HashMap<Oid, u64>>,
    lines: RwLock<HashMap<Oid, u64>>,
}

impl BlobCache {
    /// Create a new empty blob cache.
    pub fn new() -> Self {
        BlobCache {
            bytes: RwLock::new(HashMap::new()),
            lines: RwLock::new(HashMap::new()),
        }
    }

    /// Create a new blob cache with pre-allocated capacity.
    pub fn with_capacity(capacity: usize) -> Self {
        BlobCache {
            bytes: RwLock::new(HashMap::with_capacity(capacity)),
            lines: RwLock::new(HashMap::with_capacity(capacity)),
        }
    }

    /// Get or compute blob bytes.
    ///
    /// Uses read lock for fast path (cache hit), write lock for slow path (cache miss).
    pub fn get_or_compute_bytes<F>(&self, oid: Oid, compute: F) -> u64
    where
        F: FnOnce() -> u64,
    {
        // Fast path: read lock
        {
            let cache = self.bytes.read().unwrap();
            if let Some(&bytes) = cache.get(&oid) {
                return bytes;
            }
        }

        // Slow path: compute and write
        let bytes = compute();
        {
            let mut cache = self.bytes.write().unwrap();
            cache.insert(oid, bytes);
        }
        bytes
    }

    /// Get or compute blob lines.
    ///
    /// Uses read lock for fast path (cache hit), write lock for slow path (cache miss).
    pub fn get_or_compute_lines<F>(&self, oid: Oid, compute: F) -> u64
    where
        F: FnOnce() -> u64,
    {
        // Fast path: read lock
        {
            let cache = self.lines.read().unwrap();
            if let Some(&lines) = cache.get(&oid) {
                return lines;
            }
        }

        // Slow path: compute and write
        let lines = compute();
        {
            let mut cache = self.lines.write().unwrap();
            cache.insert(oid, lines);
        }
        lines
    }

    /// Get cache statistics (bytes_count, lines_count).
    pub fn stats(&self) -> (usize, usize) {
        let bytes_count = self.bytes.read().unwrap().len();
        let lines_count = self.lines.read().unwrap().len();
        (bytes_count, lines_count)
    }
}

impl Default for BlobCache {
    fn default() -> Self {
        Self::new()
    }
}

/// Wrapper for sharing cache across threads.
pub type SharedBlobCache = Arc<BlobCache>;

/// Create a new shared cache.
pub fn new_shared_cache() -> SharedBlobCache {
    Arc::new(BlobCache::new())
}

/// Create a new shared cache with pre-allocated capacity.
pub fn new_shared_cache_with_capacity(capacity: usize) -> SharedBlobCache {
    Arc::new(BlobCache::with_capacity(capacity))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_cache_hit() {
        let cache = BlobCache::new();
        let oid = Oid::from_str("0000000000000000000000000000000000000001").unwrap();

        // First call computes
        let mut computed = false;
        let result = cache.get_or_compute_bytes(oid, || {
            computed = true;
            42
        });
        assert_eq!(result, 42);
        assert!(computed);

        // Second call uses cache
        computed = false;
        let result = cache.get_or_compute_bytes(oid, || {
            computed = true;
            999
        });
        assert_eq!(result, 42); // Returns cached value
        assert!(!computed); // Compute function not called
    }

    #[test]
    fn test_cache_stats() {
        let cache = BlobCache::new();
        let oid1 = Oid::from_str("0000000000000000000000000000000000000001").unwrap();
        let oid2 = Oid::from_str("0000000000000000000000000000000000000002").unwrap();

        cache.get_or_compute_bytes(oid1, || 100);
        cache.get_or_compute_bytes(oid2, || 200);
        cache.get_or_compute_lines(oid1, || 10);

        let (bytes_count, lines_count) = cache.stats();
        assert_eq!(bytes_count, 2);
        assert_eq!(lines_count, 1);
    }

    #[test]
    fn test_shared_cache() {
        let cache = new_shared_cache_with_capacity(100);
        let oid = Oid::from_str("0000000000000000000000000000000000000001").unwrap();

        cache.get_or_compute_bytes(oid, || 42);

        // Clone the Arc and verify same data
        let cache2 = Arc::clone(&cache);
        let result = cache2.get_or_compute_bytes(oid, || 999);
        assert_eq!(result, 42);
    }
}
