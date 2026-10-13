from datetime import datetime, timezone

from opaas.core.hashing import (
    allocate_run_id,
    canonical_json,
    config_hash,
    file_digest,
    short_hash,
    slugify,
    text_file_digest,
    weights_hash,
)


def test_canonical_json_is_order_independent():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_config_hash_ignores_volatile_fields():
    base = {"model": "llama", "api": "completions", "temperature": 0.0}
    assert config_hash(base) == config_hash(
        {**base, "base_url": "http://otro-host:8000/v1", "api_key": "secreto", "timeout_s": 60}
    )


def test_config_hash_changes_with_semantics():
    assert config_hash({"api": "chat"}) != config_hash({"api": "completions"})


def test_short_hash_strips_algorithm_prefix():
    assert short_hash("sha256:abcdef0123456789") == "abcdef01"


def test_slugify():
    assert slugify("meta-llama/Llama-3.3-70B-Instruct") == "meta-llama-llama-3.3-70b-instruct"
    assert slugify("///") == "unknown"


def test_weights_hash_without_path_is_unavailable():
    result = weights_hash(None)
    assert result["available"] is False
    assert "not reachable" in result["note"]


def test_weights_hash_over_directory(tmp_path):
    (tmp_path / "model-00001.safetensors").write_bytes(b"fake weights")
    (tmp_path / "README.md").write_text("not a weights file")
    result = weights_hash(tmp_path)
    assert result["available"] is True
    assert result["fileCount"] == 1
    assert result["hash"].startswith("sha256:")


def test_weights_hash_is_stable(tmp_path):
    (tmp_path / "model.gguf").write_bytes(b"abc")
    assert weights_hash(tmp_path)["hash"] == weights_hash(tmp_path)["hash"]


def test_file_digest_is_byte_exact(tmp_path):
    """The report is signed by bytes: any difference counts."""
    unix = tmp_path / "a.json"
    unix.write_bytes(b'{"a":1}\n')
    windows = tmp_path / "b.json"
    windows.write_bytes(b'{"a":1}\r\n')
    assert file_digest(unix) != file_digest(windows)


def test_text_file_digest_ignores_line_endings(tmp_path):
    """Datasets are hashed by content: CRLF and LF are the same dataset."""
    unix = tmp_path / "a.jsonl"
    unix.write_bytes(b'{"a":1}\n{"b":2}\n')
    windows = tmp_path / "b.jsonl"
    windows.write_bytes(b'{"a":1}\r\n{"b":2}\r\n')
    assert text_file_digest(unix) == text_file_digest(windows)


def test_text_file_digest_still_detects_content_changes(tmp_path):
    original = tmp_path / "a.jsonl"
    original.write_text('{"a":1}\n', encoding="utf-8")
    before = text_file_digest(original)
    original.write_text('{"a":2}\n', encoding="utf-8")
    assert text_file_digest(original) != before


def test_allocate_run_id_increments(tmp_path):
    now = datetime(2026, 8, 6, tzinfo=timezone.utc)
    first = allocate_run_id(tmp_path, now)
    assert first == "20260806-001"
    (tmp_path / first).mkdir()
    assert allocate_run_id(tmp_path, now) == "20260806-002"
