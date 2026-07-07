#include "auth.h"
#include "user.h"

/* A file-local (static) helper: resolves only within this translation unit. */
static int validate(const char *name) {
    return name != 0;
}

int authenticate(const char *name) {
    if (validate(name)) {
        struct User *u = create_user(name);  /* cross-file call via the global index */
        return u != 0;
    }
    return 0;
}
