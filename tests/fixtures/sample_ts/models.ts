export class Base {
  greet(): string {
    return "hi";
  }
}

export class User extends Base {
  private name: string;

  constructor(name: string) {
    super();
    this.name = name;
  }

  who(): string {
    return this.name;
  }
}
