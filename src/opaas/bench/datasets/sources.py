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
class SourceFile:
    """A further file a source needs, pinned exactly like its main one."""

    url: str
    filename: str
    sha256: str


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
    #: Files downloaded next to the main one, each verified against its own
    #: hash. BBQ ships one file per category plus the metadata that marks the
    #: stereotyped answer; all of them are part of the same artifact.
    extra_files: tuple[SourceFile, ...] = ()


#: BBQ is read at a fixed commit, never a branch: a branch can move, a commit cannot.
_BBQ_RAW = "https://raw.githubusercontent.com/nyu-mll/BBQ/bea11bd97d79217245b5871acd247b9d6eb24598"

#: RealToxicityPrompts at a fixed dataset commit: `main` on the Hub can move.
_RTP_REVISION = "f21629712ffd6a3d13a54fd2807ccd521c55ef74"

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
    "squad2": Source(
        name="squad2",
        url="https://rajpurkar.github.io/SQuAD-explorer/dataset/dev-v2.0.json",
        filename="dev-v2.0.json",
        importer="squad2",
        license="CC-BY-SA-4.0 (Rajpurkar, Jia and Liang, ACL 2018)",
        sha256="80a5225e94905956a6446d296ca1093975c4d3b3260f1d6c8f68bc2ab77182d8",
        description="Passages with answerable and unanswerable questions (200 of 11,873)",
        block="D",
    ),
    "bbq": Source(
        name="bbq",
        url=f"{_BBQ_RAW}/analysis_scripts/additional_metadata.csv",
        filename="additional_metadata.csv",
        importer="bbq",
        license="CC-BY-4.0 (Parrish et al., Findings of ACL 2022)",
        sha256="f36708416b0e7adb81b47ad1926f9c39c2beb611702b73b01e06c9b6c9ffbd3d",
        description="Social-bias questions, 9 categories (1,800 of 31,356)",
        block="D",
        extra_files=(
            SourceFile(
                url=f"{_BBQ_RAW}/data/Age.jsonl",
                filename="Age.jsonl",
                sha256="46e805b3fc2d8cbd26eeb8e8430d98cf7b2dc9c83574ff3674e8ce4f0fca2a60",
            ),
            SourceFile(
                url=f"{_BBQ_RAW}/data/Disability_status.jsonl",
                filename="Disability_status.jsonl",
                sha256="375d40d9b71f056150264445ec189b13934998f3f49a1e311aa7578eb3b45e5f",
            ),
            SourceFile(
                url=f"{_BBQ_RAW}/data/Gender_identity.jsonl",
                filename="Gender_identity.jsonl",
                sha256="8b5adbb368510a97d5775dc43e1c719e0249fe2da33f13c83e3e29edafec7a00",
            ),
            SourceFile(
                url=f"{_BBQ_RAW}/data/Nationality.jsonl",
                filename="Nationality.jsonl",
                sha256="a583e74666aec0341ded8aa3983e7cad694eb24de72c254d28b2df3035720090",
            ),
            SourceFile(
                url=f"{_BBQ_RAW}/data/Physical_appearance.jsonl",
                filename="Physical_appearance.jsonl",
                sha256="e48d7e13508565f4801574e611043470c25c95a0a62af3699cb5ee7cb02608fb",
            ),
            SourceFile(
                url=f"{_BBQ_RAW}/data/Race_ethnicity.jsonl",
                filename="Race_ethnicity.jsonl",
                sha256="4a9f1214cfaa115ce7f0bdb40609122e431f5152c71b8ca64d665e483b946ae6",
            ),
            SourceFile(
                url=f"{_BBQ_RAW}/data/Religion.jsonl",
                filename="Religion.jsonl",
                sha256="cb9555f9f3454a52cd2df85956b59bd7fcca5aa922c8527693b1164d08417616",
            ),
            SourceFile(
                url=f"{_BBQ_RAW}/data/SES.jsonl",
                filename="SES.jsonl",
                sha256="9f92754bb037b0982604b9112705fb81d60a19d9e759c67e4a85e484a070f528",
            ),
            SourceFile(
                url=f"{_BBQ_RAW}/data/Sexual_orientation.jsonl",
                filename="Sexual_orientation.jsonl",
                sha256="2c71036b9e7584fe589c42aef32c1a42bc01b9a5e9b1b8704342630cdb08cefd",
            ),
        ),
    ),
    "rtp": Source(
        name="rtp",
        url=(
            "https://huggingface.co/datasets/allenai/real-toxicity-prompts/resolve/"
            f"{_RTP_REVISION}/prompts.jsonl"
        ),
        filename="prompts.jsonl",
        importer="rtp",
        license="Apache-2.0 (Gehman et al., Findings of EMNLP 2020)",
        sha256="4b6808878edc985fcc6bcb6a2a7239a0fb05420cb495f1f6ca1450da5e35c163",
        description="Adversarial sentence openings to continue (200 of the 1,199 challenging)",
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
