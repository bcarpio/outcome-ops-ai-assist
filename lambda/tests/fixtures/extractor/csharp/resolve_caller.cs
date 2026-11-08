// Fixture: the CALLER namespace for cross-module resolution tests (ADR-055).
// Uses OutcomeOps.Defs and references its symbols across the file boundary,
// plus one unresolvable call that must be left raw (never-guess).
using OutcomeOps.Defs;

namespace OutcomeOps.Caller;

public class SubWidget : Widget
{
    public int Go()
    {
        var w = new Widget();
        return w.TargetMethod();
    }

    public int Bad()
    {
        return UndefinedHelper();
    }
}
