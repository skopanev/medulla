"""Opt in to the image's broker box Docker engine.

A private dockerd inside the run's own container, so integration tests run against real
services instead of mocks. Off unless --docker-engine is passed, and off is the path
everything else on this machine takes.

The cost is stated plainly because it cannot be mitigated: the container runs
--privileged and starts as root. Linux capabilities are what make a read-only bind mount
read-only, and root in a privileged container has them all. So this flag does not weaken
one guard — it removes the kind of guard --cwd-ro is.
"""
from __future__ import annotations

import json
import subprocess
import sys

FLAG = "--docker-engine"


def _workspace_is_read_only(volumes: list[str]) -> str | None:
    """The reviewed tree mounted read-only, if it is. Returns the mount spec."""
    for i, value in enumerate(volumes):
        if i == 0 or volumes[i - 1] != "-v":
            continue
        parts = value.split(":")
        if len(parts) >= 3 and parts[1] == "/workspace" and "ro" in parts[2].split(","):
            return value
    return None


def prepare(image: str, volumes: list[str], args: list[str]) -> tuple[list[str], list[str]]:
    if FLAG not in args:
        return volumes, args

    # A PRIVILEGED ROOT CONTAINER IS NOT BOUND BY A READ-ONLY MOUNT, and the two flags
    # used to combine in silence. The first fix here refused the combination. That was
    # the wrong shape, and the lane that uses it said so: it protected the record by
    # removing a capability, and the mount flags were never the thing at risk.
    #
    # What is at risk is the RECORD. A run's mounts.txt reads `ro /workspace` straight
    # from /proc/self/mountinfo, and that line gets cited as evidence that nothing inside
    # could write to the reviewed tree — twice, in written incident answers, by me. It
    # stays true on an ordinary run and stops being true here, while reading identically.
    # A line that means something else than it says is the defect; the capability is not.
    #
    # So the run admits it instead. Nothing is refused, no mount spec changes, and no
    # second flag asks the operator to acknowledge the first — passing --docker-engine IS
    # the acknowledgement. What changes is that the evidence no longer claims enforcement
    # it does not have.
    if (mount := _workspace_is_read_only(volumes)) is not None:
        print(f"[docker.py] {FLAG} with --cwd-ro ({mount}): this container runs "
              f"privileged as root, so ro mount flags are DECLARED, NOT ENFORCED "
              f"against it. The mount spec is unchanged; the run records that its ro "
              f"lines are not a guarantee.", file=sys.stderr)

    # IMAGE METADATA, NOT A PROBE CONTAINER. The uid to drop to is a property of the
    # image and is already recorded in it; starting a container to ask would cost a
    # container per run and could not be done before the run is configured anyway.
    try:
        metadata = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{json .Config}}", image],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
    except subprocess.CalledProcessError as exc:
        # check=True alone raised CalledProcessError out of run_docker and the operator
        # got a traceback. The usual cause is mundane — the image is not built on this
        # host yet — and it deserves a sentence, not a stack.
        detail = (exc.stderr or "").strip().splitlines()
        print(f"[docker.py] {FLAG}: cannot read image metadata for '{image}'"
              f"{': ' + detail[-1] if detail else ''}", file=sys.stderr)
        raise SystemExit(1) from None
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[docker.py] {FLAG}: docker did not answer for '{image}': {exc}",
              file=sys.stderr)
        raise SystemExit(1) from None

    try:
        config = json.loads(metadata)
    except json.JSONDecodeError:
        print(f"[docker.py] {FLAG}: image metadata for '{image}' is not JSON",
              file=sys.stderr)
        raise SystemExit(1) from None

    uid, _, gid = (config.get("User") or "").partition(":")
    home = next((v[5:] for v in config.get("Env") or [] if v.startswith("HOME=")), "")
    if not uid.isdigit() or not gid.isdigit() or int(uid) == 0 or not home.startswith("/"):
        print(f"[docker.py] {FLAG} requires the image to declare a numeric non-root "
              f"USER as uid:gid and an absolute HOME; '{image}' declares "
              f"USER={config.get('User')!r} HOME={home!r}", file=sys.stderr)
        raise SystemExit(1)

    mounts = [*volumes, "--privileged", "--user", "0:0",
              "--mount", "type=volume,target=/var/lib/docker",
              "-e", "BROKER_BOX_DOCKER=1", "-e", f"BROKER_BOX_UID={uid}",
              "-e", f"BROKER_BOX_GID={gid}", "-e", f"HOME={home}"]
    return mounts, [a for a in args if a != FLAG]


def exec_user(volumes: list[str]) -> str | None:
    """Kept exec bypasses the entrypoint; apply its user drop explicitly.

    `docker exec` does not run the image's ENTRYPOINT, so the lazy startup that drops
    root never happens for a kept container — without this the second and later runs in
    a kept session would execute as root while the first ran as the image's user.
    """
    values = dict(v.split("=", 1) for i, v in enumerate(volumes)
                  if i > 0 and volumes[i - 1] == "-e" and "=" in v)
    if values.get("BROKER_BOX_DOCKER") != "1":
        return None
    uid, gid = values.get("BROKER_BOX_UID"), values.get("BROKER_BOX_GID")
    if not uid or not gid:
        # Set together by prepare() or not at all. Returning a half pair would exec as
        # `1000:` — which docker reads as uid with the image's gid, quietly.
        return None
    return f"{uid}:{gid}"
