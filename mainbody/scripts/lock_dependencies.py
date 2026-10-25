"""Snapshot installed dependency trees for both Linux and Windows from a clean venv.

Run AFTER deliberately updating requirements/*.in and installing them.
This is a version snapshot, not a package hash or container image digest lock.
"""
from importlib.metadata import distribution
from pathlib import Path
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

root = Path(__file__).resolve().parents[1]


def collect(seeds):
    result, visited = {}, set()
    pending = [Requirement(value) for value in seeds]
    while pending:
        req = pending.pop()
        key = (canonicalize_name(req.name), tuple(sorted(req.extras)))
        if key in visited:
            continue
        visited.add(key)
        dist = distribution(req.name)
        result[dist.metadata["Name"]] = dist.version
        for line in dist.requires or []:
            child = Requirement(line)
            environments = [dict(default_environment(), sys_platform=platform, os_name=os_name,
                                 platform_system=system, extra=extra)
                            for platform, os_name, system in (("win32", "nt", "Windows"), ("linux", "posix", "Linux"))
                            for extra in (req.extras or {""})]
            if child.marker is None or any(child.marker.evaluate(env) for env in environments):
                pending.append(child)
    return result


base_seeds = [line.strip() for line in (root / "requirements/base.in").read_text().splitlines()
              if line.strip() and not line.startswith("#")]
dev_seeds = [line.strip() for line in (root / "requirements/dev.in").read_text().splitlines()
             if line.strip() and not line.startswith(("#", "-r"))]
base = collect(base_seeds)
dev = collect(base_seeds + dev_seeds)
header = "# Resolved using Python 3.12; update inputs then run scripts/lock_dependencies.py.\n"
for name, packages in (("base", base), ("dev", dev)):
    lines = [f"{key}=={value}" for key, value in sorted(packages.items(), key=lambda item: item[0].lower())]
    (root / f"requirements/{name}.txt").write_text(header + "\n".join(lines) + "\n", encoding="utf-8")
print("Wrote requirements/base.txt and requirements/dev.txt")
