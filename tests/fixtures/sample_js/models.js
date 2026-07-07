// ESM module: a Base class and a User subclass (an `inherits` edge).

export class Base {
  constructor(name) {
    this.name = name;
  }
}

export class User extends Base {
  greet() {
    return `hello ${this.name}`;
  }
}
