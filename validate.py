# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sam Warren
from dataclasses import dataclass, field

from .core import (ClosureZoneDef, ITEM_NODES, LinkDef, NodeDef,
                   RepeatZoneDef, SocketRef, TreeDef, ZoneDef)
from .schema import NodeTypeDef, NodeVariant, SchemaRegistry

# Node types that exist structurally but are not in the per-tree registry,
# or whose sockets come from elsewhere (interfaces, zone items).
STRUCTURAL_TYPES = {"NodeFrame", "NodeReroute"}
GROUP_TYPES = {"GeometryNodeGroup", "ShaderNodeGroup", "CompositorNodeGroup", "TextureNodeGroup"}
INTERFACE_TYPES = {"NodeGroupInput", "NodeGroupOutput"}


@dataclass
class Issue:
    severity: str  # "error" | "warning"
    tree: str
    node: str
    message: str

    def __str__(self):
        loc = f"{self.tree}/{self.node}" if self.node else self.tree
        return f"{self.severity.upper()} [{loc}]: {self.message}"


# ---------------------------------------------------------------------------
# Shared registry/tree lookups
# ---------------------------------------------------------------------------

def _global_types(registry: SchemaRegistry) -> dict[str, NodeTypeDef]:
    """Node types keyed by bl_idname. A node type may be valid in several
    tree types."""
    out: dict[str, NodeTypeDef] = {}
    for defs in registry.tree_types.values():
        for n in defs:
            out.setdefault(n.bl_idname, n)
    return out


def _effective_props(node_def: NodeDef, type_def: NodeTypeDef) -> dict:
    props = {p.identifier: p.default for p in type_def.properties}
    props.update(node_def.properties)
    return props


def _interface_names(tree: TreeDef, direction: str) -> list[str]:
    return [s.name for s in tree.interface if s.direction == direction]


@dataclass
class _Scope:
    tree: TreeDef
    zone: ZoneDef | None
    nodes: dict[str, NodeDef]
    own_nodes: list[NodeDef]
    links: list[LinkDef]
    pseudo: str | None = None
    pseudo_items: list[str] = field(default_factory=list)
    zone_reads: dict[str, list[str]] = field(default_factory=dict)


def _zone_pseudo(zone: ZoneDef) -> tuple[str, list[str]]:
    if isinstance(zone, RepeatZoneDef):
        return "repeat", [it.name for it in zone.items]
    return "closure", [it.name for it in zone.inputs]


def _tree_scopes(tree: TreeDef) -> list[_Scope]:
    top_nodes = {n.name: n for n in tree.nodes}
    zone_reads = {z.name: [it.name for it in z.items]
                  if isinstance(z, RepeatZoneDef) else ["Closure"]
                  for z in tree.zones}
    scopes = [_Scope(tree, None, top_nodes, tree.nodes, tree.links,
                     zone_reads=zone_reads)]
    for zone in tree.zones:
        nodes = dict(top_nodes)
        nodes.update({n.name: n for n in zone.nodes})
        pseudo, pseudo_items = _zone_pseudo(zone)
        scopes.append(_Scope(tree, zone, nodes, zone.nodes, zone.links,
                             pseudo, pseudo_items, zone_reads))
    return scopes


def _collect_trees(tree_defs: list[TreeDef]) -> dict[str, TreeDef]:
    out: dict[str, TreeDef] = {}
    stack = list(tree_defs)
    while stack:
        td = stack.pop()
        if td.name in out:
            continue
        out[td.name] = td
        stack.extend(td.groups.values())
    return out


def _active_variant(node_def: NodeDef,
                    type_def: NodeTypeDef) -> tuple[NodeVariant | None, bool]:
    if not type_def.variants:
        return None, False
    props = _effective_props(node_def, type_def)
    for variant in type_def.variants:
        if all(props.get(k) == v for k, v in variant.variant_key.items()):
            return variant, True
    if len(type_def.variants) == 1:
        return type_def.variants[0], True
    counts = {(len(v.inputs), len(v.outputs)) for v in type_def.variants}
    if len(counts) == 1:
        return type_def.variants[0], False
    return None, False


# ---------------------------------------------------------------------------
# Name-based socket resolution
# ---------------------------------------------------------------------------

class _NameResolver:
    def __init__(self, all_trees: dict[str, TreeDef]):
        self.all_trees = all_trees

    def _candidate_indices(self, node_def: NodeDef, tree: TreeDef,
                           name: str, side: str) -> tuple[list[int] | None, str]:
        idname = node_def.bl_idname

        if idname == "NodeReroute":
            return [0], ""

        if idname in ITEM_NODES:
            spec = ITEM_NODES[idname]
            offset = spec.input_offset if side == "in" else spec.output_offset
            items = node_def.input_items if side == "in" else node_def.output_items
            return [offset + i for i, it in enumerate(items) if it.name == name], ""

        if idname in GROUP_TYPES:
            ref = self.all_trees.get(node_def.node_tree_name or "")
            if ref is None:
                return None, (f"group tree '{node_def.node_tree_name}' is outside "
                              "this document; use an index")
            names = _interface_names(ref, "INPUT" if side == "in" else "OUTPUT")
        elif idname == "NodeGroupInput":
            names = _interface_names(tree, "INPUT")
        elif idname == "NodeGroupOutput":
            names = _interface_names(tree, "OUTPUT")
        else:
            return None, (f"sockets on {idname} are addressed by index; "
                          "names resolve on group interface sockets only")

        return [i for i, n in enumerate(names) if n == name], ""

    def _resolve_ref(self, ref: SocketRef, scope: _Scope, side: str,
                     issues: list[Issue]) -> bool:
        if side == "out":
            names = None
            if ref.node == scope.pseudo:
                names = scope.pseudo_items
            elif ref.node in scope.zone_reads:
                names = scope.zone_reads[ref.node]
            if names is not None:
                hits = [i for i, n in enumerate(names) if n == ref.name]
                if len(hits) == 1:
                    ref.index = hits[0]
                    return True
                issues.append(Issue("error", scope.tree.name, ref.node,
                                    f"Cannot resolve zone item {ref.name!r}"))
                return False
        node_def = scope.nodes.get(ref.node)
        if node_def is None:
            issues.append(Issue("error", scope.tree.name, ref.node,
                                f"Name ref ({ref.name!r}) on unknown node"))
            return False
        indices, err = self._candidate_indices(node_def, scope.tree, ref.name, side)
        if indices is None or err or len(indices) != 1:
            issues.append(Issue("error", scope.tree.name, ref.node,
                                f"Cannot resolve socket name {ref.name!r}: "
                                f"{err or 'ambiguous'}"))
            return False
        ref.index = indices[0]
        return True

    def _resolve_named_values(self, node_def: NodeDef, scope: _Scope,
                              issues: list[Issue]):
        for sock_name in list(node_def.named_input_values):
            probe = SocketRef(node=node_def.name, index=-1, name=sock_name)
            if self._resolve_ref(probe, scope, "in", issues):
                node_def.input_values[probe.index] = \
                    node_def.named_input_values.pop(sock_name)
                node_def.input_names[probe.index] = sock_name

    def _resolve_source(self, ref: tuple[str, int, str], scope: _Scope,
                        issues: list[Issue]) -> tuple[str, int, str]:
        node_name, index, sock_name = ref
        if index >= 0:
            return ref
        probe = SocketRef(node=node_name, index=index, name=sock_name)
        self._resolve_ref(probe, scope, "out", issues)
        return (node_name, probe.index, sock_name)

    def resolve_tree(self, tree: TreeDef, issues: list[Issue]):
        scopes = _tree_scopes(tree)
        top = scopes[0]

        # A repeat zone header reads from outside the zone: its iteration
        # count and each item's initial value resolve in the top scope.
        for zone in tree.zones:
            if not isinstance(zone, RepeatZoneDef):
                continue
            if isinstance(zone.iterations, tuple):
                zone.iterations = self._resolve_source(
                    zone.iterations, top, issues)
            for item in zone.items:
                if item.initial_connection is not None:
                    item.initial_connection = self._resolve_source(
                        item.initial_connection, top, issues)

        for scope in scopes:
            for link in scope.links:
                if link.source.index < 0:
                    self._resolve_ref(link.source, scope, "out", issues)
                if link.target.index < 0:
                    self._resolve_ref(link.target, scope, "in", issues)
            for node_def in scope.own_nodes:
                self._resolve_named_values(node_def, scope, issues)
            if scope.zone is not None:
                for key, ref in scope.zone.output_mappings.items():
                    scope.zone.output_mappings[key] = \
                        self._resolve_source(ref, scope, issues)


def resolve_names(tree_defs: list[TreeDef], registry: SchemaRegistry) -> list[Issue]:
    all_trees = _collect_trees(tree_defs)
    resolver = _NameResolver(all_trees)
    issues: list[Issue] = []
    for td in all_trees.values():
        resolver.resolve_tree(td, issues)
    return issues


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class _TreeValidator:
    def __init__(self, registry: SchemaRegistry, tree_def: TreeDef,
                 all_trees: dict[str, TreeDef], issues: list[Issue]):
        self.registry = registry
        self.tree = tree_def
        self.all_trees = all_trees
        self.issues = issues
        self.node_types = {n.bl_idname: n for n in registry.tree_types.get(tree_def.bl_idname, [])}
        self.global_types = _global_types(registry)
        if not self.node_types:
            self._warn("", f"No schema for tree type '{tree_def.bl_idname}'; "
                           "node-level checks skipped")

    def _error(self, node: str, msg: str):
        self.issues.append(Issue("error", self.tree.name, node, msg))

    def _warn(self, node: str, msg: str):
        self.issues.append(Issue("warning", self.tree.name, node, msg))

    def _socket_counts(self, node_def: NodeDef) -> tuple[int | None, int | None]:
        if node_def.bl_idname == "NodeReroute":
            return 1, 1
        if node_def.bl_idname == "NodeFrame":
            return 0, 0
        if node_def.bl_idname in ITEM_NODES:
            spec = ITEM_NODES[node_def.bl_idname]
            return (spec.input_offset + len(node_def.input_items),
                    spec.output_offset + len(node_def.output_items))
        if node_def.bl_idname in GROUP_TYPES:
            ref = self.all_trees.get(node_def.node_tree_name or "")
            if ref is None:
                return None, None
            return (len(_interface_names(ref, "INPUT")),
                    len(_interface_names(ref, "OUTPUT")))
        # +1 on the interface side: Blender appends a virtual extension socket.
        if node_def.bl_idname == "NodeGroupInput":
            return 0, len(_interface_names(self.tree, "INPUT")) + 1
        if node_def.bl_idname == "NodeGroupOutput":
            return len(_interface_names(self.tree, "OUTPUT")) + 1, 0
        type_def = self.node_types.get(node_def.bl_idname) \
            or self.global_types.get(node_def.bl_idname)
        if type_def is None:
            return None, None
        variant, _ = _active_variant(node_def, type_def)
        if variant is None:
            return None, None
        return len(variant.inputs), len(variant.outputs)

    def check_node(self, node_def: NodeDef):
        name = node_def.name
        idname = node_def.bl_idname

        if idname in STRUCTURAL_TYPES:
            return

        if idname in ITEM_NODES:
            # Sockets come from the declared items, not the type signature.
            n_in, _ = self._socket_counts(node_def)
            for idx in node_def.input_values:
                if idx >= n_in:
                    self._error(name, f"Input index {idx} out of range on "
                                      f"{idname} (has {n_in} inputs)")
            return

        if idname in GROUP_TYPES:
            if not node_def.node_tree_name:
                self._error(name, "Group node has no node_tree reference")
            elif node_def.node_tree_name not in self.all_trees:
                self._warn(name, f"Group references tree '{node_def.node_tree_name}' "
                                 "outside this document; skipped")
            return

        if idname in INTERFACE_TYPES:
            return

        type_def = self.node_types.get(idname) or self.global_types.get(idname)
        if type_def is None:
            if self.node_types:
                self._error(name, f"Unknown node type '{idname}' for {self.tree.bl_idname}")
            return

        prop_defs = {p.identifier: p for p in type_def.properties}
        for key, val in node_def.properties.items():
            pdef = prop_defs.get(key)
            if pdef is None:
                self._error(name, f"Unknown property '{key}' on {idname}")
                continue
            if pdef.enum_values is not None and val not in pdef.enum_values:
                self._error(name, f"Invalid enum value '{val}' for {idname}.{key}; "
                                  f"valid: {', '.join(pdef.enum_values)}")

        variant, names_reliable = _active_variant(node_def, type_def)
        if variant is None:
            if type_def.variants:
                props = _effective_props(node_def, type_def)
                keys = {k for v in type_def.variants for k in v.variant_key}
                got = {k: props.get(k) for k in keys}
                self._warn(name, f"No socket variant of {idname} matches {got}; "
                                 "socket index checks skipped")
            return

        for idx in node_def.input_values:
            if idx >= len(variant.inputs):
                self._error(name, f"Input index {idx} out of range on {idname} "
                                  f"(has {len(variant.inputs)} inputs)")
            else:
                annotated = node_def.input_names.get(idx, "")
                actual = variant.inputs[idx].name
                if names_reliable and annotated and annotated != actual:
                    self._warn(name, f"Input {idx} is '{actual}', annotated '{annotated}' — "
                                     "likely wrong index")

    def _output_count(self, node_name: str, scope: _Scope) -> int | None:
        if node_name == scope.pseudo:
            return len(scope.pseudo_items)
        if node_name in scope.zone_reads and node_name not in scope.nodes:
            return len(scope.zone_reads[node_name])
        node_def = scope.nodes.get(node_name)
        if node_def is None:
            return -1
        _, n_out = self._socket_counts(node_def)
        return n_out

    def _float_expr_terminals(self, scope: _Scope) -> set[str]:
        out = set()
        for node_def in scope.nodes.values():
            if node_def.bl_idname != "NodeFrame" \
                    or not node_def.name.endswith(".expr"):
                continue
            terminal = scope.nodes.get(node_def.name[:-len(".expr")])
            if terminal and terminal.bl_idname in ("ShaderNodeMath",
                                                   "ShaderNodeValue"):
                out.add(terminal.name)
        return out

    def _input_socket_idname(self, node_def: NodeDef, idx: int) -> str | None:
        if node_def.bl_idname in GROUP_TYPES:
            ref = self.all_trees.get(node_def.node_tree_name or "")
            sockets = [s for s in ref.interface
                       if s.direction == "INPUT"] if ref else []
            return sockets[idx].socket_type if idx < len(sockets) else None
        if node_def.bl_idname == "NodeGroupOutput":
            sockets = [s for s in self.tree.interface
                       if s.direction == "OUTPUT"]
            return sockets[idx].socket_type if idx < len(sockets) else None
        if node_def.bl_idname in ITEM_NODES:
            offset = ITEM_NODES[node_def.bl_idname].input_offset
            items = node_def.input_items
            if 0 <= idx - offset < len(items):
                return items[idx - offset].socket_type
            return None
        type_def = self.node_types.get(node_def.bl_idname) \
            or self.global_types.get(node_def.bl_idname)
        if type_def is None:
            return None
        variant, reliable = _active_variant(node_def, type_def)
        if variant is None or not reliable or idx >= len(variant.inputs):
            return None
        return variant.inputs[idx].bl_idname

    def check_link(self, link: LinkDef, scope: _Scope,
                   float_exprs: set[str] = frozenset()):
        count = self._output_count(link.source.node, scope)
        if count == -1:
            self._error(link.source.node, "Link source references unknown node")
        elif count is not None and link.source.index >= count:
            self._error(link.source.node, f"Link source: output index "
                                          f"{link.source.index} out of range "
                                          f"(has {count})")

        node_def = scope.nodes.get(link.target.node)
        if node_def is None:
            self._error(link.target.node, "Link target references unknown node")
            return
        n_in, _ = self._socket_counts(node_def)
        if n_in is not None and link.target.index >= n_in:
            self._error(link.target.node, f"Link target: input index "
                                          f"{link.target.index} out of range "
                                          f"(has {n_in})")
            return

        if link.source.node in float_exprs and \
                self._input_socket_idname(node_def, link.target.index) \
                == "NodeSocketInt":
            self._warn(link.source.node,
                       f"Float expression feeds integer input "
                       f"{link.target.index} of '{link.target.node}'; "
                       "float32 loses integer exactness — declare expr<int>")

    def check_zone(self, scope: _Scope):
        zone = scope.zone
        if isinstance(zone, RepeatZoneDef):
            items = zone.items
            declared = {it.name for it in items}
        else:
            items = zone.inputs + zone.outputs
            declared = {it.name for it in zone.outputs}

        for item in items:
            if self.registry.socket_types and \
                    item.socket_type not in self.registry.socket_types:
                self._warn(zone.name, f"Zone item '{item.name}': unknown "
                                      f"socket type '{item.socket_type}'")

        for item_name, (src_name, src_idx, _) in zone.output_mappings.items():
            if item_name not in declared:
                self._error(zone.name, f"Output mapping names undeclared "
                                       f"item '{item_name}'")
            count = self._output_count(src_name, scope)
            if count == -1:
                self._error(zone.name, f"Output '{item_name}' references "
                                       f"unknown node '{src_name}'")
            elif count is not None and src_idx >= count:
                self._error(zone.name, f"Output '{item_name}': index {src_idx} "
                                       f"out of range on '{src_name}' "
                                       f"(has {count})")

    def run(self):
        # Nodes, zone-inner nodes, and zone names share the builder's
        # namespace; any collision silently rebinds a name there.
        seen: set[str] = set()
        scopes = _tree_scopes(self.tree)
        for scope in scopes:
            for node_def in scope.own_nodes:
                if node_def.name in seen:
                    self._error(node_def.name, "Duplicate node name")
                seen.add(node_def.name)
                self.check_node(node_def)
            if scope.zone is not None:
                if scope.zone.name in seen:
                    self._error(scope.zone.name,
                                "Zone name collides with a node name")
                seen.add(scope.zone.name)

        for scope in scopes:
            float_exprs = self._float_expr_terminals(scope)
            for link in scope.links:
                self.check_link(link, scope, float_exprs)
            if scope.zone is not None:
                self.check_zone(scope)

        self.check_closure_signatures(scopes)

    def check_closure_signatures(self, scopes: list[_Scope]):
        closures = {z.name: z for z in self.tree.zones
                    if isinstance(z, ClosureZoneDef)}
        if not closures:
            return
        for scope in scopes:
            for link in scope.links:
                self._check_evaluate_link(link, scope, closures)

    def _check_evaluate_link(self, link: LinkDef, scope: _Scope,
                             closures: dict[str, ClosureZoneDef]):
        node_def = scope.nodes.get(link.target.node)
        if node_def is None or node_def.bl_idname != 'NodeEvaluateClosure' \
                or link.target.index != 0:
            return
        zone = closures.get(link.source.node)
        if zone is None:
            return
        for side, declared, zone_side in (
                ("input", node_def.input_items, zone.inputs),
                ("output", node_def.output_items, zone.outputs)):
            signature = {it.name: it.socket_type for it in zone_side}
            for item in declared:
                expected = signature.get(item.name)
                if expected is None:
                    self._error(node_def.name,
                                f"Evaluate {side} item '{item.name}' is not "
                                f"an {side} of closure '{zone.name}' "
                                f"(has: {', '.join(signature) or 'none'})")
                elif expected != item.socket_type:
                    self._error(node_def.name,
                                f"Evaluate {side} item '{item.name}' is "
                                f"[{item.socket_type}]; closure "
                                f"'{zone.name}' declares [{expected}]")
        supplied = {it.name for it in node_def.input_items}
        for item_name in (it.name for it in zone.inputs):
            if item_name not in supplied:
                self._warn(node_def.name,
                           f"Closure input '{item_name}' not supplied; "
                           "it evaluates to its default")


def validate(tree_defs: list[TreeDef], registry: SchemaRegistry) -> list[Issue]:
    issues = resolve_names(tree_defs, registry)
    all_trees = _collect_trees(tree_defs)
    for td in all_trees.values():
        _TreeValidator(registry, td, all_trees, issues).run()
    return issues
