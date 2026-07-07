// ESM module: imports User from a sibling module, calls a same-file helper and constructs User.
// The `lodash` import is a bare specifier (external) — it must never produce an in-repo edge.

import { User } from "./models";
import { isEmpty } from "lodash";

export function authenticate(name) {
  if (validate(name)) {
    return new User(name);
  }
  return null;
}

function validate(name) {
  return !isEmpty(name);
}
