// Fixture: the CALLER package for cross-package resolution tests (ADR-055).
// Imports the widget package and calls widget.TargetFn across the package
// boundary, plus one unresolvable bare call that must be left raw (never-guess).
package app

import "example.com/proj/widget"

func Caller() int {
	return widget.TargetFn(5)
}

func Bad() int {
	return undefinedHelper()
}
