"""Engine crash classes (class A). Workflow failures (class B) are signals, not exceptions."""

E_VALIDATION = "E_VALIDATION"
E_RENDER = "E_RENDER"
E_DEADLINE = "E_DEADLINE"
E_INPUTS = "E_INPUTS"
E_INPUTS_LIMIT = "E_INPUTS_LIMIT"
E_HARNESS = "E_HARNESS"
E_INTERNAL = "E_INTERNAL"


class EngineCrash(Exception):
    """Class A: the pipeline itself is broken. Not routable in-graph; exit 1."""

    def __init__(self, code: str, message: str, node: str | None = None):
        self.code = code
        self.message = message
        self.node = node
        super().__init__(f"{code}: {message}")
