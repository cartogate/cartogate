import { User } from "./models";
import lodash from "lodash";

export function authenticate(name: string, pwd: string): boolean {
  return validate(name);
}

function validate(n: string): boolean {
  return n.length > 0;
}

export const makeUser = (name: string): User => new User(name);
