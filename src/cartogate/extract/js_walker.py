"""Tree-sitter AST walk for JavaScript → raw structural facts (F-08).

JavaScript reuses the TypeScript walker wholesale: the ``tsx`` grammar from
``tree-sitter-typescript`` is a superset of JavaScript that *also* parses JSX (``language_tsx``),
whereas the plain ``typescript`` grammar errors on JSX. So pointing :class:`TypeScriptWalker` at
the ``tsx`` grammar gives JS functions, arrow consts, classes, methods, ESM imports, calls, and
``extends`` heritage for free, plus the shared walker's JSX branch (``<Foo/>`` component
references). No JS-specific traversal is needed here — only the grammar swap.
"""

from __future__ import annotations

import tree_sitter_typescript as tstypescript
from tree_sitter import Language

from cartogate.extract.ts_walker import TypeScriptWalker

#: The ``tsx`` grammar — JavaScript + JSX. Constructed once and shared by all JS walkers.
_JS_LANGUAGE = Language(tstypescript.language_tsx())


class JavaScriptWalker(TypeScriptWalker):
    """Walks JavaScript (incl. JSX) source into ``FileFacts`` via the ``tsx`` grammar."""

    def __init__(self) -> None:
        super().__init__(_JS_LANGUAGE)
