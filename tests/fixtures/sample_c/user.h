#ifndef USER_H
#define USER_H

struct User {
    char *name;
    int age;
};

struct User *create_user(const char *name);

#endif
