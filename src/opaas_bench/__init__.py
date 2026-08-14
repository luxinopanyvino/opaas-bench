"""OPAAS-Bench — On-Prem AI Audit Suite (onpremIALabs)."""

__version__ = "0.1.0"

# Version of the evaluation methodology. It is embedded in every report and in
# the credential (`evalRun.opaasBenchVersion`); it changes only when the
# methodology changes, not on every code release.
OPAAS_BENCH_SPEC_VERSION = "1.0-draft"

__all__ = ["__version__", "OPAAS_BENCH_SPEC_VERSION"]
