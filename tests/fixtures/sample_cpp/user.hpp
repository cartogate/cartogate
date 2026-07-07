#pragma once
#include <string>

namespace app {

class Base {
public:
    void init();
};

class User : public Base {
    std::string name_;

public:
    User(const std::string &name);
    bool isActive() const;
};

User *makeUser(const std::string &name);

}  // namespace app
