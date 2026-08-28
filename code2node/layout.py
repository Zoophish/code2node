# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sam Warren
from collections import defaultdict

from .core import NodeDef, RepeatZoneDef, TreeDef, ZoneDef
from .expr import ARITY, INT_ARITY, _DEFAULT_OP

H_GAP = 60.0
V_GAP = 30.0
FRAME_PAD = 30.0
FRAME_HEADER = 20.0
ZONE_PAD = 60.0
FOLD_H_GAP = 40.0
FOLD_V_GAP = 20.0
FOLD_MIN = 4       # shortest chain worth folding
FOLD_ASPECT = 1.5  # target width:height of a folded block
BAND_WIDTH = 900.0  # consumer clustering for parameter localisation

_DEFAULT_WIDTH = 140.0
_WIDTHS = {'NodeReroute': 16.0}
_HEADER = 24.0
_ROW = 22.0
_HIDDEN_SIZE = (100.0, 32.0)
_PSEUDO = ('repeat', 'closure')

_MATH_ARITY = {'ShaderNodeMath': ARITY,
               'FunctionNodeIntegerMath': INT_ARITY,
               'FunctionNodeBitMath': INT_ARITY}


def layout_trees(trees: list[TreeDef], registry=None):
    for tree in trees:
        layout_tree(tree, registry)


def layout_tree(tree: TreeDef, registry=None,
                sizes: dict[str, tuple[float, float]] | None = None,
                collapse: bool = True, localise: bool = True):
    if collapse:
        collapse_nodes(tree)
    top, paths = _build_graph(tree, registry, sizes)
    top.finalise()
    if localise and _localise_parameters(tree, paths):
        top, paths = _build_graph(tree, registry, sizes)
        top.finalise()
    top.emit(0.0, 0.0)


def _build_graph(tree: TreeDef, registry, sizes):
    sizer = _Sizer(tree, registry, sizes)
    top = _Group()
    paths: dict[str, list] = {}

    _add_nodes(top, tree.nodes, [], paths, sizer)
    for zone in tree.zones:
        group = _Group(pads=(ZONE_PAD, ZONE_PAD, ZONE_PAD, ZONE_PAD))
        zone_in = _ZoneIO(zone, 'in', sizer)
        zone_out = _ZoneIO(zone, 'out', sizer)
        group.members.extend((zone_in, zone_out))
        group.io = (zone_in, zone_out)
        top.members.append(group)
        paths[zone.name] = [group]
        _add_nodes(group, zone.nodes, [group], paths, sizer)

    _connect(tree, top, paths)
    return top, paths


# ---------------------------------------------------------------------------
# Collapsing
# ---------------------------------------------------------------------------

def collapse_nodes(tree: TreeDef):
    for nodes, links in _scope_pairs(tree):
        expr_framed = {c for n in nodes
                       if n.bl_idname == 'NodeFrame'
                       and n.name.endswith('.expr')
                       for c in n.children}
        linked: dict[str, set[int]] = defaultdict(set)
        for link in links:
            linked[link.target.node].add(link.target.index)
        for node in nodes:
            if node.bl_idname == 'NodeFrame':
                continue
            if node.name in expr_framed:
                node.hide = True
                continue
            arities = _MATH_ARITY.get(node.bl_idname)
            if arities is None or node.input_values:
                continue
            op = node.properties.get('operation',
                                     _DEFAULT_OP.get(node.bl_idname))
            arity = arities.get(op)
            if arity and set(range(arity)) <= linked[node.name]:
                node.hide = True


# ---------------------------------------------------------------------------
# Size estimation
# ---------------------------------------------------------------------------

class _Sizer:
    def __init__(self, tree: TreeDef, registry, sizes: dict | None):
        self.groups = tree.groups
        self.sizes = sizes or {}
        if registry is not None:
            from .validate import _global_types
            self.node_types = _global_types(registry)
        else:
            self.node_types = {}
        # Socket indices seen in use, per node — the registry-free floor.
        self.seen: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for nodes, links in _scope_pairs(tree):
            for link in links:
                self.seen[link.target.node][0] = max(
                    self.seen[link.target.node][0], link.target.index + 1)
                self.seen[link.source.node][1] = max(
                    self.seen[link.source.node][1], link.source.index + 1)
            for node in nodes:
                counts = self.seen[node.name]
                if node.input_values:
                    counts[0] = max(counts[0], max(node.input_values) + 1)
                if node.output_values:
                    counts[1] = max(counts[1], max(node.output_values) + 1)

    def size(self, node: NodeDef) -> tuple[float, float]:
        if node.bl_idname == 'NodeReroute':
            return _WIDTHS['NodeReroute'], 16.0
        if node.hide:
            return _HIDDEN_SIZE
        if node.name in self.sizes:
            return self.sizes[node.name]
        width = node.width or _WIDTHS.get(node.bl_idname, _DEFAULT_WIDTH)
        n_in, n_out = self._socket_counts(node)
        rows = n_in + n_out + len(node.properties)
        return width, _HEADER + _ROW * max(rows, 1) + 10.0

    def _socket_counts(self, node: NodeDef) -> tuple[int, int]:
        if node.node_tree_name in self.groups:
            iface = self.groups[node.node_tree_name].interface
            return (sum(s.direction == 'INPUT' for s in iface),
                    sum(s.direction == 'OUTPUT' for s in iface))
        type_def = self.node_types.get(node.bl_idname)
        if type_def is not None:
            from .validate import _active_variant
            variant, _ = _active_variant(node, type_def)
            if variant is not None:
                return (len(variant.inputs) + len(node.input_items),
                        len(variant.outputs) + len(node.output_items))
        return tuple(self.seen[node.name])


def _scope_pairs(tree: TreeDef):
    yield tree.nodes, tree.links
    for zone in tree.zones:
        yield zone.nodes, zone.links


# ---------------------------------------------------------------------------
# Layout units
# ---------------------------------------------------------------------------
# Internal coordinates grow downward from a local (0, 0) origin; emit()
# converts to Blender space (y up) when it writes locations.

class _Leaf:
    def __init__(self, node: NodeDef, sizer: _Sizer):
        self.node = node
        self.w, self.h = sizer.size(node)
        self.x = self.y = 0.0

    def finalise(self):
        pass

    def emit(self, x: float, y: float):
        self.node.location = (x, -y)


class _ZoneIO:
    def __init__(self, zone: ZoneDef, side: str, sizer: _Sizer):
        self.zone = zone
        self.side = side
        if isinstance(zone, RepeatZoneDef):
            rows = len(zone.items) + 1
        else:
            rows = len(zone.inputs) + len(zone.outputs)
        measured = sizer.sizes.get(zone.name) if side == 'in' else None
        self.w, self.h = measured or (_DEFAULT_WIDTH,
                                      _HEADER + _ROW * max(rows, 1) + 10.0)
        self.x = self.y = 0.0

    def finalise(self):
        pass

    def emit(self, x: float, y: float):
        if self.side == 'in':
            self.zone.location = (x, -y)
        else:
            self.zone.output_location = (x, -y)


class _Group:
    def __init__(self, pads=(0.0, 0.0, 0.0, 0.0), frame: NodeDef | None = None):
        self.pads = pads  # left, top, right, bottom
        self.frame = frame
        self.members: list = []
        self.edges: set[tuple] = set()
        self.io = None  # zone (input, output) units
        self.w = self.h = 0.0
        self.x = self.y = 0.0

    def finalise(self):
        for member in self.members:
            member.finalise()
        if self.io is not None:
            self.edges.add(self.io)
        self.members, self.edges = _fold_chains(self.members, self.edges)
        left, top, right, bottom = self.pads
        w, h = _arrange(self.members, self.edges)
        self.w = w + left + right
        self.h = h + top + bottom

    def emit(self, x: float, y: float):
        left, top, _, _ = self.pads
        if self.frame is not None:
            self.frame.location = (x, -y)
            x = y = 0.0
        for member in self.members:
            member.emit(x + left + member.x, y + top + member.y)


class _Stack:
    def __init__(self, units: list):
        self.units = units
        width = max(u.w for u in units)
        y = 0.0
        for unit in units:
            unit.x = (width - unit.w) / 2.0
            unit.y = y
            y += unit.h + FOLD_V_GAP
        self.w = width
        self.h = y - FOLD_V_GAP
        self.x = self.y = 0.0

    def finalise(self):
        pass

    def emit(self, x: float, y: float):
        for unit in self.units:
            unit.emit(x + unit.x, y + unit.y)


class _Pack:
    def __init__(self, units: list, serpentine: bool = True):
        self.units = units
        self.serpentine = serpentine
        area = sum((u.w + FOLD_H_GAP) * (u.h + FOLD_V_GAP) for u in units)
        target = max(max(u.w for u in units), (area * FOLD_ASPECT) ** 0.5)
        rows: list[list] = [[]]
        x = 0.0
        for unit in units:
            if rows[-1] and x + unit.w > target:
                rows.append([])
                x = 0.0
            unit.x = x
            rows[-1].append(unit)
            x += unit.w + FOLD_H_GAP
        y = 0.0
        self.w = 0.0
        for i, row in enumerate(rows):
            row_w = sum(u.w for u in row) + FOLD_H_GAP * (len(row) - 1)
            row_h = max(u.h for u in row)
            for unit in row:
                if self.serpentine and i % 2:
                    unit.x = row_w - unit.x - unit.w
                unit.y = y + (row_h - unit.h) / 2.0
            y += row_h + FOLD_V_GAP
            self.w = max(self.w, row_w)
        self.h = y - FOLD_V_GAP
        self.x = self.y = 0.0

    def finalise(self):
        pass

    def emit(self, x: float, y: float):
        for unit in self.units:
            unit.emit(x + unit.x, y + unit.y)


def _contract(members: list, edges: set, into: dict) -> tuple[list, set]:
    seen: set = set()
    ordered = []
    for member in members:
        unit = into.get(member, member)
        if id(unit) not in seen:
            seen.add(id(unit))
            ordered.append(unit)
    remapped = {(into.get(a, a), into.get(b, b)) for a, b in edges}
    return ordered, {(a, b) for a, b in remapped if a is not b}


def _fold_chains(members: list, edges: set) -> tuple[list, set]:
    succ: dict = defaultdict(set)
    pred: dict = defaultdict(set)
    for a, b in edges:
        succ[a].add(b)
        pred[b].add(a)

    into: dict = {}
    for unit in members:
        if isinstance(unit, _ZoneIO):
            continue
        leaves = [p for p in members
                  if p in pred[unit] and not pred[p]
                  and succ[p] == {unit} and not isinstance(p, _ZoneIO)]
        if leaves:
            stack = _Stack(leaves + [unit])
            for part in (*leaves, unit):
                into[part] = stack
    members, edges = _contract(members, edges, into)

    succ, pred = defaultdict(set), defaultdict(set)
    for a, b in edges:
        succ[a].add(b)
        pred[b].add(a)

    follows: dict = {}
    continued: set = set()  # spine targets already claimed
    for unit in members:
        if isinstance(unit, _ZoneIO) or len(succ[unit]) != 1:
            continue
        (nxt,) = succ[unit]
        if isinstance(nxt, _ZoneIO) or id(nxt) in continued:
            continue
        follows[unit] = nxt
        continued.add(id(nxt))

    heads = set(follows) - set(follows.values())
    into = {}
    for head in [m for m in members if m in heads]:
        run = [head]
        while run[-1] in follows:
            run.append(follows[run[-1]])
        if len(run) < FOLD_MIN:
            continue
        block = _Pack(run)
        for unit in run:
            into[unit] = block
    members, edges = _contract(members, edges, into)

    # Parallel twins — units sharing all predecessors and successors —
    # pack into a grid instead of one tall column.
    succ, pred = defaultdict(set), defaultdict(set)
    for a, b in edges:
        succ[a].add(b)
        pred[b].add(a)
    twins: dict = defaultdict(list)
    for unit in members:
        if not isinstance(unit, _ZoneIO):
            twins[(frozenset(id(p) for p in pred[unit]),
                   frozenset(id(s) for s in succ[unit]))].append(unit)
    into = {}
    for group in twins.values():
        if len(group) < 3:
            continue
        block = _Pack(group, serpentine=False)
        for unit in group:
            into[unit] = block
    return _contract(members, edges, into)


def _add_nodes(group: _Group, nodes: list[NodeDef], path: list,
               paths: dict, sizer: _Sizer):
    by_name = {n.name: n for n in nodes}
    framed = {c for n in nodes if n.bl_idname == 'NodeFrame'
              for c in n.children}

    def build(node: NodeDef, container: _Group, node_path: list):
        if node.bl_idname == 'NodeFrame':
            sub = _Group(pads=(FRAME_PAD, FRAME_PAD + FRAME_HEADER,
                               FRAME_PAD, FRAME_PAD), frame=node)
            container.members.append(sub)
            paths[node.name] = node_path + [sub]
            for child in node.children:
                if child in by_name:
                    build(by_name[child], sub, node_path + [sub])
        else:
            leaf = _Leaf(node, sizer)
            container.members.append(leaf)
            paths[node.name] = node_path + [leaf]

    for node in nodes:
        if node.name not in framed:
            build(node, group, path)


# ---------------------------------------------------------------------------
# Parameter localisation
# ---------------------------------------------------------------------------

_PARAM_TYPES = ('NodeGroupInput', 'ShaderNodeValue', 'FunctionNodeInputInt',
                'FunctionNodeInputVector', 'FunctionNodeInputBool',
                'FunctionNodeInputColor', 'FunctionNodeInputString',
                'FunctionNodeInputRotation', 'GeometryNodeInputMaterial')


def _localise_parameters(tree: TreeDef, paths: dict) -> bool:
    import copy as _copy

    def band(name):
        path = paths.get(name)
        if path is None:
            return None
        x = 0.0
        for unit in path:
            x += unit.x
        return int(x // BAND_WIDTH)

    taken = {n.name for n in tree.nodes}
    taken.update(n.name for z in tree.zones for n in z.nodes)
    framed = {c for n in tree.nodes for c in n.children}
    changed = False

    for source in [n for n in tree.nodes
                   if n.bl_idname in _PARAM_TYPES and n.name not in framed]:
        consumers: dict[int, list] = defaultdict(list)
        for link in tree.links:
            if link.source.node == source.name:
                b = band(link.target.node)
                if b is not None:
                    consumers[b].append(link)
        for zone in tree.zones:
            b = band(zone.name)
            for link in zone.links:
                if link.source.node == source.name and b is not None:
                    consumers[b].append(link)
        if len(consumers) < 2:
            continue
        for i, b in enumerate(sorted(consumers)[1:], start=1):
            name = f"{source.name}.{i:03d}"
            while name in taken:
                i += 1
                name = f"{source.name}.{i:03d}"
            taken.add(name)
            clone = _copy.deepcopy(source)
            clone.name = name
            clone.hide = True
            clone.children = []
            tree.nodes.append(clone)
            for link in consumers[b]:
                link.source.node = name
            changed = True
    return changed


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------

def _connect(tree: TreeDef, top: _Group, paths: dict):
    def edge(source_path, target_path):
        if source_path is None or target_path is None:
            return
        i = 0
        while (i < len(source_path) and i < len(target_path)
               and source_path[i] is target_path[i]):
            i += 1
        if i == len(source_path) or i == len(target_path):
            return  # one endpoint contains the other
        container = top if i == 0 else source_path[i - 1]
        container.edges.add((source_path[i], target_path[i]))

    for link in tree.links:
        edge(paths.get(link.source.node), paths.get(link.target.node))

    for zone in tree.zones:
        zone_path = paths[zone.name]
        group = zone_path[-1]
        zone_in, zone_out = group.io

        def zone_ref(name):
            if name in _PSEUDO:
                return zone_path + [zone_in]
            return paths.get(name)

        for link in zone.links:
            edge(zone_ref(link.source.node), zone_ref(link.target.node))
        for source in zone.output_mappings.values():
            edge(zone_ref(source[0]), zone_path + [zone_out])
        if isinstance(zone, RepeatZoneDef):
            if isinstance(zone.iterations, tuple):
                edge(zone_ref(zone.iterations[0]), zone_path + [zone_in])
            for item in zone.items:
                if item.initial_connection is not None:
                    edge(zone_ref(item.initial_connection[0]),
                         zone_path + [zone_in])


# ---------------------------------------------------------------------------
# Arrangement
# ---------------------------------------------------------------------------

def _arrange(members: list, edges: set) -> tuple[float, float]:
    if not members:
        return 0.0, 0.0
    succ: dict = defaultdict(set)
    pred: dict = defaultdict(set)
    for a, b in edges:
        succ[a].add(b)
        pred[b].add(a)

    rank: dict = {}
    active: set = set()

    def to_sink(unit) -> int:
        if id(unit) in rank:
            return rank[id(unit)]
        if id(unit) in active:
            return 0
        active.add(id(unit))
        rank[id(unit)] = max((to_sink(s) + 1 for s in succ[unit]), default=0)
        active.discard(id(unit))
        return rank[id(unit)]

    max_rank = max(to_sink(u) for u in members)
    column: dict = {}
    for unit in members:
        if not succ[unit] and not pred[unit]:
            column[id(unit)] = 0  # unconnected: leftmost
        else:
            column[id(unit)] = max_rank - rank[id(unit)]

    columns: list[list] = [[] for _ in range(max_rank + 1)]
    for unit in members:  # original order — deterministic start
        columns[column[id(unit)]].append(unit)

    # Barycenter ordering: order each column by the mean position of its
    # neighbours, sweeping both directions.
    pos = {id(u): i for col in columns for i, u in enumerate(col)}

    def sweep(neighbours, cols):
        for col in cols:
            keys = {}
            for i, unit in enumerate(col):
                near = [pos[id(n)] for n in neighbours[unit]]
                keys[id(unit)] = sum(near) / len(near) if near else float(i)
            col.sort(key=lambda u: keys[id(u)])
            for i, unit in enumerate(col):
                pos[id(unit)] = i

    for _ in range(2):
        sweep(pred, columns)
        sweep(succ, columns[::-1])

    # Coordinates: columns left to right, then alignment passes pulling
    # each unit toward its neighbours' mean centre without reordering.
    x = 0.0
    for col in columns:
        width = max(u.w for u in col)
        for unit in col:
            unit.x = x + (width - unit.w) / 2.0
        x += width + H_GAP
    total_w = x - H_GAP

    centre = {}
    for col in columns:
        y = 0.0
        for unit in col:
            centre[id(unit)] = y + unit.h / 2.0
            y += unit.h + V_GAP

    for _ in range(3):
        for col in columns:
            desired = {}
            for unit in col:
                near = [centre[id(n)]
                        for n in (*pred[unit], *succ[unit])]
                desired[id(unit)] = (sum(near) / len(near) if near
                                     else centre[id(unit)])
            floor = None
            for unit in col:
                c = desired[id(unit)]
                if floor is not None:
                    c = max(c, floor + unit.h / 2.0)
                centre[id(unit)] = c
                floor = c + unit.h / 2.0 + V_GAP

    top_y = min(centre[id(u)] - u.h / 2.0 for u in members)
    for unit in members:
        unit.y = centre[id(unit)] - unit.h / 2.0 - top_y
    total_h = max(u.y + u.h for u in members)
    return total_w, total_h
