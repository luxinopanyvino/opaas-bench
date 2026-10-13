"""Known dataset sources: downloaded on demand, with a pinned hash.

The first run should not require preparing anything: `--dataset ifeval` just
works. But downloading at run time carries two risks, and both are closed here:

1. **The content changing under your feet.** Every source carries the SHA-256
   verified when it was registered. If what is downloaded does not match, the
   run fails instead of certifying against something else. A certificate that
   says "evaluated with IFEval" has to refer to a concrete artifact.
2. **It ceasing to work without a network.** What is downloaded is kept in a
   persistent cache and later runs do not touch the network. For an air-gapped
   deployment, populate the cache on a connected machine and copy the
   directory.

The licenses belong to their authors and are recorded here so they end up in
the dataset manifest and, from there, on the certificate page.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Source:
    name: str
    url: str
    filename: str
    importer: str
    license: str
    #: SHA-256 verified when the source was registered. If what is downloaded
    #: does not match, the run stops: it may be a new version, or something worse.
    sha256: str
    description: str
    block: str


KNOWN_SOURCES: dict[str, Source] = {
    "ifeval": Source(
        name="ifeval",
        url="https://huggingface.co/datasets/google/IFEval/resolve/main/ifeval_input_data.jsonl",
        filename="ifeval_input_data.jsonl",
        importer="ifeval",
        license="Apache-2.0 (Google Research)",
        sha256="6a85310ca8ce15eff755aa08a3a4ff931c7e273e7515ebb3c492ea85fd8288f2",
        description="541 prompts with programmatically checkable instructions",
        block="B",
    ),
    "xstest": Source(
        name="xstest",
        url="https://raw.githubusercontent.com/paul-rottger/xstest/main/xstest_prompts.csv",
        filename="xstest_prompts.csv",
        importer="xstest",
        license="CC-BY-4.0 (Röttger et al., NAACL 2024)",
        sha256="11783fb294ed017473ee53c207d71f2161c7672c8d0b037501e78387f801cb5a",
        description="250 benign prompts that look dangerous + 200 harmful ones",
        block="D",
    ),
}


#: Default dataset: real, not smoke. IFEval covers block B and XSTest block D,
#: so an unconfigured run measures something real instead of producing a report
#: that declares itself useless.
DEFAULT_DATASET = "ifeval,xstest"


def available_sources() -> list[str]:
    return sorted(KNOWN_SOURCES)


def get_source(name: str) -> Source | None:
    return KNOWN_SOURCES.get(name)
