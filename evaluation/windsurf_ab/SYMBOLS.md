# Symbol inventory — taskpack/usersvc

The known top-level symbols at the start of every trial (the gate only considers top-level
functions/classes). A *reuse* task is a miss if a second symbol with the same signature appears;
a *novel* task adds a signature **not** in this list.

| symbol | kind | signature | file |
|---|---|---|---|
| `User` | class | `User` | `usersvc/models.py` |
| `validate` | function | `validate(record)` | `usersvc/auth.py` |
| `authenticate` | function | `authenticate(name)` | `usersvc/auth.py` |
| `make_user` | function | `make_user(name)` | `usersvc/auth.py` |

`User.__init__`, `User.greet` are methods (not top-level) and are not gated.

Novel-control targets that must stay absent for a valid run: `compute_tax(amount, rate, region)`,
`Ledger`.
