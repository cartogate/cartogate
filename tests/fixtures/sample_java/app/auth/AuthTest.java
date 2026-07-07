package app.auth;

public class AuthTest {
    public void testAuthenticate() {
        boolean ok = Auth.authenticate("alice");
        assert ok;
    }
}
