//! Error handling for the analytics native extension.

use pyo3::exceptions::PyRuntimeError;
use pyo3::PyErr;
use thiserror::Error;

#[derive(Error, Debug)]
pub enum AnalyticsError {
    #[error("Git error: {0}")]
    Git(#[from] git2::Error),

    #[error("Invalid SHA '{sha}': {message}")]
    InvalidSha { sha: String, message: String },

    #[error("Commit not found: {0}")]
    CommitNotFound(String),

    #[error("Tree not found for commit: {0}")]
    TreeNotFound(String),
}

impl From<AnalyticsError> for PyErr {
    fn from(err: AnalyticsError) -> PyErr {
        PyRuntimeError::new_err(err.to_string())
    }
}
