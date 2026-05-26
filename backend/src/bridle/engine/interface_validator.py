"""Interface contract validator — enforce node interface boundary rules.

Each node's consumes must only reference interfaces exposed by adjacent nodes
(direct predecessors via depends_on, or direct successors who depend on this node).
Cross-hop access is forbidden.
"""
from __future__ import annotations


class InterfaceContractValidator:
    """Validates interface contracts across a plan graph.

    The validator receives a list of node dicts with keys:
    - plan_node_id: str
    - depends_on: list[str]
    - interfaces: dict with 'exposes' and 'consumes' lists
    """

    @staticmethod
    def validate(nodes: list[dict]) -> list[str]:
        """Validate all interface contracts in the plan graph.

        Returns a list of error messages. An empty list means all valid.
        Errors are collected exhaustively, not fail-fast.
        """
        errors: list[str] = []
        node_map = {n["plan_node_id"]: n for n in nodes}

        # Build adjacency: for each node, compute its adjacent node IDs
        # Adjacent = direct predecessors (depends_on) + direct successors
        adjacent: dict[str, set[str]] = {}
        for n in nodes:
            nid = n["plan_node_id"]
            adjacent.setdefault(nid, set())
            # Predecessors
            for dep in n.get("depends_on", []):
                adjacent[nid].add(dep)
                # Successors: if m depends on nid, nid is adjacent to m
                adjacent.setdefault(dep, set()).add(nid)

        for n in nodes:
            nid = n["plan_node_id"]
            ifaces = n.get("interfaces", {}) or {}
            consumes = ifaces.get("consumes", []) or []

            for consume in consumes:
                target_id = consume.get("node_id", "")
                interface_name = consume.get("interface_name", "")
                req_fields = consume.get("fields", []) or []
                req_endpoints = consume.get("endpoints", []) or []

                # Check adjacency
                if target_id not in adjacent.get(nid, set()):
                    errors.append(
                        f"Node '{nid}' consumes from '{target_id}' but they are not adjacent "
                        f"(only direct depends_on predecessors and successors are adjacent)"
                    )
                    continue

                # Check target exists
                target_node = node_map.get(target_id)
                if target_node is None:
                    errors.append(f"Node '{nid}' consumes from unknown node '{target_id}'")
                    continue

                target_ifaces = target_node.get("interfaces", {}) or {}
                target_exposes = target_ifaces.get("exposes", []) or []

                # Find the matching exposed interface
                expose = None
                for exp in target_exposes:
                    if exp.get("name") == interface_name:
                        expose = exp
                        break

                if expose is None:
                    errors.append(
                        f"Node '{nid}' consumes interface '{interface_name}' "
                        f"from '{target_id}', but '{target_id}' does not expose it"
                    )
                    continue

                # Check fields subset
                exposed_field_names = {f.get("name") for f in expose.get("fields", []) or []}
                for f in req_fields:
                    if f not in exposed_field_names:
                        errors.append(
                            f"Node '{nid}' consumes field '{f}' from '{target_id}.{interface_name}', "
                            f"but it is not exposed"
                        )

                # Check endpoints subset
                exposed_endpoint_names = {e.get("name") for e in expose.get("endpoints", []) or []}
                for ep in req_endpoints:
                    if ep not in exposed_endpoint_names:
                        errors.append(
                            f"Node '{nid}' consumes endpoint '{ep}' from '{target_id}.{interface_name}', "
                            f"but it is not exposed"
                        )

        return errors
