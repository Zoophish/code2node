# Copyright (C) 2026, Sam Warren, All rights reserved.
"""
Expression compiler: a formula string lowers to Math nodes inside a frame.

Each expression type is the closure of a Math node family under composition:
literals, bound identifiers, unary minus, operators, parentheses, and calls
in standard math shorthand, each mapping onto one node operation.
`expr<float>` is the closure of the float Math node; `expr<int>` of the
integer math and bit math nodes. Compilation runs at
document parse time: lexer, Pratt parser, constant folding, common
subexpression elimination, deterministic emission. The final node takes the
expression's name, so downstream references resolve like any node's.

Folding computes each operation's mathematical definition at full python
precision; it does not model the runtime representation (float32 rounding,
int32 wraparound). Cases the runtime defines but the definition does not
(division by zero, negative integer exponents) stay nodes, and an integer
constant that folds outside int32 range is a compile error rather than a
silent divergence from the wrapping nodes.
"""
import math

from .core import LinkDef, NodeDef, SocketRef


class ExprError(Exception):
    def __init__(self, msg: str, col: int):
        super().__init__(msg)
        self.msg = msg
        self.col = col


INT_MIN, INT_MAX = -2**31, 2**31 - 1


# ---------------------------------------------------------------------------
# Float tables: the float Math node closure
# ---------------------------------------------------------------------------

# operation -> arity, keyed by the ShaderNodeMath enum.
ARITY = {
    'ADD': 2, 'SUBTRACT': 2, 'MULTIPLY': 2, 'DIVIDE': 2, 'POWER': 2,
    'LOGARITHM': 2, 'SQRT': 1, 'INVERSE_SQRT': 1, 'ABSOLUTE': 1,
    'EXPONENT': 1, 'MINIMUM': 2, 'MAXIMUM': 2, 'LESS_THAN': 2,
    'GREATER_THAN': 2, 'SIGN': 1, 'COMPARE': 3, 'SMOOTH_MIN': 3,
    'SMOOTH_MAX': 3, 'ROUND': 1, 'FLOOR': 1, 'CEIL': 1, 'TRUNC': 1,
    'FRACT': 1, 'MODULO': 2, 'FLOORED_MODULO': 2, 'WRAP': 3, 'SNAP': 2,
    'PINGPONG': 2, 'SINE': 1, 'COSINE': 1, 'TANGENT': 1, 'ARCSINE': 1,
    'ARCCOSINE': 1, 'ARCTANGENT': 1, 'ARCTAN2': 2, 'SINH': 1, 'COSH': 1,
    'TANH': 1, 'RADIANS': 1, 'DEGREES': 1, 'MULTIPLY_ADD': 3,
}

# Surface names are standard math shorthand; each maps onto a Math
# operation. `%` is Blender's MODULO (truncated); mod() is floored, the
# useful one for cyclic indexing.
FUNCTIONS = {
    'sin': 'SINE', 'cos': 'COSINE', 'tan': 'TANGENT',
    'asin': 'ARCSINE', 'acos': 'ARCCOSINE', 'atan': 'ARCTANGENT',
    'atan2': 'ARCTAN2', 'sinh': 'SINH', 'cosh': 'COSH', 'tanh': 'TANH',
    'sqrt': 'SQRT', 'rsqrt': 'INVERSE_SQRT', 'abs': 'ABSOLUTE',
    'pow': 'POWER',
    'sign': 'SIGN', 'exp': 'EXPONENT', 'log': 'LOGARITHM',
    'floor': 'FLOOR', 'ceil': 'CEIL', 'round': 'ROUND', 'trunc': 'TRUNC',
    'fract': 'FRACT', 'radians': 'RADIANS', 'degrees': 'DEGREES',
    'min': 'MINIMUM', 'max': 'MAXIMUM', 'mod': 'FLOORED_MODULO',
    'snap': 'SNAP', 'wrap': 'WRAP', 'pingpong': 'PINGPONG',
    'less_than': 'LESS_THAN', 'greater_than': 'GREATER_THAN',
    'compare': 'COMPARE', 'smooth_min': 'SMOOTH_MIN',
    'smooth_max': 'SMOOTH_MAX', 'multiply_add': 'MULTIPLY_ADD',
}

# Named constants: literals with names. An inputs binding of the same name
# shadows the constant.
CONSTANTS = {'pi': math.pi, 'tau': math.tau, 'e': math.e}

BINOPS = {'+': 'ADD', '-': 'SUBTRACT', '*': 'MULTIPLY', '/': 'DIVIDE',
          '%': 'MODULO', '^': 'POWER'}

# precedence: (level, right_associative)
PRECEDENCE = {'+': (1, False), '-': (1, False), '*': (2, False),
              '/': (2, False), '%': (2, False), '^': (4, True)}
UNARY_PRECEDENCE = 3

PYFOLD = {
    'ADD': lambda a, b: a + b, 'SUBTRACT': lambda a, b: a - b,
    'MULTIPLY': lambda a, b: a * b, 'DIVIDE': lambda a, b: a / b,
    'POWER': lambda a, b: a ** b, 'MINIMUM': min, 'MAXIMUM': max,
    'ABSOLUTE': abs, 'FLOOR': math.floor, 'CEIL': math.ceil,
    # Blender rounds half away from zero; python's round() is half-to-even.
    'ROUND': lambda a: math.floor(a + 0.5) if a >= 0 else math.ceil(a - 0.5),
    'TRUNC': math.trunc, 'FRACT': lambda a: a - math.floor(a),
    'SQRT': math.sqrt, 'EXPONENT': math.exp, 'SIGN': lambda a: (a > 0) - (a < 0),
    'MODULO': math.fmod, 'FLOORED_MODULO': lambda a, b: a - b * math.floor(a / b),
    'SNAP': lambda a, b: math.floor(a / b) * b,
    'SINE': math.sin, 'COSINE': math.cos, 'TANGENT': math.tan,
    'ARCSINE': math.asin, 'ARCCOSINE': math.acos, 'ARCTANGENT': math.atan,
    'ARCTAN2': math.atan2, 'SINH': math.sinh, 'COSH': math.cosh,
    'TANH': math.tanh, 'RADIANS': math.radians, 'DEGREES': math.degrees,
    'LESS_THAN': lambda a, b: 1.0 if a < b else 0.0,
    'GREATER_THAN': lambda a, b: 1.0 if a > b else 0.0,
    'MULTIPLY_ADD': lambda a, b, c: a * b + c,
}


# ---------------------------------------------------------------------------
# Int tables: the integer math + bit math node closure
# ---------------------------------------------------------------------------
# The two enums share no names, so one operation namespace covers both.
# Semantics measured in Blender 5.1.1: DIVIDE truncates toward zero,
# DIVIDE_ROUND rounds half away from zero, MODULO is truncated and
# FLOORED_MODULO floored (matching the float surface's `%`/`mod` split),
# x/0 and x%0 are 0, POWER with a negative exponent is 0, GCD/LCM take
# absolute values, and overflow wraps two's complement. SHIFT is left for
# positive counts and a *logical* right shift for negative; ROTATE is a
# 32-bit rotate, negative counts rotating right.

INT_ARITY = {
    'ADD': 2, 'SUBTRACT': 2, 'MULTIPLY': 2, 'DIVIDE': 2, 'MULTIPLY_ADD': 3,
    'ABSOLUTE': 1, 'NEGATE': 1, 'POWER': 2, 'MINIMUM': 2, 'MAXIMUM': 2,
    'SIGN': 1, 'DIVIDE_ROUND': 2, 'DIVIDE_FLOOR': 2, 'DIVIDE_CEIL': 2,
    'FLOORED_MODULO': 2, 'MODULO': 2, 'GCD': 2, 'LCM': 2,
    'AND': 2, 'OR': 2, 'XOR': 2, 'NOT': 1, 'SHIFT': 2, 'ROTATE': 2,
}

INT_FUNCTIONS = {
    'abs': 'ABSOLUTE', 'sign': 'SIGN', 'min': 'MINIMUM', 'max': 'MAXIMUM',
    'pow': 'POWER', 'multiply_add': 'MULTIPLY_ADD',
    'mod': 'FLOORED_MODULO',
    'div_round': 'DIVIDE_ROUND', 'div_floor': 'DIVIDE_FLOOR',
    'div_ceil': 'DIVIDE_CEIL', 'gcd': 'GCD', 'lcm': 'LCM',
    'band': 'AND', 'bor': 'OR', 'bxor': 'XOR', 'bnot': 'NOT',
    'shift': 'SHIFT', 'rotate': 'ROTATE',
}

INT_BINOPS = {'+': 'ADD', '-': 'SUBTRACT', '*': 'MULTIPLY', '/': 'DIVIDE',
              '%': 'MODULO'}


def _tdiv(a, b):
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def _int_pow(a, b):
    if b < 0:
        raise ValueError  # runtime defines it (0); stays a node
    return a ** b


INT_PYFOLD = {
    'ADD': lambda a, b: a + b, 'SUBTRACT': lambda a, b: a - b,
    'MULTIPLY': lambda a, b: a * b, 'MULTIPLY_ADD': lambda a, b, c: a * b + c,
    'DIVIDE': _tdiv,
    'DIVIDE_ROUND': lambda a, b: _tdiv(2 * a + (b if (a < 0) == (b < 0)
                                                else -b), 2 * b),
    'DIVIDE_FLOOR': lambda a, b: a // b,
    'DIVIDE_CEIL': lambda a, b: -((-a) // b),
    'MODULO': lambda a, b: a - _tdiv(a, b) * b,
    'FLOORED_MODULO': lambda a, b: a % b,
    'POWER': _int_pow, 'MINIMUM': min, 'MAXIMUM': max,
    'ABSOLUTE': abs, 'NEGATE': lambda a: -a,
    'SIGN': lambda a: (a > 0) - (a < 0),
    'GCD': math.gcd, 'LCM': math.lcm,
    'AND': lambda a, b: a & b, 'OR': lambda a, b: a | b,
    'XOR': lambda a, b: a ^ b, 'NOT': lambda a: ~a,
    # SHIFT and ROTATE are representation operations; they stay nodes.
}


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

# bl_idname -> default operation (the property is omitted when it matches).
_DEFAULT_OP = {'ShaderNodeMath': 'ADD', 'FunctionNodeIntegerMath': 'ADD',
               'FunctionNodeBitMath': 'AND'}

# BitMath's third socket is Shift; two-argument SHIFT/ROTATE skip socket 1.
_INT_OPS = {}
for _op, _ar in INT_ARITY.items():
    if _op in ('AND', 'OR', 'XOR'):
        _INT_OPS[_op] = ('FunctionNodeBitMath', (0, 1), ('A', 'B'))
    elif _op == 'NOT':
        _INT_OPS[_op] = ('FunctionNodeBitMath', (0,), ('A',))
    elif _op in ('SHIFT', 'ROTATE'):
        _INT_OPS[_op] = ('FunctionNodeBitMath', (0, 2), ('A', 'Shift'))
    else:
        _INT_OPS[_op] = ('FunctionNodeIntegerMath', tuple(range(_ar)),
                         ('Value',) * _ar)


class _Backend:
    """One expression type: its surface language and its target nodes."""

    def __init__(self, name, binops, functions, arity, pyfold, constants, ops):
        self.name = name
        self.binops = binops
        self.functions = functions
        self.arity = arity
        self.pyfold = pyfold
        self.constants = constants
        self.ops = ops   # OP -> (bl_idname, input socket indices, input names)

    def binop(self, token, col):
        op = self.binops.get(token)
        if op is None:
            raise ExprError(f"'{token}' is not an operator in {self.name} "
                            "expressions", col)
        return op

    def literal(self, value, col):
        return float(value)

    def fold_result(self, value, col):
        return float(value)

    def negate(self, operand, col):
        return ('call', 'MULTIPLY', [('num', -1.0), operand], col)

    def const_node(self, name, value):
        node = NodeDef(name=name, bl_idname='ShaderNodeValue')
        node.output_values[0] = value
        node.output_names[0] = "Value"
        return node


class _IntBackend(_Backend):
    def binop(self, token, col):
        if token == '^':
            raise ExprError("'^' is power in float expressions and would be "
                            "ambiguous here; use pow() or bxor()", col)
        return super().binop(token, col)

    def literal(self, value, col):
        if not float(value).is_integer():
            raise ExprError(f"'{value}' is not an integer", col)
        value = int(value)
        if not INT_MIN <= value <= INT_MAX:
            raise ExprError(f"{value} overflows a 32-bit integer", col)
        return value

    def fold_result(self, value, col):
        value = int(value)
        if not INT_MIN <= value <= INT_MAX:
            raise ExprError(f"constant folds to {value}, which overflows a "
                            "32-bit integer", col)
        return value

    def negate(self, operand, col):
        return ('call', 'NEGATE', [operand], col)

    def const_node(self, name, value):
        return NodeDef(name=name, bl_idname='FunctionNodeInputInt',
                       properties={'integer': int(value)})


BACKENDS = {
    'float': _Backend(
        'float', BINOPS, FUNCTIONS, ARITY, PYFOLD, CONSTANTS,
        {op: ('ShaderNodeMath', tuple(range(ar)), ('Value',) * ar)
         for op, ar in ARITY.items()}),
    'int': _IntBackend(
        'int', INT_BINOPS, INT_FUNCTIONS, INT_ARITY, INT_PYFOLD, {},
        _INT_OPS),
}


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------

def _lex(text: str):
    tokens = []
    i = 0
    while i < len(text):
        c = text[i]
        if c.isspace():
            i += 1
        elif c.isdigit() or (c == '.' and i + 1 < len(text) and text[i + 1].isdigit()):
            j = i
            while j < len(text) and (text[j].isdigit() or text[j] in '.eE'
                                     or (text[j] in '+-' and text[j - 1] in 'eE')):
                j += 1
            try:
                tokens.append(('num', float(text[i:j]), i))
            except ValueError:
                raise ExprError(f"bad number '{text[i:j]}'", i)
            i = j
        elif c.isalpha() or c == '_':
            j = i
            while j < len(text) and (text[j].isalnum() or text[j] == '_'):
                j += 1
            tokens.append(('ident', text[i:j], i))
            i = j
        elif c in '+-*/%^(),':
            tokens.append((c, c, i))
            i += 1
        else:
            raise ExprError(f"unexpected character '{c}'", i)
    tokens.append(('end', '', len(text)))
    return tokens


# ---------------------------------------------------------------------------
# Parser (Pratt). AST: ('num', v) | ('var', name, col) | ('call', OP, args, col)
# ---------------------------------------------------------------------------

class _Parser:
    def __init__(self, tokens, backend):
        self.tokens = tokens
        self.backend = backend
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def next(self):
        t = self.tokens[self.pos]
        self.pos += 1
        return t

    def expect(self, kind):
        t = self.next()
        if t[0] != kind:
            raise ExprError(f"expected '{kind}', got '{t[1] or 'end'}'", t[2])
        return t

    def parse(self):
        ast = self.expression(0)
        t = self.peek()
        if t[0] != 'end':
            raise ExprError(f"unexpected '{t[1]}'", t[2])
        return ast

    def expression(self, min_prec):
        left = self.atom()
        while True:
            t = self.peek()
            if t[0] not in PRECEDENCE:
                return left
            prec, right_assoc = PRECEDENCE[t[0]]
            if prec < min_prec:
                return left
            self.next()
            right = self.expression(prec if right_assoc else prec + 1)
            left = ('call', self.backend.binop(t[0], t[2]), [left, right], t[2])

    def atom(self):
        t = self.next()
        if t[0] == 'num':
            return ('num', self.backend.literal(t[1], t[2]))
        if t[0] == '-':
            operand = self.expression(UNARY_PRECEDENCE)
            if operand[0] == 'num':
                return ('num', -operand[1])
            return self.backend.negate(operand, t[2])
        if t[0] == '(':
            inner = self.expression(0)
            self.expect(')')
            return inner
        if t[0] == 'ident':
            if self.peek()[0] == '(':
                op = self.backend.functions.get(t[1])
                if op is None:
                    raise ExprError(
                        f"unknown function '{t[1]}' in {self.backend.name} "
                        f"expressions; available: "
                        f"{', '.join(sorted(self.backend.functions))}", t[2])
                self.next()
                args = []
                if self.peek()[0] != ')':
                    args.append(self.expression(0))
                    while self.peek()[0] == ',':
                        self.next()
                        args.append(self.expression(0))
                self.expect(')')
                if len(args) != self.backend.arity[op]:
                    raise ExprError(
                        f"{t[1]}() takes {self.backend.arity[op]} argument(s), "
                        f"got {len(args)}", t[2])
                return ('call', op, args, t[2])
            return ('var', t[1], t[2])
        raise ExprError(f"unexpected '{t[1] or 'end'}'", t[2])


# ---------------------------------------------------------------------------
# Folding and emission
# ---------------------------------------------------------------------------

def _fold(ast, backend):
    if ast[0] != 'call':
        return ast
    args = [_fold(a, backend) for a in ast[2]]
    impl = backend.pyfold.get(ast[1])
    if impl and all(a[0] == 'num' for a in args):
        try:
            value = impl(*[a[1] for a in args])
        except (ValueError, ZeroDivisionError, OverflowError):
            value = None
        if value is not None:
            return ('num', backend.fold_result(value, ast[3]))
    return ('call', ast[1], args, ast[3])


def _free_vars(ast, out):
    if ast[0] == 'var':
        out.setdefault(ast[1], ast[2])
    elif ast[0] == 'call':
        for a in ast[2]:
            _free_vars(a, out)


def _key(ast):
    if ast[0] == 'num':
        return ('num', ast[1])
    if ast[0] == 'var':
        return ('var', ast[1])
    return ('call', ast[1], tuple(_key(a) for a in ast[2]))


def compile_expression(name: str, formula: str, bindings: dict,
                       location=(0.0, 0.0), dtype: str = 'float'):
    """Lower a formula to (nodes, links). `bindings` maps identifier ->
    SocketRef (connection) or literal. Nodes comprise the Math nodes, the
    final one named `name`, and a frame labelled with the formula."""
    backend = BACKENDS[dtype]
    ast = _Parser(_lex(formula), backend).parse()

    # Binding checks run on the raw tree, before substitution erases vars.
    free: dict = {}
    _free_vars(ast, free)
    missing = sorted(set(free) - set(bindings) - set(backend.constants))
    if missing:
        raise ExprError(f"unbound identifier(s): {', '.join(missing)}",
                        min(free[m] for m in missing))
    unused = sorted(set(bindings) - set(free))
    if unused:
        raise ExprError(f"binding(s) never used: {', '.join(unused)}", 0)

    # Names become numbers where they can: literal bindings fold through
    # like any constant; connections stay as vars. Unbound names resolve to
    # the named constants (a binding shadows its constant).
    def substitute(node):
        if node[0] == 'var':
            if node[1] in bindings:
                if not isinstance(bindings[node[1]], SocketRef):
                    return ('num', backend.literal(bindings[node[1]], node[2]))
            elif node[1] in backend.constants:
                return ('num', backend.constants[node[1]])
        if node[0] == 'call':
            return ('call', node[1], [substitute(a) for a in node[2]], node[3])
        return node

    ast = _fold(substitute(ast), backend)

    nodes: list[NodeDef] = []
    links: list[LinkDef] = []

    if ast[0] != 'call':
        # Whole formula folded to a constant (or is a lone identifier).
        if ast[0] == 'num':
            nodes.append(backend.const_node(name, ast[1]))
        else:
            binding = bindings[ast[1]]
            if isinstance(binding, SocketRef):
                raise ExprError("formula is a bare identifier; reference the "
                                "source directly instead", ast[2])
            nodes.append(backend.const_node(
                name, backend.literal(binding, ast[2])))
    else:
        emitted: dict = {}   # cse key -> (node name, depth)
        counter = [0]

        def emit(node_ast, is_root) -> tuple[str, int]:
            key = _key(node_ast)
            if not is_root and key in emitted:
                return emitted[key]
            if is_root:
                node_name = name
            else:
                counter[0] += 1
                node_name = f"{name}.{counter[0]}"
            op = node_ast[1]
            bl_idname, sockets, socket_names = backend.ops[op]
            node = NodeDef(name=node_name, bl_idname=bl_idname,
                           properties={'operation': op}
                           if op != _DEFAULT_OP[bl_idname] else {})
            depth = 0
            for pos, arg in enumerate(node_ast[2]):
                idx = sockets[pos]
                if arg[0] == 'num':
                    node.input_values[idx] = arg[1]
                elif arg[0] == 'var':
                    binding = bindings[arg[1]]
                    if isinstance(binding, SocketRef):
                        links.append(LinkDef(
                            source=binding,
                            target=SocketRef(node_name, idx, socket_names[pos])))
                    else:
                        node.input_values[idx] = backend.literal(binding, node_ast[3])
                    node.input_names[idx] = socket_names[pos]
                else:
                    child_name, child_depth = emit(arg, False)
                    depth = max(depth, child_depth + 1)
                    links.append(LinkDef(
                        source=SocketRef(child_name, 0, "Value"),
                        target=SocketRef(node_name, idx, socket_names[pos])))
            nodes.append(node)
            emitted[key] = (node_name, depth)
            return node_name, depth

        _, max_depth = emit(ast, True)

        # Deterministic layout inside the frame: columns by depth, rows in
        # emission order within a column. Frame-relative coordinates.
        depth_of = {n: d for n, d in emitted.values()}
        rows: dict[int, int] = {}
        for node in nodes:
            d = depth_of.get(node.name, 0)
            row = rows.get(d, 0)
            rows[d] = row + 1
            node.location = (float((d - max_depth) * 180), float(-row * 130))

    frame = NodeDef(name=f"{name}.expr", bl_idname='NodeFrame',
                    label=formula, location=location,
                    children=[n.name for n in nodes])
    nodes.append(frame)
    return nodes, links
