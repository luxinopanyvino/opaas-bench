"""Dataset cache: downloaded once, reused always.

Flow per known source:

    ~/.opaas/cache/sources/<source>/<file>   raw download, verified
    ~/.opaas/cache/datasets/<source>/        converted to our own format

The second run does not touch the network. For air-gapped use, populate the
cache on a connected machine (`opaas fetch`) and copy the directory;
`--offline` forbids the network explicitly and fails saying what is missing
instead of trying.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx

from opaas.bench.datasets.sources import Source, get_source

_CHUNK = 1024 * 64
_TIMEOUT = 120.0


class CacheError(RuntimeError):
    """The source could not be obtained."""


def cache_dir() -> Path:
    if override := os.environ.get("OPAAS_CACHE_DIR"):
        return Path(override)
    return Path.home() / ".opaas" / "cache"


def source_path(source: Source) -> Path:
    return cache_dir() / "sources" / source.name / source.filename


def dataset_path(source: Source) -> Path:
    return cache_dir() / "datasets" / source.name


def is_cached(name: str) -> bool:
    source = get_source(name)
    return bool(source) and _manifest_is_current(dataset_path(source) / "dataset.json")


def source_is_downloaded(name: str) -> bool:
    """Whether the raw artifact is in the cache and still hashes to what it should.

    This is not `is_cached`. A source used inside a composition is downloaded
    and converted into the composition's directory, never into its own, so
    `is_cached` says False about a file that is on disk, verified and being
    evaluated right now. Reporting that as "not downloaded" misstates where the
    data came from, which is the one thing this tool cannot afford to do.
    """
    source = get_source(name)
    if source is None:
        return False
    path = source_path(source)
    # The hash is what makes the answer worth anything: a corrupt file is not
    # a downloaded one. Every file of the source counts, not only the main one.
    if not (path.is_file() and _sha256(path) == source.sha256):
        return False
    return all(
        (path.parent / extra.filename).is_file()
        and _sha256(path.parent / extra.filename) == extra.sha256
        for extra in source.extra_files
    )


def _manifest_is_current(manifest_path: Path) -> bool:
    """A cached manifest written by an older importer is not reused.

    Provenance fields — how many items the source had, how many were excluded
    and why — are added at import time. Serving a stale manifest would leave
    them out of the report with nothing saying so.
    """
    from opaas.bench.datasets.importers import MANIFEST_VERSION

    if not manifest_path.is_file():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError):
        return False
    return int(manifest.get("manifestVersion", 1)) >= MANIFEST_VERSION


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_source(source: Source, *, offline: bool = False, force: bool = False) -> Path:
    """Downloads the source's files if needed, verifies each hash, returns the main one."""
    destination = source_path(source)
    for extra in source.extra_files:
        _fetch_file(
            source.name, extra.url, destination.parent / extra.filename, extra.sha256,
            offline=offline, force=force,
        )
    return _fetch_file(
        source.name, source.url, destination, source.sha256, offline=offline, force=force
    )


def _fetch_file(
    name: str, url: str, destination: Path, sha256: str, *, offline: bool, force: bool
) -> Path:
    """One file of a source: reuse it if it verifies, otherwise download and verify it."""
    source = _FileRef(name=name, url=url, sha256=sha256)

    if destination.is_file() and not force:
        actual = _sha256(destination)
        if actual == source.sha256:
            return destination
        # A corrupt or tampered cache is not used silently.
        destination.unlink()

    if offline:
        raise CacheError(
            f"'{source.name}' is not in the cache and offline mode was requested.\n"
            f"  Populate the cache on a connected machine: opaas fetch {source.name}\n"
            f"  and copy {cache_dir()} to this machine."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".partial")
    try:
        with httpx.stream(
            "GET", source.url, follow_redirects=True, timeout=_TIMEOUT
        ) as response:
            response.raise_for_status()
            with temporary.open("wb") as handle:
                for chunk in response.iter_bytes(_CHUNK):
                    handle.write(chunk)
    except httpx.HTTPError as exc:
        temporary.unlink(missing_ok=True)
        raise CacheError(
            f"Could not download {source.name} from {source.url}: {exc}\n"
            f"  If you have no internet access, use --offline and copy the cache "
            f"from a connected machine."
        ) from None

    actual = _sha256(temporary)
    if actual != source.sha256:
        temporary.unlink(missing_ok=True)
        raise CacheError(
            f"The file downloaded from {source.name} is NOT the recorded one.\n"
            f"  expected : sha256:{source.sha256}\n"
            f"  obtained : sha256:{actual}\n"
            "It may be a new version of the dataset, or something worse. The run stops: "
            "a certificate claiming to have evaluated with this dataset has to refer to "
            "the verified artifact. Review the change and update the hash in "
            "datasets/sources.py if it is legitimate."
        )

    temporary.replace(destination)
    return destination


@dataclass(frozen=True, slots=True)
class _FileRef:
    """What _fetch_file needs to name, fetch and verify one file."""

    name: str
    url: str
    sha256: str


def ensure_dataset(
    name: str, *, offline: bool = False, force: bool = False, limit: int | None = None
) -> Path:
    """Returns the converted dataset directory, downloading it if needed."""
    source = get_source(name)
    if source is None:
        raise CacheError(f"Unknown source: {name!r}")

    destination = dataset_path(source)
    if _manifest_is_current(destination / "dataset.json") and not force:
        return destination

    raw = fetch_source(source, offline=offline, force=force)

    # Late import: importers imports the loader, and the loader imports this.
    from opaas.bench.datasets.importers import import_dataset

    if destination.exists():
        shutil.rmtree(destination)
    import_dataset(
        source.importer,
        raw,
        destination,
        limit=limit,
        license=source.license,
        source_url=source.url,
        # The version identifies the source artifact, not the download date: two
        # people downloading the same thing get the same version.
        version=source.sha256[:12],
        dataset_name=source.name,
    )
    return destination


def composite_path(names: list[str]) -> Path:
    # A local path can be long or contain odd characters: it is identified by a
    # short hash so the directory name stays manageable.
    parts = [
        name if get_source(name) else "local-" + hashlib.sha256(name.encode()).hexdigest()[:8]
        for name in sorted(names)
    ]
    return cache_dir() / "datasets" / "+".join(parts)


def _planned_tasks(name: str) -> list[str]:
    """Which tasks each part of the composition will contribute."""
    if source := get_source(name):
        from opaas.bench.datasets.importers import IMPORTERS

        return [str(IMPORTERS[source.importer].TASK["name"])]
    manifest = json.loads((Path(name) / "dataset.json").read_text(encoding="utf-8-sig"))
    return [str(task.get("name", task["file"])) for task in manifest.get("tasks", [])]


def _assert_no_task_clashes(names: list[str]) -> None:
    seen: dict[str, str] = {}
    for name in sorted(names):
        for task in _planned_tasks(name):
            if task in seen:
                raise CacheError(
                    f"Task {task!r} from {name} clashes with another from {seen[task]}. "
                    "Rename it in its dataset.json: composing would discard one of the two."
                )
            seen[task] = name


def _merge_local(reference: str, destination: Path) -> str:
    """Folds an already-written local dataset into the composition.

    This is what allows measuring hallucination over **your own documents**
    alongside the public benchmarks, which is the differentiator the design
    asked for: an on-prem deployment cares more about whether the model makes
    things up about its internal documentation than about a Wikipedia passage.
    """
    root = Path(reference)
    manifest_path = root / "dataset.json"
    if not manifest_path.is_file():
        raise CacheError(
            f"{reference} is neither a known source nor a directory with dataset.json"
        )

    local = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    target = destination / "dataset.json"
    if target.is_file():
        merged = json.loads(target.read_text(encoding="utf-8"))
    else:
        merged = {
            "name": "",
            "version": "composite",
            "description": "",
            "license": "",
            "source": "",
            "syntheticSmoke": False,
            "tasks": [],
        }

    existing = {task["name"] for task in merged["tasks"]}
    for task in local.get("tasks", []):
        name = str(task.get("name", task["file"]))
        if name in existing:
            raise CacheError(
                f"Task {name!r} from {reference} clashes with another already composed. "
                "Rename it in its dataset.json."
            )
        shutil.copyfile(root / task["file"], destination / task["file"])
        merged["tasks"].append({**task, "name": name})

    merged["license"] = _join_text(merged.get("license"), local.get("license", "not declared"))
    merged["source"] = _join_text(merged.get("source"), local.get("source", str(root)))
    # If any part is a smoke set, the whole cannot be presented as a measurement.
    merged["syntheticSmoke"] = bool(merged.get("syntheticSmoke")) or bool(
        local.get("syntheticSmoke")
    )
    target.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(local.get("name", root.name))


def _join_text(existing: str | None, addition: str) -> str:
    current = str(existing or "").strip()
    if not current:
        return addition
    return current if addition in current else f"{current} · {addition}"


def ensure_composite(
    names: list[str], *, offline: bool = False, force: bool = False, limit: int | None = None
) -> Path:
    """Composes several sources into a single dataset.

    A certificate is issued from one run, one report and one credential. If
    block B and block D needed different datasets, two runs and two certificates
    would be required for the same deployment, which is exactly the opposite of
    what is wanted.
    """
    unknown = [
        name
        for name in names
        if get_source(name) is None and not (Path(name) / "dataset.json").is_file()
    ]
    if unknown:
        raise CacheError(
            f"Neither known sources nor directories with dataset.json: {', '.join(unknown)}"
        )

    destination = composite_path(names)
    if _manifest_is_current(destination / "dataset.json") and not force:
        return destination

    # Before writing anything: on re-import, a task with the same name replaces
    # the previous one. In a composition that would silently discard somebody's
    # data, so the clash is detected first.
    _assert_no_task_clashes(names)

    from opaas.bench.datasets.importers import MANIFEST_VERSION, import_dataset

    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)

    labels: list[str] = []
    for name in sorted(names):
        source = get_source(name)
        if source is None:
            labels.append(_merge_local(name, destination))
            continue
        raw = fetch_source(source, offline=offline, force=force)
        import_dataset(
            source.importer,
            raw,
            destination,
            limit=limit,
            license=source.license,
            source_url=source.url,
            version=source.sha256[:12],
            dataset_name=source.name,
            append=(destination / "dataset.json").is_file(),
        )
        labels.append(source.name)

    # The name and version describe the composition, not the last importer that
    # came through here.
    manifest_path = destination / "dataset.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["name"] = "+".join(labels)
    manifest["version"] = "+".join(
        get_source(n).sha256[:8] if get_source(n) else "local" for n in sorted(names)
    )
    # A composition of local datasets only never goes through import_dataset:
    # it stamps the version here so the cache is not rebuilt on every run.
    manifest["manifestVersion"] = MANIFEST_VERSION
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return destination


def describe_cache() -> list[dict[str, object]]:
    """Cache status, for `opaas datasets`.

    `status` is the honest one of the two: 'dataset' when the source has its own
    converted directory, 'source' when only the raw artifact is cached — which
    is what a composition leaves behind — and 'missing' when the network is
    still needed. `cached` is kept as it was: converted, standalone.
    """
    from opaas.bench.datasets.sources import KNOWN_SOURCES

    entries: list[dict[str, object]] = []
    for source in KNOWN_SOURCES.values():
        converted = is_cached(source.name)
        if converted:
            status = "dataset"
        elif source_is_downloaded(source.name):
            status = "source"
        else:
            status = "missing"
        entries.append(
            {
                "name": source.name,
                "block": source.block,
                "license": source.license,
                "description": source.description,
                "cached": converted,
                "status": status,
                "path": str(dataset_path(source)),
                "source_path": str(source_path(source)),
            }
        )
    return entries


def describe_cached_datasets() -> list[dict[str, object]]:
    """Datasets already built in the cache, compositions included.

    A run over several sources evaluates the composition, not the parts, and it
    is the composition's hash that ends up in the report. A listing that only
    knows about individual sources leaves the evaluated dataset off the screen.
    """
    from opaas.bench.datasets.loader import DatasetError, load_dataset

    root = cache_dir() / "datasets"
    if not root.is_dir():
        return []

    entries: list[dict[str, object]] = []
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        if not _manifest_is_current(directory / "dataset.json"):
            continue
        try:
            dataset = load_dataset(str(directory))
        except (DatasetError, OSError):
            # A half-written or hand-edited directory is skipped: a listing is
            # not the place to fail over it, and `run` will fail loudly anyway.
            continue
        entries.append(
            {
                "name": dataset.name,
                "version": dataset.version,
                "items": dataset.item_count,
                "tasks": [(task.category, len(task.items)) for task in dataset.tasks],
                "hash": dataset.content_hash,
                "synthetic_smoke": dataset.synthetic_smoke,
                "composite": "+" in directory.name,
                "path": str(directory),
            }
        )
    return entries
