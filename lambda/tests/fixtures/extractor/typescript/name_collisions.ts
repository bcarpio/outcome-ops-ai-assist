function helper(): string {
  return 'module';
}

class A {
  helper(): string {
    return 'A';
  }
}

class B {
  helper(): string {
    return 'B';
  }
}

export function caller(): void {
  helper();
}
