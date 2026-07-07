using Sample.Services;

namespace Sample.Tests
{
    // A *Tests.cs file -> is_test_unit, so this powers suggest_tests for the symbols it exercises.
    public class AuthServiceTests
    {
        public void TestAuthenticate()
        {
            // Explicit receiver type so `service.Authenticate()` resolves (declared, not inferred).
            AuthService service = new AuthService(null);
            service.Authenticate();
        }
    }
}
