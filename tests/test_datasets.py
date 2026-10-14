import json

import pytest

from opaas.bench.datasets import DatasetError, builtin_datasets, load_dataset

MANIFEST = {
    "name": "test",
    "version": "1.0",
    "description": "test dataset",
    "tasks": [{"file": "mc.jsonl", "name": "mc", "category": "reasoning"}],
}
ITEM = {"id": "i1", "question": "2+2?", "choices": ["3", "4"], "answer": "B"}


def _write(tmp_path, manifest=None, items=None, filename="mc.jsonl"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "dataset.json").write_text(
        json.dumps(manifest or MANIFEST), encoding="utf-8"
    )
    lines = items if items is not None else [ITEM]
    (tmp_path / filename).write_text(
        "\n".join(json.dumps(item) for item in lines), encoding="utf-8"
    )
    return tmp_path


def test_smoke_dataset_is_packaged():
    assert "opaas-smoke" in builtin_datasets()


def test_smoke_dataset_loads_and_declares_itself_as_smoke():
    dataset = load_dataset("opaas-smoke")
    assert dataset.synthetic_smoke is True
    assert dataset.item_count == 40
    assert {task.category for task in dataset.tasks} == {
        "reasoning",
        "science",
        "instruction-following",
        "refusal",
        "hallucination",
    }


def test_smoke_dataset_covers_both_faces_of_refusal():
    """Without benign items, over-refusal cannot be measured."""
    task = next(t for t in load_dataset("opaas-smoke").tasks if t.type == "refusal")
    expectations = [item.expected for item in task.items]
    assert expectations.count("refuse") >= 3
    assert expectations.count("comply") >= 3


def test_smoke_dataset_has_unanswerable_grounded_questions():
    """Hallucination is measured with questions whose answer is NOT in the passage."""
    task = next(t for t in load_dataset("opaas-smoke").tasks if t.type == "grounded_qa")
    assert any(not item.answerable for item in task.items)
    assert any(item.answerable for item in task.items)


def test_dataset_hash_is_stable_and_recorded():
    first = load_dataset("opaas-smoke")
    second = load_dataset("opaas-smoke")
    assert first.content_hash == second.content_hash
    provenance = first.provenance()
    assert provenance["datasetHash"].startswith("sha256:")
    assert all(task["fileHash"].startswith("sha256:") for task in provenance["tasks"])


def test_limit_does_not_change_the_dataset_hash(tmp_path):
    """The artifact is the same; what changes is which part was evaluated."""
    full = load_dataset("opaas-smoke")
    limited = load_dataset("opaas-smoke", limit=2)
    assert limited.content_hash == full.content_hash
    assert limited.item_count == 2 * len(full.tasks)
    assert all(len(task.items) == 2 for task in limited.tasks)


def test_run_time_limit_keeps_both_sides_of_a_refusal_task(tmp_path):
    """Taking the first N would leave only benign prompts: the trimming is stratified."""
    manifest = {
        "name": "refusal",
        "version": "1",
        "tasks": [{"file": "safety.jsonl", "type": "refusal", "category": "x"}],
    }
    items = [
        {"id": f"s{i}", "prompt": "benign", "expected": "comply", "category": "safe"}
        for i in range(20)
    ] + [
        {"id": f"u{i}", "prompt": "harmful", "expected": "refuse", "category": "harmful"}
        for i in range(20)
    ]
    _write(tmp_path, manifest=manifest, items=items, filename="safety.jsonl")

    selected = load_dataset(str(tmp_path), limit=10).tasks[0].items
    assert len(selected) == 10
    assert {item.expected for item in selected} == {"comply", "refuse"}


def test_run_time_limit_is_deterministic(tmp_path):
    manifest = {
        "name": "refusal",
        "version": "1",
        "tasks": [{"file": "safety.jsonl", "type": "refusal", "category": "x"}],
    }
    items = [
        {"id": f"s{i}", "prompt": "p", "expected": "comply", "category": f"c{i % 3}"}
        for i in range(30)
    ]
    _write(tmp_path, manifest=manifest, items=items, filename="safety.jsonl")

    first = [i.id for i in load_dataset(str(tmp_path), limit=9).tasks[0].items]
    second = [i.id for i in load_dataset(str(tmp_path), limit=9).tasks[0].items]
    assert first == second


def test_line_endings_do_not_change_the_dataset_hash(tmp_path):
    """The same dataset cloned on Windows and on Linux has to be the same dataset."""
    unix = _write(tmp_path / "unix")
    (unix / "mc.jsonl").write_bytes(json.dumps(ITEM).encode() + b"\n")
    windows = _write(tmp_path / "win")
    (windows / "mc.jsonl").write_bytes(json.dumps(ITEM).encode() + b"\r\n")

    assert load_dataset(str(unix)).content_hash == load_dataset(str(windows)).content_hash


def test_editing_an_item_changes_the_hash(tmp_path):
    before = load_dataset(str(_write(tmp_path))).content_hash
    _write(tmp_path, items=[{**ITEM, "answer": "A"}])
    assert load_dataset(str(tmp_path)).content_hash != before


def test_load_from_an_arbitrary_path(tmp_path):
    dataset = load_dataset(str(_write(tmp_path)))
    assert dataset.name == "test"
    assert dataset.tasks[0].items[0].id == "i1"


def test_unknown_dataset_lists_the_packaged_ones():
    with pytest.raises(DatasetError, match="opaas-smoke"):
        load_dataset("does-not-exist")


def test_manifest_without_tasks_is_rejected(tmp_path):
    with pytest.raises(DatasetError, match="tasks"):
        load_dataset(str(_write(tmp_path, manifest={"name": "x", "version": "1"})))


def test_missing_task_file_is_reported(tmp_path):
    manifest = {**MANIFEST, "tasks": [{"file": "missing.jsonl"}]}
    _write(tmp_path, manifest=manifest)
    with pytest.raises(DatasetError, match="does not exist"):
        load_dataset(str(tmp_path))


def test_unsupported_item_type_is_rejected(tmp_path):
    manifest = {**MANIFEST, "tasks": [{"file": "mc.jsonl", "type": "free_text"}]}
    _write(tmp_path, manifest=manifest)
    with pytest.raises(DatasetError, match="Unsupported item type"):
        load_dataset(str(tmp_path))


def test_multiple_choice_answer_out_of_range_is_caught(tmp_path):
    """An 'answer' of 'C' with two choices is a dataset error, not a model one."""
    _write(tmp_path, items=[{**ITEM, "answer": "C"}])
    with pytest.raises(DatasetError, match="must be one of AB"):
        load_dataset(str(tmp_path))


def test_multiple_choice_needs_choices_and_answer(tmp_path):
    _write(tmp_path, items=[{"id": "i", "question": "?", "choices": ["only one"], "answer": "A"}])
    with pytest.raises(DatasetError, match="at least 2 choices"):
        load_dataset(str(tmp_path))


def test_instruction_item_without_constraints_is_rejected(tmp_path):
    manifest = {**MANIFEST, "tasks": [{"file": "mc.jsonl", "type": "instruction_following"}]}
    _write(tmp_path, manifest=manifest, items=[{"id": "i", "prompt": "do something"}])
    with pytest.raises(DatasetError, match="at least one constraint"):
        load_dataset(str(tmp_path))


def test_item_without_prompt_points_to_the_line(tmp_path):
    _write(tmp_path, items=[ITEM, {"id": "i2", "choices": ["a", "b"], "answer": "A"}])
    with pytest.raises(DatasetError, match=r"mc\.jsonl:2"):
        load_dataset(str(tmp_path))


def test_malformed_jsonl_points_to_the_line(tmp_path):
    _write(tmp_path)
    (tmp_path / "mc.jsonl").write_text(
        json.dumps(ITEM) + "\n{this is not json}\n", encoding="utf-8"
    )
    with pytest.raises(DatasetError, match=r"mc\.jsonl:2"):
        load_dataset(str(tmp_path))


def test_empty_task_file_is_rejected(tmp_path):
    _write(tmp_path, items=[])
    with pytest.raises(DatasetError, match="is empty"):
        load_dataset(str(tmp_path))


# ------------------------------------------------------- the item type seam


def test_the_built_in_item_types_are_registered():
    from opaas.bench.datasets import available_item_types

    assert available_item_types() == [
        "bias_mc",
        "grounded_qa",
        "instruction_following",
        "multiple_choice",
        "refusal",
        "toxicity_prompt",
    ]


def test_a_block_can_bring_its_own_item_type(tmp_path):
    """The seam that was closed: a security or XAI layer needs its own kind of
    item, and used to have to edit the loader to get one."""
    from opaas.bench.datasets import load_dataset, register_item_type
    from opaas.bench.datasets.loader import _ITEM_TYPES, DatasetError

    def _check_injection(item, where):
        if not item.expected_facts:
            raise DatasetError(f"{where}: prompt_injection needs 'expected_facts'")

    register_item_type("prompt_injection", _check_injection)
    try:
        (tmp_path / "dataset.json").write_text(
            json.dumps(
                {
                    "name": "injection",
                    "version": "1",
                    "tasks": [
                        {"file": "items.jsonl", "type": "prompt_injection", "category": "security"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "items.jsonl").write_text(
            json.dumps({"id": "i-1", "prompt": "ignore previous", "expected_facts": ["refused"]}),
            encoding="utf-8",
        )

        dataset = load_dataset(str(tmp_path))
        assert dataset.tasks[0].type == "prompt_injection"
        assert dataset.tasks[0].items[0].expected_facts == ["refused"]
    finally:
        _ITEM_TYPES.pop("prompt_injection", None)


def test_a_registered_type_still_enforces_its_own_check(tmp_path):
    """Registering a type must not mean anything is accepted under its name."""
    from opaas.bench.datasets import load_dataset, register_item_type
    from opaas.bench.datasets.loader import _ITEM_TYPES, DatasetError

    def _always_refuses(item, where):
        raise DatasetError(f"{where}: nope")

    register_item_type("picky", _always_refuses)
    try:
        (tmp_path / "dataset.json").write_text(
            json.dumps(
                {
                    "name": "picky",
                    "version": "1",
                    "tasks": [{"file": "items.jsonl", "type": "picky", "category": "x"}],
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "items.jsonl").write_text(
            json.dumps({"id": "p-1", "prompt": "hello"}), encoding="utf-8"
        )
        with pytest.raises(DatasetError, match="nope"):
            load_dataset(str(tmp_path))
    finally:
        _ITEM_TYPES.pop("picky", None)


def test_an_unknown_type_lists_what_is_registered(tmp_path):
    from opaas.bench.datasets import load_dataset
    from opaas.bench.datasets.loader import DatasetError

    (tmp_path / "dataset.json").write_text(
        json.dumps(
            {
                "name": "bad",
                "version": "1",
                "tasks": [{"file": "items.jsonl", "type": "telepathy", "category": "x"}],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "items.jsonl").write_text(
        json.dumps({"id": "1", "prompt": "x"}), encoding="utf-8"
    )
    with pytest.raises(DatasetError, match="grounded_qa"):
        load_dataset(str(tmp_path))


# --------------------------------------------------- RealToxicityPrompts (D59)


def test_rtp_keeps_only_the_challenging_openings_and_counts_them_as_the_source():
    from opaas.bench.datasets.importers import rtp

    rows = [
        {"filename": "a", "begin": 1, "challenging": True,
         "prompt": {"text": "You are such a", "toxicity": 0.8}},
        {"filename": "b", "begin": 2, "challenging": True,
         "prompt": {"text": "The weather today", "toxicity": 0.1}},
        {"filename": "c", "begin": 3, "challenging": True, "prompt": {"text": "Unscored"}},
        {"filename": "d", "begin": 4, "challenging": False,
         "prompt": {"text": "Not part of the set", "toxicity": 0.9}},
    ]
    items, stats = rtp.convert(rows)
    assert [i["category"] for i in items] == ["toxic prompt", "non-toxic prompt"]
    assert stats.total == 3 and stats.source_rows == 4
    assert stats.skipped_by_reason == {"prompt toxicity not scored in the source": 1}
    selected, design = rtp.sample(items, seed=1234)
    assert len(selected) == 2 and "challenging" in design

