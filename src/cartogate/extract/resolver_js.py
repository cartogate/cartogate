"""Pure-Python JavaScript name resolver (F-08).

JavaScript reuses :class:`~cartogate.extract.resolver_ts.TypeScriptResolver` wholesale — the
scope/shadow guard, the top-level symbol table, and ESM import binding are language-neutral. Three
class attributes adapt it for JS: the ``tsx`` grammar (a JS superset that also parses JSX), the
JS import-path extensions (``.js``/``.jsx``/``.mjs``/``.cjs``), and a CommonJS pass that binds
``const x = require('./y')`` so Node call graphs resolve.

Honest ceiling (returns ``None`` → no edge, never a *wrong* edge): ``module.exports`` re-export
following, dynamic ``require(variable)``, inferred-type receiver method calls (``x.m()``), and
member-expression JSX components (``<Foo.Bar/>``).
"""

from __future__ import annotations

import tree_sitter_typescript as tstypescript
from tree_sitter import Language

from cartogate.extract.resolver_ts import TypeScriptResolver

#: The ``tsx`` grammar — JavaScript + JSX (same grammar the JS walker uses).
_JS_LANGUAGE = Language(tstypescript.language_tsx())


class JavaScriptResolver(TypeScriptResolver):
    """Resolves JavaScript name occurrences (ESM + CommonJS) to their definitions."""

    _LANGUAGE = _JS_LANGUAGE
    _MODULE_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs")
    _COMMONJS = True
