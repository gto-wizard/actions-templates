"""Guard every composite action against expressions the runner will not accept.

A composite action is validated when it is *loaded*, so an unsupported function does not
degrade one step -- GitHub refuses the whole file and every job using it dies at "Set up
job", before a single line runs. That is silent until something calls the action: this was
shipped, merged, and only surfaced when a caller re-pinned to it days later, at which point
all four opencode reviewers failed with

    Unrecognized function: 'cancelled'. Located at position 1 within expression: cancelled()

Nothing in a unit suite catches that, because the defect is in YAML the tests never read.
So the YAML is read here.
"""

import re
import unittest
from pathlib import Path

ACTIONS_DIR = Path(__file__).resolve().parent.parent

# `always()`, `success()` and `failure()` are accepted in composite steps and are in active
# use. `cancelled()` is not -- verified on a runner, not inferred from documentation, which
# does not state the restriction.
UNSUPPORTED = ("cancelled",)

EXPRESSION = re.compile(r"\$\{\{[^}]*\}\}")


def composite_actions() -> list[Path]:
    return [
        path
        for path in sorted(ACTIONS_DIR.glob("*/action.yaml"))
        if re.search(r"^\s*using:\s*[\"']?composite", path.read_text(encoding="utf-8"), re.M)
    ]


class CompositeExpressionTest(unittest.TestCase):
    def test_there_are_composite_actions_to_check(self) -> None:
        # A glob that silently matches nothing would make every assertion below vacuous.
        self.assertTrue(composite_actions())

    def test_no_composite_action_calls_an_unsupported_function(self) -> None:
        for path in composite_actions():
            text = path.read_text(encoding="utf-8")
            for expression in EXPRESSION.findall(text):
                for name in UNSUPPORTED:
                    with self.subTest(action=path.parent.name, expression=expression.strip()):
                        self.assertNotRegex(
                            expression,
                            rf"\b{name}\s*\(",
                            f"{name}() is rejected inside a composite action; GitHub refuses "
                            f"the whole of {path.parent.name}/action.yaml and every caller "
                            f"fails at 'Set up job'",
                        )


if __name__ == "__main__":
    unittest.main()
