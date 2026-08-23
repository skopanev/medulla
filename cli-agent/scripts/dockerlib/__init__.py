"""The container wrapper, split by what each part IS.

docker.py stayed one file until it was a thousand lines carrying five unrelated jobs —
secrets, image resolution, mounts, path policy, process lifecycle. The project contract
puts the ceiling at 250 lines per file ($MAX_LOC), so each of those is a module here and
docker.py is the entry point that orchestrates them.
"""
