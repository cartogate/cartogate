// A *Tests.swift file -> is_test_unit, so it powers suggest_tests for the symbols it exercises.
class AuthServiceTests {
    func testAuthenticate() {
        let service: AuthService = AuthService()
        let user: User = User(name: "admin")
        _ = service.authenticate(u: user)
    }
}
