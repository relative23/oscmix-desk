"""The nightly mutation run in shards: every mutant judged once, or no score at all."""

import importlib.util
import json
import re

import pytest
from support import repo_file

FILES = {"src/oscmix_desk/a.py.meta": ["oscmix_desk.a.x_f__mutmut_%d" % n for n in range(1, 24)],
         "src/oscmix_desk/b.py.meta": ["oscmix_desk.b.xǁCǁm__mutmut_%d" % n for n in range(1, 8)]}


@pytest.fixture
def shards(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "mutation_shards", repo_file("scripts", "mutation-shards.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "commit", lambda: "c" * 40)
    monkeypatch.setattr(module, "mutmut_version", lambda: "3.7.0")
    return module


def run_shard(module, root, shard, count, judge=None):
    """A fresh tree after ``mutmut run <pattern>``: only this shard's mutants have codes."""
    for name, keys in FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        mine = [key for key in keys if (judge or module.owner)(key, count) == shard]
        path.write_text(json.dumps({
            "exit_code_by_key": {key: (1 if key in mine else None) for key in keys},
            "hash_by_function_name": {"f": "h"},
            "type_check_error_by_key": {},
            "durations_by_key": dict.fromkeys(mine, 1.5),
            "estimated_durations_by_key": dict.fromkeys(keys, 2.0),
        }))
    return root


def recorded(module, tmp_path, count=5, **changes):
    directories = []
    for shard in range(1, count + 1):
        tree = run_shard(module, tmp_path / ("tree%d" % shard), shard, count)
        directories.append(tmp_path / "shards" / ("mutation-shard-%d" % shard))
        module.record(shard, count, directories[-1], mutants=tree)
    return directories


@pytest.mark.parametrize("count", [1, 2, 5, 10])
def test_every_mutant_number_belongs_to_exactly_one_shard(shards, count):
    patterns = [shards.pattern(shard, count) for shard in range(1, count + 1)]
    for number in range(1, 400):
        key = "oscmix_desk.backend.x_send__mutmut_%d" % number
        hits = [shard for shard, glob in enumerate(patterns, 1)
                if re.fullmatch(glob.replace("*", ".*"), key)]
        assert hits == [shards.owner(key, count)]


@pytest.mark.parametrize(("shard", "count"), [(0, 5), (6, 5), (1, 3), (1, 4), (1, 0)])
def test_a_split_that_cannot_be_named_by_digits_is_refused(shards, shard, count):
    with pytest.raises(ValueError, match="a shard is K of N"):
        shards.pattern(shard, count)


def test_the_pattern_is_what_mutmut_receives(shards):
    assert shards.pattern(2, 5) == "*__mutmut_*[16]"
    assert shards.owner("oscmix_desk.a.x_f__mutmut_16", 5) == 2


def test_recorded_shards_join_into_one_complete_run(shards, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = shards.merge(recorded(shards, tmp_path), mutants=tmp_path / "mutants")
    assert result == {"shards": 5, "judged": 30, "files": 2}
    for name, keys in FILES.items():
        meta = json.loads((tmp_path / "mutants" / name).read_text())
        assert meta["exit_code_by_key"] == dict.fromkeys(keys, 1)
        assert set(meta["durations_by_key"]) == set(keys)
        assert set(meta) == shards.META_KEYS


def test_a_shard_that_did_not_finish_is_not_recorded(shards, tmp_path):
    tree = run_shard(shards, tmp_path / "tree", 3, 5)
    meta = tmp_path / "tree" / "src/oscmix_desk/a.py.meta"
    data = json.loads(meta.read_text())
    data["exit_code_by_key"]["oscmix_desk.a.x_f__mutmut_7"] = None
    meta.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="1 unjudged"):
        shards.record(3, 5, tmp_path / "out", mutants=tree)
    assert not (tmp_path / "out").exists()


def test_a_shard_that_ran_another_share_is_not_recorded(shards, tmp_path):
    # Shard 2 ran with shard 3's pattern: it judged nothing of its own.
    tree = run_shard(shards, tmp_path / "tree", 3, 5)
    with pytest.raises(ValueError, match="judged outside it"):
        shards.record(2, 5, tmp_path / "out", mutants=tree)


def test_a_missing_shard_gives_no_score(shards, tmp_path):
    directories = recorded(shards, tmp_path)
    with pytest.raises(ValueError, match=r"not 1\.\.N of one split"):
        shards.merge(directories[:-1], mutants=tmp_path / "mutants")
    assert not (tmp_path / "mutants").exists()


def test_the_same_shard_twice_gives_no_score(shards, tmp_path):
    directories = recorded(shards, tmp_path)
    identity = directories[1] / shards.SHARD_FILE
    data = json.loads(identity.read_text())
    data["shard"] = 1
    identity.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="each once"):
        shards.merge(directories, mutants=tmp_path / "mutants")


def test_a_mutant_judged_by_two_shards_gives_no_score(shards, tmp_path):
    directories = recorded(shards, tmp_path)
    meta = directories[0] / "results" / "src/oscmix_desk/b.py.meta"
    data = json.loads(meta.read_text())
    data["exit_code_by_key"]["oscmix_desk.b.xǁCǁm__mutmut_2"] = 0
    meta.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="also judged by shard"):
        shards.merge(directories, mutants=tmp_path / "mutants")


def test_a_mutant_its_shard_left_open_gives_no_score(shards, tmp_path):
    directories = recorded(shards, tmp_path)
    meta = directories[4] / "results" / "src/oscmix_desk/a.py.meta"
    data = json.loads(meta.read_text())
    data["exit_code_by_key"]["oscmix_desk.a.x_f__mutmut_9"] = None
    meta.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="not judged by its shard 5"):
        shards.merge(directories, mutants=tmp_path / "mutants")


@pytest.mark.parametrize(("field", "value"), [("commit", "d" * 40), ("mutmut", "3.8.0")])
def test_a_shard_from_another_commit_or_mutmut_gives_no_score(shards, tmp_path, field, value):
    directories = recorded(shards, tmp_path)
    identity = directories[2] / shards.SHARD_FILE
    data = json.loads(identity.read_text())
    data[field] = value
    identity.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="shard 3 ran on " + field):
        shards.merge(directories, mutants=tmp_path / "mutants")


def test_shards_that_generated_different_mutants_give_no_score(shards, tmp_path):
    directories = recorded(shards, tmp_path)
    meta = directories[1] / "results" / "src/oscmix_desk/a.py.meta"
    data = json.loads(meta.read_text())
    data["exit_code_by_key"]["oscmix_desk.a.x_f__mutmut_99"] = None
    meta.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="different mutants"):
        shards.merge(directories, mutants=tmp_path / "mutants")


def test_a_result_layout_mutmut_did_not_write_is_refused(shards, tmp_path):
    directories = recorded(shards, tmp_path)
    meta = directories[0] / "results" / "src/oscmix_desk/a.py.meta"
    data = json.loads(meta.read_text())
    data["new_field"] = {}
    meta.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="unknown mutmut result layout"):
        shards.merge(directories, mutants=tmp_path / "mutants")


def test_the_join_never_overwrites_a_local_run(shards, tmp_path):
    directories = recorded(shards, tmp_path)
    (tmp_path / "mutants").mkdir()
    with pytest.raises(ValueError, match="needs a fresh one"):
        shards.merge(directories, mutants=tmp_path / "mutants")


def test_ci_runs_every_shard_the_makefile_splits_into(shards):
    workflow = repo_file(".github", "workflows", "ci.yml").read_text()
    makefile = repo_file("Makefile").read_text()
    count = int(re.search(r"^SHARDS \?= (\d+)$", makefile, re.MULTILINE).group(1))
    matrix = re.search(r"^\s+shard: \[([\d, ]+)\]$", workflow, re.MULTILINE).group(1)
    assert [int(item) for item in matrix.split(",")] == list(range(1, count + 1))
    assert "SHARDS=%d" % count in workflow
    # Every shard runs the same mutmut: the pin lives in one file the jobs install.
    assert "pip install -r requirements-mutation.txt" in workflow
    pins = repo_file("requirements-mutation.txt").read_text()
    assert re.search(r"^mutmut==\S+$", pins, re.MULTILINE)
