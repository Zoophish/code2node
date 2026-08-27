# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Sam Warren
import re

from .core import NodeIOError


class PreprocessError(NodeIOError):
    def __init__(self, message: str, pos: int = 0):
        super().__init__(f"pos {pos}: {message}")
        self.pos = pos


class _State:
    def __init__(self):
        self.defines: dict[str, str] = {}
        self.pattern = None

    def substitute(self, line: str) -> str:
        if self.pattern is None:
            return line
        return self.pattern.sub(lambda m: self.defines[m.group(0)], line)


_DEFINE_RE = re.compile(r'([A-Za-z_]\w*)\s+(\S.*?)\s*$')


def _d_define(state: _State, arg: str, pos: int):
    m = _DEFINE_RE.match(arg)
    if m is None:
        raise PreprocessError("expected: #define NAME value", pos)
    name, value = m.groups()
    if name in state.defines:
        raise PreprocessError(f"'{name}' is already defined", pos)
    state.defines[name] = state.substitute(value)
    state.pattern = re.compile(
        r'\b(' + '|'.join(re.escape(n) for n in state.defines) + r')\b')


# directive keyword -> handler(state, argument text, file position)
DIRECTIVES = {
    'define': _d_define,
}

_DIRECTIVE_RE = re.compile(r'#([A-Za-z_]\w*)\s*(.*)$')


def preprocess(text: str) -> str:
    state = _State()
    out = []
    pos = 0
    for line in text.split('\n'):
        if line.lstrip().startswith('#'):
            m = _DIRECTIVE_RE.match(line.lstrip())
            handler = DIRECTIVES.get(m.group(1)) if m else None
            if handler is None:
                raise PreprocessError(
                    f"unknown directive; available: "
                    f"{', '.join(sorted(DIRECTIVES))}", pos)
            handler(state, m.group(2), pos)
            line = ''
        else:
            line = state.substitute(line)
        out.append(line)
        pos += len(line) + 1
    return '\n'.join(out)
