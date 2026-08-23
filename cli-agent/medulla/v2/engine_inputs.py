"""Where a pool's inputs come from: a list, a shell command, or a file of JSON.

Split from engine_pool.py under the project's 250-line rule ($MAX_LOC). Kept apart
from the running of them because this is the step that decides HOW MANY there are —
and an empty list is a legitimate answer that routes, not a failure.
"""
from __future__ import annotations

import json
from pathlib import Path

from .engine_scan import _sniff_inputs, _tail
from .errors import E_DEADLINE, E_INPUTS, E_INPUTS_LIMIT, EngineCrash
from .model import INPUTS_HARD_CAP, Node
from .procrun import run as proc_run


class InputsMixin:
    def _materialize_inputs(self, node: Node, step_dir: Path) -> list:
        """Snapshot inputs into steps/NNN-<node>/inputs.json (resume foundation).
        An existing snapshot short-circuits everything: sources are NEVER
        re-executed on resume (contract), and the caps/kind checks already passed."""
        snapshot = step_dir / "inputs.json"
        if snapshot.is_file():
            return json.loads(snapshot.read_text(encoding="utf-8"))
        spec = node.pool.inputs
        if spec.data is not None:
            inputs = list(spec.data)
        else:
            cmd = self._render_or_crash(spec.shell, node, "inputs.shell")
            res = proc_run(cmd, self.workdir, self._clamp(spec.shell_timeout),
                           extra_env=self._base_env(),
                           log_path=step_dir / "inputs-source.txt")
            if res.timed_out:
                rem = self._remaining()
                if rem is not None and rem <= 0:      # the run budget killed it, not its own limit
                    raise EngineCrash(E_DEADLINE,
                                      f"workflow timeout ({self.p.timeout}s) exhausted "
                                      f"while sourcing inputs", node=node.name)
                raise EngineCrash(E_INPUTS, f"inputs source timed out ({spec.shell_timeout}s)",
                                  node=node.name)
            if res.rc != 0:
                raise EngineCrash(
                    E_INPUTS,
                    f"inputs source exited rc={res.rc} (a broken producer is not an "
                    f"empty queue); stderr: {_tail(res.stderr)}",
                    node=node.name)
            inputs = _sniff_inputs(res.stdout, node.name)
        if len(inputs) > INPUTS_HARD_CAP:
            raise EngineCrash(E_INPUTS_LIMIT,
                              f"{len(inputs)} inputs (cap {INPUTS_HARD_CAP}) — "
                              f"this is almost certainly not what you wanted",
                              node=node.name)
        if any(isinstance(v, list) for v in inputs):
            raise EngineCrash(E_INPUTS, "array inputs are forbidden (wrap in an object)",
                              node=node.name)
        kinds = {isinstance(v, dict) for v in inputs}
        if len(kinds) > 1:
            raise EngineCrash(E_INPUTS, "mixed scalar/object inputs from source",
                              node=node.name)
        (step_dir / "inputs.json").write_text(
            json.dumps(inputs, ensure_ascii=False, indent=1), encoding="utf-8")
        return inputs

