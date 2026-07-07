#include "user.hpp"

// A test_*.cpp file -> is_test_unit, so it powers suggest_tests for the symbols it exercises.
bool test_isActive() {
    app::User *u = app::makeUser("admin");  // qualified free-function call
    return u->isActive();                   // declared-receiver method call
}
