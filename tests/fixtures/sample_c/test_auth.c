#include "auth.h"

/* A test_*.c file -> is_test_unit, so it powers suggest_tests for authenticate. */
int test_authenticate(void) {
    return authenticate("admin") == 1;
}
