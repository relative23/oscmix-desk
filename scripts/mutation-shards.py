#!/usr/bin/env python3
"""Split one mutation run into shards and join their results again.

A mutant is named ``module.function__mutmut_N``. Shard ``k`` of ``n`` runs
the mutants whose number ``N`` ends in a digit ``d`` with ``d % n == k - 1``,
so every function is spread over all shards. A split by module cannot be
balanced: backend.py alone took a third of the summed mutant time of a
0.9.0-era run (214 of 665 minutes), while five shards by number differed
by 5% (122 to 140 minutes each).

    pattern K N         the mutmut name pattern of shard K
    record K N --output DIR
                        after ``mutmut run <pattern>``: check that the shard
                        judged exactly its own mutants and copy its results
    merge DIR...        join recorded shards into ./mutants, refusing a gap,
                        an overlap, another commit or another mutmut

The joined tree is read by ``mutation-policy.py`` exactly like one complete
run, so its rules -- a complete result, no unresolved mutant, the score
floor -- hold for the shards together.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
import subprocess
import sys
from pathlib import Path

MUTANTS = Path("mutants")
SHARD_FILE = "shard.json"
#: Every key of a mutmut 3.7 ``.meta`` file. Another layout is refused rather
#: than half merged.
META_KEYS = {"exit_code_by_key", "hash_by_function_name", "type_check_error_by_key",
             "durations_by_key", "estimated_durations_by_key"}
#: Per-mutant tables, taken from the shard that ran the mutant.
PER_MUTANT = ("exit_code_by_key", "type_check_error_by_key", "durations_by_key",
              "estimated_durations_by_key")


def digits(shard: int, shards: int) -> str:
    if shards not in (1, 2, 5, 10) or not 1 <= shard <= shards:
        raise ValueError("a shard is K of N with N in 1, 2, 5, 10 and 1 <= K <= N")
    return "".join(str(digit) for digit in range(10) if digit % shards == shard - 1)


def pattern(shard: int, shards: int) -> str:
    return "*__mutmut_*[%s]" % digits(shard, shards)


def owner(key: str, shards: int) -> int:
    number = key.rpartition("__mutmut_")[2]
    if "__mutmut_" not in key or not number.isdigit():
        raise ValueError("not a mutant name: " + key)
    return int(number[-1]) % shards + 1


def commit() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


def mutmut_version() -> str:
    return importlib.metadata.version("mutmut")


def load_meta(path: Path) -> dict:
    meta = json.loads(path.read_text())
    if not isinstance(meta, dict) or set(meta) != META_KEYS:
        raise ValueError("unknown mutmut result layout: %s" % path)
    return meta


def metas(root: Path) -> dict[str, dict]:
    """Relative path -> result table, for every ``.meta`` below ``root``."""
    found = {path.relative_to(root).as_posix(): load_meta(path)
             for path in sorted(root.rglob("*.meta"))}
    if not found:
        raise ValueError("no mutation results in %s" % root)
    return found


def record(shard: int, shards: int, output: Path, mutants: Path = MUTANTS) -> dict:
    digits(shard, shards)
    results = metas(mutants)
    own = foreign = unjudged = 0
    for meta in results.values():
        for key, code in meta["exit_code_by_key"].items():
            if owner(key, shards) == shard:
                own += 1
                unjudged += code is None
            elif code is not None:
                foreign += 1
    if unjudged or foreign or not own:
        raise ValueError("shard %d of %d is not exactly its share: %d own mutants, %d unjudged, "
                         "%d judged outside it" % (shard, shards, own, unjudged, foreign))
    output.mkdir(parents=True)
    for name in results:
        target = output / "results" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(mutants / name, target)
    identity = dict(schema=1, shard=shard, shards=shards, commit=commit(),
                    mutmut=mutmut_version(), judged=own)
    (output / SHARD_FILE).write_text(json.dumps(identity, indent=2) + "\n")
    return identity


def _identities(directories: list[Path]) -> tuple[int, list[dict]]:
    identities = [json.loads((directory / SHARD_FILE).read_text()) for directory in directories]
    shards = identities[0].get("shards") if identities else None
    if (not identities or any(item.get("schema") != 1 for item in identities)
            or any(item.get("shards") != shards for item in identities)
            or sorted(item.get("shard") for item in identities) != list(range(1, shards + 1))):
        raise ValueError("the recorded shards are not 1..N of one split, each once")
    here = dict(commit=commit(), mutmut=mutmut_version())
    for item in identities:
        for field, value in here.items():
            if item.get(field) != value:
                raise ValueError("shard %s ran on %s %s, not %s"
                                 % (item["shard"], field, item.get(field), value))
    return shards, identities


def join(shards: int, results: dict[int, dict[str, dict]]) -> tuple[dict[str, dict], int]:
    """One complete result table per file, each mutant from the shard that owns it."""
    names = set(results[1])
    if any(set(tables) != names for tables in results.values()):
        raise ValueError("the shards hold results for different source files")
    joined, judged = {}, 0
    for name in sorted(names):
        tables = {shard: results[shard][name] for shard in results}
        keys = set(tables[1]["exit_code_by_key"])
        if any(set(table["exit_code_by_key"]) != keys for table in tables.values()):
            raise ValueError("the shards generated different mutants for " + name)
        if any(table["hash_by_function_name"] != tables[1]["hash_by_function_name"]
               for table in tables.values()):
            raise ValueError("the shards mutated different sources for " + name)
        merged = {field: {} for field in PER_MUTANT}
        merged["hash_by_function_name"] = tables[1]["hash_by_function_name"]
        for key in sorted(keys):
            own = owner(key, shards)
            if tables[own]["exit_code_by_key"][key] is None:
                raise ValueError("mutant %s was not judged by its shard %d" % (key, own))
            others = [shard for shard, table in tables.items()
                      if shard != own and table["exit_code_by_key"][key] is not None]
            if others:
                raise ValueError("mutant %s was also judged by shard %s" % (key, others))
            for field in PER_MUTANT:
                if key in tables[own][field]:
                    merged[field][key] = tables[own][field][key]
            judged += 1
        joined[name] = merged
    return joined, judged


def merge(directories: list[Path], mutants: Path = MUTANTS) -> dict:
    if mutants.exists():
        raise ValueError("%s exists; the joined result needs a fresh one" % mutants)
    shards, identities = _identities(directories)
    results = {item["shard"]: metas(directory / "results")
               for directory, item in zip(directories, identities, strict=True)}
    joined, judged = join(shards, results)
    if judged != sum(item["judged"] for item in identities):
        raise ValueError("the shards' records do not add up to the joined result")
    for name, meta in joined.items():
        target = mutants / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(meta, indent=4))
    return dict(shards=shards, judged=judged, files=len(joined))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("pattern", "record"):
        command = commands.add_parser(name)
        command.add_argument("shard", type=int)
        command.add_argument("shards", type=int)
        if name == "record":
            command.add_argument("--output", required=True, type=Path)
    joined = commands.add_parser("merge")
    joined.add_argument("directories", nargs="+", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "pattern":
            print(pattern(args.shard, args.shards))
            return 0
        result = (record(args.shard, args.shards, args.output) if args.command == "record"
                  else merge(sorted(args.directories)))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError,
            importlib.metadata.PackageNotFoundError) as exc:
        print("mutation-shards: %s" % exc, file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
