// A `.test.js` file → classified as a test unit (powers `suggest_tests`).

import { authenticate } from "./auth";

test("authenticate returns a user", () => {
  const user = authenticate("alice");
  expect(user).not.toBeNull();
});
