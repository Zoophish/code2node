![](./code2node_title.svg)

# code2node

Serialise and deserialise Blender node trees to a text DSL. Works with any node tree type that derives from Blender's `NodeTree` — shader, geometry, compositor, and custom tree types registered by other addons are all discovered automatically. Intended for LLM and agentic development of node networks.

## Install

Install as a Blender extension from the `code2node/` folder, or symlink it into your Blender addons path.

## Editor support

`vscode-nodes/` is a VS Code extension providing syntax highlighting, bracket
matching, and folding for `.nodes` and `.nodetypes` files. Install by copying
or symlinking the folder into `~/.vscode/extensions/`.

## Blender UI

The panel lives in **Node Editor > Sidebar (N) > Node IO**:

- **Export Node Tree** — serialise the active node tree to a `.nodes` file
- **Import Node Tree** — build a node tree from a `.nodes` file
- **Generate Node Schema** — dump the full type universe to a directory (all tree types)

## CLI

`schema` needs the Blender binary; `validate` and `roundtrip` run under plain `python3` — that's the fast pre-flight loop, no Blender startup.

```bash
# Generate schema for all node types (writes registry.json + browsable .nodetypes)
blender --background --python code2node/cli.py -- schema ./schema

# Find node types, then print one's sockets in paste-ready DSL refs
python3 code2node/cli.py -- query schema/node_schema/registry.json -s raycast
python3 code2node/cli.py -- query schema/node_schema/registry.json GeometryNodeSetPosition

# Parse a .nodes file; with a registry, also check types/properties/sockets
python3 code2node/cli.py -- validate material.nodes schema/node_schema/registry.json

# Roundtrip: parse and re-serialise (normalises formatting)
python3 code2node/cli.py -- roundtrip material.nodes cleaned.nodes
```

## DSL Format

Comments are `//` to end of line and `/* */` blocks.

`#` starts a preprocessor directive. `#define NAME value` substitutes
`NAME` (whole words, strings and formulas included) through the rest of
the file. Defines are file-local and may use earlier defines.

```
#define SEGMENTS 28

node "Line" [GeometryNodeMeshLine] {
  inputs { (0) = SEGMENTS, }
}
```

Sockets are addressed by **index**; the quoted name after the index is an
annotation for readability. Indices are the structural identifier — get them
from the schema detail files. The validator warns when an annotated name
doesn't match the socket at that index.

Group interface sockets may also be addressed by name alone —
`("Step Count") = 28` on a group instance, `("Radius") -> "Group Input"(1)`.
Interface names are author-chosen and stay stable while a library tool's
parameter list evolves, so documents that instance nodelib groups keep
meaning the same thing. Validation compiles name refs to indices; the
serialised form is always indexed. Names resolve on group instances,
`Group Input`/`Group Output`, and `"repeat"` zone items.

### Trees

```
tree "My Material" [ShaderNodeTree] {
  ...
}
```

### Nodes

```
node "Principled BSDF" [ShaderNodeBsdfPrincipled] @(-200, 100) {
  distribution = "MULTI_GGX"
  inputs {
    (0: "Base Color") -> "Image Texture"(0: "Color"),
    (2: "Roughness") = 0.8,
  }
}
```

- `[Type]` — Blender node type identifier in brackets
- `@(x, y)` — node position (optional, integers)
- `@(x, y, w=200, h=50)` — position with custom width/height (only when non-default)
- `(index: "Name") -> "Node"(index: "Name")` — connection; this input is sourced
  from that node's output. The same input index may appear on several lines for
  multi-input sockets (e.g. Join Geometry).
- `(index: "Name") = value` — literal socket value
- `=` values: numbers, `"strings"`, `true`/`false`, tuples `(0.8, 0.8, 0.8, 1.0)`
- Entries are comma-delimited; names are always quoted
- Properties and socket values are only emitted when they differ from factory defaults

### Node Groups

Groups define their interface and appear before the trees that reference
them; a document's trees build in file order into one shared namespace. The
instancing node is the tree type's group node (`GeometryNodeGroup`,
`ShaderNodeGroup`, …):

```
tree "My Group" [ShaderNodeTree] {
  interface {
    inputs {
      "Factor": [NodeSocketFloat],
    }
    outputs {
      "Result": [NodeSocketColor],
    }
  }

  node "Group Input" [NodeGroupInput] {
  }

  node "Mix" [ShaderNodeMix] {
    inputs {
      (0: "Factor") -> "Group Input"(0: "Factor"),
    }
  }

  node "Group Output" [NodeGroupOutput] {
    inputs {
      (0: "Result") -> "Mix"(0: "Result"),
    }
  }
}
```

Referenced via `node_tree`; the instance's sockets follow the interface order:

```
node "My Instance" [ShaderNodeGroup] {
  node_tree = "My Group"
  inputs {
    (0: "Factor") = 0.5,
  }
}
```

### Inline Trees

An `inline` tree allows code reuse without manifesting as a datablock in
Blender: it exists at compile time only, and each instance expands to a
copy of the body, framed under the instance's name.

```
inline tree "Add One" [GeometryNodeTree] {
  ...
}
```

### Imports

A tree name is a datablock identity, as in Blender: defined once, referenced
by name anywhere. Imports bring trees from other files into the document's
namespace. Module paths are python-style — dots separate segments, leading
dots climb directories from the importing file (none or one = same
directory), and the `.nodes` extension is implied:

```
import helpers, curves
import ..nodelib.masonry { tree "Voussoir Ring", tree "Stone Pillar" }
```

A comma list imports several modules whole; a tree selection applies to a
single module.

The braced form imports the named trees plus whatever trees they depend on;
entries are typed, and `tree` is the only importable kind. Without braces,
every tree in the file. Imported trees build before the document's own, so
`node_tree = "Voussoir Ring"` resolves as usual. The same name from two
different definitions is an error — rename at the source. `loader.load(path)`
resolves a file's full closure; `bh apply` does this automatically.

### Expressions

An `expr` block expands a formula in place to math nodes inside a frame
labelled with the source. The type is the domain the formula computes in:
`expr<float>` expands to Math nodes, `expr<int>` to Integer Math and Bit
Math nodes. The final node takes the block's name, so other nodes reference
`"falloff"(0)` as usual. Free identifiers bind in `inputs` to a connection or a literal.
Formula errors surface at parse time with the column:
`expr "falloff": formula col 14: ...`.

Float functions are standard math shorthand (`sin`, `min`, `floor`,
`cosh`, …), each a Math operation; operators are `+ - * / % ^`; `pi`,
`tau` and `e` are named constants. Int functions are `abs`, `sign`, `min`,
`max`, `pow`, `multiply_add`, `mod`, `div_round`, `div_floor`, `div_ceil`,
`gcd`, `lcm`, `band`, `bor`, `bxor`, `bnot`, `shift`, `rotate`; operators
are `+ - * / %` (`^` reads two ways for integers — write `pow()` or
`bxor()`). In both types `/` and `%` follow the node (truncated) and
`mod()` is floored, the useful one for cyclic indexing. Int arithmetic
wraps at 32 bits at runtime; a constant that folds outside that range is a
compile error. Literals must be whole numbers in an int formula.

```
expr<float> "falloff" @(-400, 300) {
  expression = "(cosh(k) - cosh(2*k*u)) / (cosh(k) - 1)"
  inputs {
    "k" = 4.0,
    "u" -> "SepX"(0),
  }
}

expr<int> "pla_id" {
  expression = "mod(parent * 1664525 + (slot + 1) * 1013904223, 65536)"
  inputs {
    "parent" -> "Parent ID"(0),
    "slot" -> "Slot"(0),
  }
}
```

### Repeat Zones

Repeat zones replace unrolled loop patterns with a single iterated block:

```
repeat "Ray March" @(-500, 100) {
  iterations = 32
  items {
    "t": [NodeSocketFloat] = 0.0,
    "search mag": [NodeSocketFloat] = 0.0,
  }

  node "Evaluate" [ShaderNodeGroup] {
    node_tree = "sp_EvaluateQuadric"
    inputs {
      (0: "t") -> "repeat"(0: "t"),
      (1: "search mag") -> "repeat"(1: "search mag"),
    }
  }

  outputs {
    "t" -> "Evaluate"(0),
    "search mag" -> "Evaluate"(1),
  }
}
```

- `iterations` — literal, or a connection: `iterations -> "Some Node"(0)`
- `items` — carried values that loop between iterations: `"name": [SocketType] = initial`,
  or with an initial connection: `"name": [SocketType] -> "Outer Node"(0)`
- Inner nodes read carried values from the pseudo-node `"repeat"`, indexed in
  item order
- Inner nodes can reference external nodes directly
- `outputs` — maps carried items back from inner node outputs
- External nodes read the zone's final outputs via the zone name: `"Ray March"(0)`

### Closures

A closure zone packages a subnetwork as a value: its `inputs` and `outputs`
declare the signature, the body computes it. The zone compiles to Blender's
Closure zone pair; the DSL adds no semantics of its own. External nodes read
the closure value via the zone name — `"Falloff"(0)` is the Closure socket:

```
closure "Falloff" @(-400, 300) {
  inputs {
    "u": [NodeSocketFloat],
  }

  node "shape" [ShaderNodeMath] {
    operation = "MULTIPLY"
    inputs {
      (0) -> "closure"(0: "u"),
      (1) = 2.0,
    }
  }

  outputs {
    "result": [NodeSocketFloat] -> "shape"(0),
  }
}
```

- Inner nodes read the closure's parameters from the pseudo-node
  `"closure"`, indexed in input order
- Inner nodes can reference external nodes directly (captured at definition)
- `outputs` declares each result and maps it from an inner node output

`NodeEvaluateClosure` calls a closure. Typed entries in its
`inputs`/`outputs` blocks declare its items; input 0 is the Closure socket,
so input items start at index 1, output items at 0. `validate` checks the
declared items against the closure's signature when both are in the
document:

```
node "eval falloff" [NodeEvaluateClosure] {
  inputs {
    (0: "Closure") -> "Falloff"(0),
    "u": [NodeSocketFloat] -> "SepX"(0),
  }
  outputs {
    "result": [NodeSocketFloat],
  }
}
```

## Schema Output

The schema generator writes a machine-readable registry plus browsable text:

```
node_schema/
  README.md
  registry.json        # full universe as JSON (types, properties, per-variant sockets)
  ShaderNodeTree/
    index.nodetypes    # one summary entry per node type
    ShaderNodeMath.nodetypes
    ...
  GeometryNodeTree/
    index.nodetypes
    ...
```

Detail files carry full property definitions, enum values, and socket
signatures per variant — this is where socket indices come from.

## Module Structure

| File | Purpose |
|------|---------|
| `core.py` | IR dataclasses (`TreeDef`, `NodeDef`, `LinkDef`, `RepeatZoneDef`, `ClosureZoneDef`) and `bpy.NodeTree` ↔ IR conversion |
| `format.py` | DSL text ↔ IR serialisation, schema text formatting |
| `schema.py` | Introspects `bpy.types` to extract the full node type universe; JSON round-trip |
| `validate.py` | Offline validation of parsed trees against the registry (no bpy) |
| `cli.py` | Headless CLI (`validate`/`roundtrip` run under python3; `schema` needs Blender) |
| `blender_addon.py` | Addon operators and panel (requires bpy) |
| `__init__.py` | Package init; registers the addon when bpy is present |
