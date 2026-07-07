#include "user.hpp"

namespace app {

// A file-local helper: a free function with internal linkage.
static bool validate(const std::string &n) {
    return !n.empty();
}

User::User(const std::string &name) : name_(name) {}

// Out-of-line method definition: lands under the same `user` module as the class (same stem).
bool User::isActive() const {
    return validate(name_);  // unqualified call -> the file-local free function
}

User *makeUser(const std::string &name) {
    return new User(name);  // `new User` -> the constructor's class
}

}  // namespace app
