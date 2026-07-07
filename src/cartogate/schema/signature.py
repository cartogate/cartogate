"""Signature normalization for duplicate detection (spec §7.1 ``check_duplicate``).

Both stored symbol signatures and incoming ``check_duplicate`` queries pass through
``normalize_signature`` so the duplicate gate compares like with like. The canonical
form is ``name(p1,p2,...)`` — the callable name plus its parameters: by **name** for
Python/TS (``name: Type`` → the name), but by erased **type** for Java (``Type name`` →
the type), since Java overloads are distinguished by parameter type. Annotations, defaults,
receivers (``self``/``cls``), generics, and positional/keyword markers are stripped.

Normalization is **per-language** (the gate keys on the language too), so a Python
``def add(a, b)`` and a TypeScript ``function add(a, b)`` both canonicalize to ``add(a,b)``
but are kept apart by the language dimension — they are not cross-language duplicates.

Parameter splitting is bracket-depth aware so commas inside annotations or defaults
(e.g. ``Dict[str, int]`` or ``[1, 2, 3]``) are not mistaken for parameter separators.
"""

from __future__ import annotations

import re

from cartogate.schema.enums import Language

# Angle brackets are tracked too so a comma inside generics (``Map<String,Integer>``, Java/TS)
# is not mistaken for a parameter separator.
_OPENERS = {"(": ")", "[": "]", "{": "}", "<": ">"}
_CLOSERS = {")", "]", "}", ">"}

#: Leading keyword prefixes stripped (repeatedly) to reach ``name(params)``.
_LEADING: dict[Language, tuple[str, ...]] = {
    Language.PYTHON: ("async def ", "def ", "class "),
    Language.TYPESCRIPT: (
        "export default ", "export ", "declare ", "abstract ", "async ", "function ",
        "class ", "interface ", "public ", "private ", "protected ", "static ", "readonly ",
        "const ", "let ", "var ", "get ", "set ",
    ),
    Language.JAVA: (
        "public ", "private ", "protected ", "static ", "final ", "abstract ", "default ",
        "synchronized ", "native ", "strictfp ", "transient ", "volatile ",
        "class ", "interface ", "enum ", "record ",
    ),
    # Go params are name-first (``a int``), so the default leading-name extraction applies; only
    # the declaration keywords need stripping. A method's receiver is dropped by the walker.
    Language.GO: ("func ", "type "),
    # Rust params are name-first (``a: i32``); strip the declaration keywords + visibility.
    Language.RUST: (
        "pub(crate) ", "pub(super) ", "pub(self) ", "pub ", "async ", "unsafe ", "const ",
        "extern ", "default ", "fn ", "struct ", "enum ", "trait ", "union ", "type ", "impl ",
    ),
    # JavaScript is TypeScript minus the type-only keywords; params are name-first like TS.
    Language.JAVASCRIPT: (
        "export default ", "export ", "async ", "function ", "class ",
        "static ", "const ", "let ", "var ", "get ", "set ",
    ),
    # C# is ``Type name`` like Java; strip access/decl modifiers. (Walker-built signatures are
    # already ``name(params)``, but the snippet path may carry modifiers — strip them defensively.)
    # The while-loop strip means multi-word combos (``protected internal``) fall out word-by-word.
    Language.CSHARP: (
        "public ", "private ", "protected ", "internal ", "static ", "abstract ", "sealed ",
        "virtual ", "override ", "readonly ", "async ", "partial ", "extern ", "unsafe ", "new ",
        "class ", "interface ", "struct ", "enum ", "record ", "delegate ",
    ),
    # C is ``Type name`` like Java; the walker emits ``name(params)`` directly, so this only strips
    # storage/decl keywords that may lead a raw signature on the snippet path. C has no overloading.
    Language.C: (
        "static ", "extern ", "inline ", "register ", "auto ", "const ", "volatile ",
        "typedef ", "struct ", "union ", "enum ", "unsigned ", "signed ",
    ),
    # C++ adds class/namespace decl keywords + method modifiers to C's set. The walker emits
    # ``name(params)``; these strip a raw snippet-path signature to the bare callable/type name.
    Language.CPP: (
        "static ", "extern ", "inline ", "virtual ", "explicit ", "friend ", "constexpr ",
        "const ", "volatile ", "typedef ", "struct ", "union ", "enum ", "class ", "namespace ",
        "template ", "unsigned ", "signed ",
    ),
    # Kotlin is name-first (``name: Type``) like TS; strip visibility/decl/modifier keywords.
    Language.KOTLIN: (
        "public ", "private ", "protected ", "internal ", "open ", "abstract ", "final ",
        "sealed ", "data ", "inner ", "override ", "suspend ", "inline ", "operator ", "infix ",
        "fun ", "class ", "object ", "interface ", "enum ", "val ", "var ", "const ", "companion ",
    ),
    # Swift is name-first (``label name: Type``) like TS/Kotlin; strip visibility/decl modifiers.
    Language.SWIFT: (
        "public ", "private ", "fileprivate ", "internal ", "open ", "final ", "static ",
        "class ", "override ", "convenience ", "required ", "lazy ", "mutating ", "func ",
        "struct ", "enum ", "protocol ", "extension ", "let ", "var ", "init ",
    ),
}
#: Receiver params dropped so a method and a free function with the same shape match.
_SKIP_PARAMS: dict[Language, frozenset[str]] = {
    Language.PYTHON: frozenset({"self", "cls"}),
    Language.TYPESCRIPT: frozenset(),
    Language.JAVA: frozenset(),
    Language.GO: frozenset(),
    Language.RUST: frozenset({"self"}),  # receiver: self / &self / &mut self / mut self
    Language.JAVASCRIPT: frozenset(),  # no receiver param (like TS)
    Language.CSHARP: frozenset(),  # no receiver param (instance methods bind via the resolver)
    Language.C: frozenset(),  # no receiver param (free functions only)
    Language.CPP: frozenset(),  # methods bind via the resolver; no explicit receiver param
    Language.KOTLIN: frozenset(),  # methods bind via the resolver; params are name-first
    Language.SWIFT: frozenset(),  # methods bind via the resolver; params are name-first
}
#: Languages written ``Type name`` (vs Python/TS ``name: Type``), so the *callable* name is the
#: trailing identifier before the paren. (Java's *parameter* key is the type — see ``_param_name``.)
_TYPE_BEFORE_NAME: frozenset[Language] = frozenset({Language.JAVA})


def _strip_leading(text: str, language: Language) -> str:
    keywords = _LEADING[language]
    changed = True
    while changed:
        changed = False
        for keyword in keywords:
            if text.startswith(keyword):
                text = text[len(keyword) :].lstrip()
                changed = True
    return text


def _split_top_level(params: str) -> list[str]:
    """Split a parameter string on commas that sit at bracket depth zero."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in params:
        if char in _OPENERS:
            depth += 1
            current.append(char)
        elif char in _CLOSERS:
            depth = max(0, depth - 1)
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _param_name(param: str, language: Language) -> str | None:
    """Extract a parameter's canonical key, or ``None`` for ``/``, ``*`` and empty markers.

    Java keys on the parameter **type** (overloads are distinguished by erased type, not name).
    For Python/TS the name leads (``name: Type``) and the name is the key. A lone type token (a
    class base) yields that token, which is what the class-signature normalization wants.
    """
    if language is Language.JAVA:
        return _java_param_type(param)
    # Drop * / ** / ... (Python) and & (Rust refs) prefixes, then a leading ``mut `` (Rust), so a
    # receiver like ``&mut self`` reduces to ``self`` and is dropped by the per-language skip set.
    token = param.strip().lstrip("*.&")
    if token.startswith("mut "):
        token = token[4:].lstrip()
    if not token or token == "/":
        return None
    # Name ends at the first annotation (:), default (=), optional (?), or whitespace.
    name_chars: list[str] = []
    for char in token:
        if char in ":=? \t":
            break
        name_chars.append(char)
    name = "".join(name_chars)
    return name or None


#: Java parameter modifiers dropped before the type/name split.
_JAVA_MODIFIERS = frozenset({"final"})


def _strip_angle_brackets(text: str) -> str:
    """Drop balanced ``<...>`` generic arguments (Java erases them — you can't overload on them)."""
    out: list[str] = []
    depth = 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _simple_type(type_str: str) -> str:
    """A Java type in its overload-significant form: simple name + array brackets.

    ``java.util.List`` -> ``List`` (package qualifier stripped); ``Foo[][]`` keeps its brackets.
    """
    brackets = ""
    t = type_str
    while t.endswith("[]"):
        brackets += "[]"
        t = t[:-2]
    return t.rsplit(".", 1)[-1] + brackets  # strip package qualifier, restore array brackets


def _java_param_type(param: str) -> str | None:
    """The overload-significant **type** of a Java ``Type name`` parameter.

    Java overloads resolve on erased parameter type, so this keeps the type and discards the name:
    annotations and ``final`` are stripped, generics erased, ``T...`` varargs normalize to ``T[]``,
    a package qualifier reduces to the simple name, and array ``[]`` is preserved.
    """
    s = re.sub(r"@\w[\w.]*(\s*\([^)]*\))?", " ", param)  # strip annotations (@NonNull, @Foo(...))
    s = _strip_angle_brackets(s).replace("...", "[]").strip()  # erase generics; varargs -> array
    tokens = [t for t in s.split() if t not in _JAVA_MODIFIERS]
    if not tokens or tokens[0] == "/":
        return None
    if len(tokens) == 1:
        return _simple_type(tokens[0])  # a lone type token (no name) — keep it
    name, type_tokens = tokens[-1], tokens[:-1]
    brackets = ""
    while name.endswith("[]"):  # C-style array on the name (`int xs[]`) belongs to the type
        brackets += "[]"
        name = name[:-2]
    return _simple_type("".join(type_tokens) + brackets)


def normalize_signature(raw: str, language: Language = Language.PYTHON) -> str:
    """Return the canonical ``name(p1,p2,...)`` key for a callable/class signature.

    A bare name with no parameter list normalizes to just the name (arity is unknown, so it is
    not assumed to be zero-arg). ``language`` selects the keyword/receiver rules.
    """
    text = _strip_leading(raw.strip().rstrip(":").strip(), language)

    open_idx = text.find("(")
    if open_idx == -1:
        return _clean_name(text)

    # In ``Type name(...)`` languages the callable name is the trailing identifier before the
    # paren (a return type / generics precede it); elsewhere the name leads.
    pre = text[:open_idx]
    name = _trailing_callable_name(pre) if language in _TYPE_BEFORE_NAME else _clean_name(pre)

    # Find the matching close paren for the first open paren (depth-aware).
    depth = 0
    close_idx = -1
    for i in range(open_idx, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth = max(0, depth - 1)
            if depth == 0:
                close_idx = i
                break
    params_str = text[open_idx + 1 : close_idx] if close_idx != -1 else text[open_idx + 1 :]

    skip = _SKIP_PARAMS[language]
    names: list[str] = []
    for part in _split_top_level(params_str):
        param_name = _param_name(part, language)
        if param_name and param_name not in skip:
            names.append(param_name)

    return f"{name}({','.join(names)})"


def _trailing_callable_name(pre: str) -> str:
    """The callable name in ``Type name`` form: the last identifier token before the paren."""
    tokens = re.findall(r"[A-Za-z_$][\w$]*", pre)
    return tokens[-1] if tokens else _clean_name(pre)


def _clean_name(raw_name: str) -> str:
    """Strip generics, a trailing ``=`` (arrow-fn assignment), and any body/whitespace tail."""
    name = raw_name.strip()
    for cut in ("<", "{", " ", "\t"):  # drop generics / a `{` body / trailing tokens
        if cut in name:
            name = name[: name.index(cut)]
    return name.rstrip("=").strip()
