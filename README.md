<img src="./blendharness_512.png" align="right" width="180" alt="">

# code2node

Author Blender nodes as code instead of data files. Code2node is a library that serialises and compiles Blender node trees from a text based node language. It works with any node tree type that derives from Blender's `NodeTree`; i.e. shader, geometry, compositor, and custom tree types registered by other addons.

## Overview

### CLI

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

### VSCode Support

`vscode-nodes/` is a VS Code extension providing syntax highlighting, bracket matching, and folding for `.nodes` and `.nodetypes` files.

Download the `.vsix` from the latest release, then install it from the
Extensions view (`...` menu > Install from VSIX) or the command line:

```bash
code --install-extension blender-nodes-dsl-0.1.0.vsix
```

### Blender Extension

Install `code2node-<version>.zip` from the latest release.

**Node Editor > Sidebar (N) > code2node**:

- **Export Node Tree** — serialise the active node tree to a `.nodes` file
- **Import Node Tree** — build a node tree from a `.nodes` file
- **Auto Layout** — arrange the active node tree: dataflow left to right,
  frames and zones as blocks
- **Generate Node Schema** — register every node type to a directory

## Node Language Reference

The `.nodes` syntax is fundamentally based on block definitions, similar to JSON, supporting arbitrary node
types which are registered in the schema. It contains extras such as a preprocessor, expressions
and compile time constructs to improve compactness and legibility.


### Lexical elements

**Keywords**

| Keyword | Description |
|---------|-------------|
| `tree` | Declares a node tree |
| `node` | Declares a node |
| `frame` | Declares a frame |
| `reroute` | Declares a reroute |
| `repeat` | Declares a repeat zone |
| `closure` | Declares a closure zone |
| `expr` | Declares a formula, compiled to math nodes |
| `import` | Imports trees from another module |
| `inline` | Qualifies a declaration as compile time only, producing no datablock |
| `interface` | Holds the `inputs`/`outputs` blocks of a tree's interface |
| `inputs` | Lists input sockets of a node, `interface` or zone; bindings of an `expr` |
| `outputs` | Lists output sockets, or a zone's result mappings |
| `items` | Lists the values a `repeat` zone carries between iterations |
| `children` | Lists the nodes a `frame` contains |

**Syntax**

| Form | Description |
|------|---------|
| `{ }` | Block body |
| `[Type]` | Type identifier: a node type, or a socket type |
| `<numeric type>` | Numeric type parameter, as on `expr<float>` |
| `"..."` | Quoted string: the name of a declaration, or a string literal |
| `(index)` | Socket by index |
| `("Name")` | Socket by name |
| `(index: "Name")` | Socket by index, annotated with the socket's name |
| `->` | Connection, target on the left |
| `=` | Literal value |
| `@(x, y)` | Position, and optional size `w=` and `h=` |
| `,` | Terminates entries in a block |
| `:` | Binds a name to a type |
| `.` | Separates module path segments in an `import` |
| `//`, `/* */` | Comment to end of line, and block comment |
| `#` | Preprocessor directive |

**Literals**

| Type | Examples | Description |
|------|----------|-------|
| Integer | `28`, `-4` | Digits with no point or exponent |
| Float | `0.8`, `-2.5`, `1e3` | A point or an exponent makes it a float |
| String | `"Base Color"` | Double quotes, with no escape sequences |
| Boolean | `true`, `false` | |
| Tuple | `(0.8, 0.8, 0.8, 1.0)` | Comma-separated values, for vectors and colours |

### Preprocessor

`#` starts a preprocessor directive. `#define NAME value` substitutes
`NAME` (whole words, strings and formulas included) through the rest of
the file. Definitions are file-local and may use earlier definitions.

```
#define SEGMENTS 28

node "Line" [GeometryNodeMeshLine] {
  inputs { (0) = SEGMENTS, }
}
```

### Trees

`tree` blocks define node trees in Blender. Each materialises as a
datablock with the tree's name.

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

- `[Type]` — Blender node type identifier
- `@(x, y)` — node position (optional, integers)
- `@(x, y, w=200, h=50)` — position with custom width/height (only when non-default)
- `(index: "Name") -> "Node"(index: "Name")` — connection; this input is sourced
  from that node's output. The same input index may appear on several lines for
  multi-input sockets (e.g. Join Geometry).
- `(index: "Name") = value` — literal socket value
- `=` values: numbers, `"strings"`, `true`/`false`, tuples `(0.8, 0.8, 0.8, 1.0)`
- Entries are comma-delimited; names are always quoted
- Properties and socket values are only emitted when they differ from factory defaults

Sockets are addressed by **index**; the quoted name after the index is an
annotation for readability. Indices are the structural identifier — get them
from the schema detail files. The validator warns when an annotated name
doesn't match the socket at that index.

Group interface sockets may also be addressed by name alone —
`("Step Count") = 28` on a group instance, `("Radius") -> "Group Input"(1)`.
Names resolve on group instances, `Group Input`/`Group Output`, and
`"repeat"` zone items, and keep working when a group's sockets are added
or reordered. Validation compiles name refs to indices, and output files
use indices.

### Frames

`children` lists the nodes a frame contains. `label` sets the text drawn on
it, and is emitted only when it differs from the name.

```
frame "Setup" @(-600, 200) label="Base mesh" {
  children {
    "Line",
    "Set Position",
  }
}
```

### Reroutes

A reroute takes no block. `->` connects its input.

```
reroute "bus" @(-200, 0) -> "Set Position"(0)
```

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

An `inline` tree exists at compile time only (does not emit a datablock). Each instance expands to a
copy of the body, framed under the instance's name. This can be used for tree reuse without polluting
the datablocks in Blender.

```
inline tree "Add One" [GeometryNodeTree] {
  ...
}
```

### Imports

Imports bring trees from other files into the document's namespace. Module
paths use dots between segments, and leading dots climb directories from
the importing file (none or one = same directory). The `.nodes` extension
is implied.

```
import helpers, curves
import ..lib.masonry { tree "Arch", tree "Pillar" }
```

A comma list imports whole modules. The braced form takes only the named
trees and their dependencies.

Imported trees build before the trees that reference them. A tree reached
by two import paths is deduplicated; two different trees sharing a name
must be renamed at the source.

### Expressions

An `expr` block expands a formula in place to math nodes inside a frame
labelled with the source. Its type governs which operators and functions
are available, and how the arithmetic behaves: `expr<float>` expands to
Math nodes, `expr<int>` to Integer Math and Bit Math nodes. The final
node takes the block's name. Free identifiers bind in `inputs` to a
connection or a literal.

Float functions are standard math shorthand (`sin`, `min`, `floor`,
`cosh`, …). Operators are `+ - * / % ^`, and `pi`,
`tau` and `e` are named constants.

Int functions are `abs`, `sign`, `min`, `max`, `pow`, `multiply_add`,
`mod`, `div_round`, `div_floor`, `div_ceil`, `gcd`, `lcm`, `band`, `bor`,
`bxor`, `bnot`, `shift`, `rotate`. Operators are `+ - * / %` (`^` reads
two ways for integers — write `pow()` or `bxor()`).

In both types `/` and `%` follow the node (truncated) and `mod()` is
floored. Int arithmetic wraps at 32 bits at runtime, and a constant that
folds outside that range produces a compile error. Literals must be whole
numbers in an int formula.

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
repeat "Accumulate" @(-500, 100) {
  iterations = 16
  items {
    "total": [NodeSocketFloat] = 0.0,
    "step": [NodeSocketFloat] = 1.0,
  }

  node "Add Step" [ShaderNodeMath] {
    operation = "ADD"
    inputs {
      (0) -> "repeat"(0: "total"),
      (1) -> "repeat"(1: "step"),
    }
  }

  node "Halve Step" [ShaderNodeMath] {
    operation = "MULTIPLY"
    inputs {
      (0) -> "repeat"(1: "step"),
      (1) = 0.5,
    }
  }

  outputs {
    "total" -> "Add Step"(0),
    "step" -> "Halve Step"(0),
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
  registry.json        # every node type as JSON (types, properties, per-variant sockets)
  ShaderNodeTree/
    index.nodetypes    # one summary entry per node type
    ShaderNodeMath.nodetypes
    ...
  GeometryNodeTree/
    index.nodetypes
    ...
```

Socket indices come from the detail files, which list each variant's
properties, enum values and sockets.
