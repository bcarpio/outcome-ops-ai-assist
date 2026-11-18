// Fixture: top-level type-only declarations must emit `defines` facts so
// type-only imports (`import type { Conversation }`) resolve to a def node.

export interface Conversation {
  id: string;
  title: string;
}

interface InternalShape {
  x: number;
}

export type Message = {
  role: 'user' | 'assistant';
  content: string;
};

type AliasOnly = number | string;

export enum Color {
  Red,
  Green,
  Blue,
}

enum InternalEnum {
  A,
  B,
}
