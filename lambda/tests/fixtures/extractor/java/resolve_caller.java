// Fixture: the CALLER package for cross-module resolution tests (ADR-055).
// Imports com.outcomeops.defs.Widget and references it across the file boundary,
// plus one unresolvable call that must be left raw (never-guess).
package com.outcomeops.caller;

import com.outcomeops.defs.Widget;

public class SubWidget extends Widget {
    public int go() {
        Widget w = new Widget();
        return w.targetMethod();
    }

    public int bad() {
        return undefinedHelper();
    }
}
