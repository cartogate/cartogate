// Node CommonJS module: a `require` destructure binds `authenticate`, then calls it
// (exercises require-path resolution → a `calls` edge into auth.js).

const { authenticate } = require("./auth");

function login(name) {
  return authenticate(name);
}

module.exports = { login };
