// A top-level function (resolved repo-wide — Swift's flat module namespace needs no import).
func validate(name: String) -> Bool {
    return name.isEmpty == false
}

class AuthService {
    // `u: User` is an explicitly declared receiver type, so `u.isActive()` resolves.
    func authenticate(u: User) -> Bool {
        return validate(name: "admin") && u.isActive()
    }

    // `User(name:)` is an initializer call (Swift has no `new`).
    func make() -> User {
        return User(name: "admin")
    }
}
