using System;            // external namespace -> external package node, no in-repo edge
using Sample.Models;     // in-repo namespace: brings User/Base into scope

namespace Sample.Services
{
    public class AuthService
    {
        private readonly User _user;

        public AuthService(User user)
        {
            _user = user;
        }

        // Calls a same-class method (Validate) and a declared-receiver method (_user.IsActive()).
        public bool Authenticate()
        {
            return Validate() && _user.IsActive();
        }

        private bool Validate()
        {
            return true;
        }

        // `new User(...)` resolves across the namespace via the `using Sample.Models`.
        public static User Make()
        {
            return new User("admin");
        }
    }
}
