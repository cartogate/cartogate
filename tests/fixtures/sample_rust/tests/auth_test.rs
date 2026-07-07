//! Lives under `tests/`, so Cartogate classifies it as a test unit (powers `suggest_tests`).

use crate::auth::authenticate;

#[test]
fn test_authenticate() {
    // Call outside the assert! macro so it parses as a real call expression (a `calls` edge),
    // not an opaque macro token tree.
    let result = authenticate("alice".to_string());
    assert!(result.is_some());
}
