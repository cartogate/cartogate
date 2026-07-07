package app.service

import app.models.User

// A top-level function (Kotlin allows file-level functions).
fun validate(name: String): Boolean {
    return name.isNotEmpty()
}

class AuthService {
    // `u: User` is an explicitly declared receiver type, so `u.isActive()` resolves.
    fun authenticate(u: User): Boolean {
        return validate("admin") && u.isActive()
    }

    // `User("admin")` is a constructor call (Kotlin has no `new`).
    fun make(): User {
        return User("admin")
    }
}
