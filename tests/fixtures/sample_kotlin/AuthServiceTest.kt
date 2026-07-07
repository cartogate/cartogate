package app.test

import app.service.AuthService
import app.models.User

// A *Test.kt file -> is_test_unit, so it powers suggest_tests for the symbols it exercises.
class AuthServiceTest {
    fun testAuthenticate() {
        val service: AuthService = AuthService()
        val user: User = User("admin")
        service.authenticate(user)
    }
}
