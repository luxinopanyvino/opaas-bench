"""`swarm`: flows of several models and agents (D51).

Does the whole flow behave as declared, and when it fails, can the failure be
attributed? Topology declared against observed, responsibility as a
subgraph, scripted fault injection through the sentinel proxy, and figures
per edge — never per flow.
"""

from opaas.swarm.analysis import analyse
from opaas.swarm.faults import FaultPlan, FaultPlanError, analyse_faults
from opaas.swarm.topology import (
    TopologyError,
    attribute,
    compare_topology,
    edge_figures,
    load_topology,
)

__all__ = [
    "FaultPlan",
    "FaultPlanError",
    "TopologyError",
    "analyse",
    "analyse_faults",
    "attribute",
    "compare_topology",
    "edge_figures",
    "load_topology",
]
