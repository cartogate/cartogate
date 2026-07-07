//! Domain types: a `User` struct, a `Greeter` trait, and `User`'s associated/trait methods.

/// Anything that can greet. `User` implements it (an `inherits` edge to this trait).
pub trait Greeter {
    fn greet(&self) -> String;
}

/// A registered user.
pub struct User {
    pub name: String,
}

impl User {
    /// Associated constructor — the target of `User::new` calls across the crate.
    pub fn new(name: String) -> User {
        User { name }
    }
}

impl Greeter for User {
    fn greet(&self) -> String {
        format!("hello {}", self.name)
    }
}
