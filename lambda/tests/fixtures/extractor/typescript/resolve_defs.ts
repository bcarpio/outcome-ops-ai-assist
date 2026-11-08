// Fixture: the DEFINING module for cross-module resolution tests (ADR-055).
export function targetFn(x: number): number {
  return x;
}

export class Widget {
  use(): number {
    return 1;
  }
}
