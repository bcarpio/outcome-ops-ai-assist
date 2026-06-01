// Fixture: TS function/method/arrow signatures with varied annotations.

export function bare(a, b) { return null; }

export function typed(a: number, b: string): boolean { return true; }

export function defaulted(a: number, b: string = "x"): number | undefined {
    return undefined;
}

export function rest(...args: number[]): number[] { return args; }

export const arrow = (name: string, loud: boolean = false): string => name;

export class Greeter {
    greet(name: string, loud?: boolean): string {
        return name;
    }
}
