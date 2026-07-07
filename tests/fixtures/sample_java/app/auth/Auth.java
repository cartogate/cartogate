package app.auth;

import app.models.User;
import java.util.List;

public class Auth {
    public static boolean authenticate(String name) {
        return validate(name);
    }

    private static boolean validate(String name) {
        return name.length() > 0;
    }

    public static User makeUser(String name) {
        return new User(name);
    }
}
