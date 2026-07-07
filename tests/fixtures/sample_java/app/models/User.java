package app.models;

public class User extends Base implements Greeter {
    private String name;

    public User(String name) {
        this.name = name;
    }

    public String who() {
        return this.name;
    }
}
