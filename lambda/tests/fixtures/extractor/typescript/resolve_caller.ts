// Fixture: the CALLER module for cross-module resolution tests (ADR-055).
// Imports from ./resolve_defs and references its symbols across the file
// boundary, plus one unresolvable call that must be left raw (never-guess).
import { targetFn, Widget } from "./resolve_defs";

export function caller(): number {
  return targetFn(5);
}

export class SubWidget extends Widget {
  go(): number {
    return undefinedHelper();
  }
}
