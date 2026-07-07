#include "user.h"
#include <stdlib.h>

struct User *create_user(const char *name) {
    struct User *u = malloc(sizeof(struct User));
    if (u) {
        u->name = (char *)name;
        u->age = 0;
    }
    return u;
}
