//! Authentication: imports `User` from a sibling module, calls a same-module helper and
//! the cross-module associated function `User::new`. The `std::fmt` import is external.

use crate::models::User;
use std::fmt;

/// Log in `name`. Calls the same-module `validate` and builds a `User` via `make_user`.
pub fn authenticate(name: String) -> Option<User> {
    if validate(&name) {
        Some(make_user(name))
    } else {
        None
    }
}

fn validate(name: &str) -> bool {
    !name.is_empty()
}

fn make_user(name: String) -> User {
    let _ = fmt::Error; // touch the external import so it is not dead
    User::new(name)
}
