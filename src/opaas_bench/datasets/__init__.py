"""Evaluation datasets: bundled, downloadable with caching, or local."""

from opaas_bench.datasets.cache import (
    CacheError,
    cache_dir,
    describe_cache,
    ensure_composite,
    ensure_dataset,
    fetch_source,
    is_cached,
)
from opaas_bench.datasets.loader import (
    BUILTIN_DIR,
    MANIFEST_FILENAME,
    SUPPORTED_ITEM_TYPES,
    Dataset,
    DatasetError,
    Item,
    Task,
    builtin_datasets,
    load_dataset,
    resolve_dataset_path,
)
from opaas_bench.datasets.sources import (
    DEFAULT_DATASET,
    KNOWN_SOURCES,
    Source,
    available_sources,
    get_source,
)

__all__ = [
    "BUILTIN_DIR",
    "DEFAULT_DATASET",
    "KNOWN_SOURCES",
    "MANIFEST_FILENAME",
    "SUPPORTED_ITEM_TYPES",
    "CacheError",
    "Dataset",
    "DatasetError",
    "Item",
    "Source",
    "Task",
    "available_sources",
    "builtin_datasets",
    "cache_dir",
    "describe_cache",
    "ensure_composite",
    "ensure_dataset",
    "fetch_source",
    "get_source",
    "is_cached",
    "load_dataset",
    "resolve_dataset_path",
]
