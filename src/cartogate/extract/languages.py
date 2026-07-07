"""Language registry — the dispatch seam for multi-language extraction (FUTURE F-08).

Each :class:`LanguageSpec` says how to recognise a language's files, walk them into the shared
``FileFacts``, derive a module qualified name from a path, and whether a name resolver exists.
The pipeline iterates files, looks up the spec by suffix, and runs the structural pass per
language; the schema, store, and gate stay language-neutral.

v0 had one hard-coded language; this keeps that behaviour for Python and adds TypeScript (its
own walker + a pure-Python resolver). Adding Java later is just another spec.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cartogate.extract.ast_walker import (
    FileFacts,
    RawSymbol,
    TreeSitterWalker,
    extract_signatures,
)
from cartogate.extract.c_walker import CWalker
from cartogate.extract.cpp_walker import CppWalker
from cartogate.extract.csharp_walker import CSharpWalker
from cartogate.extract.go_walker import GoWalker
from cartogate.extract.java_walker import JavaWalker
from cartogate.extract.js_walker import JavaScriptWalker
from cartogate.extract.kotlin_walker import KotlinWalker
from cartogate.extract.resolver import JediResolver, NameResolver
from cartogate.extract.resolver_c import CResolver
from cartogate.extract.resolver_cpp import CppResolver
from cartogate.extract.resolver_csharp import CSharpResolver
from cartogate.extract.resolver_go import GoResolver
from cartogate.extract.resolver_java import JavaResolver
from cartogate.extract.resolver_js import JavaScriptResolver
from cartogate.extract.resolver_kotlin import KotlinResolver
from cartogate.extract.resolver_rust import RustResolver
from cartogate.extract.resolver_swift import SwiftResolver
from cartogate.extract.resolver_ts import TypeScriptResolver
from cartogate.extract.rust_walker import RustWalker
from cartogate.extract.swift_walker import SwiftWalker
from cartogate.extract.ts_walker import TypeScriptWalker
from cartogate.schema.enums import Language


class _Walker(Protocol):
    """Structural pass: ``walk(source, *, module_qname, rel_path, abs_path) -> FileFacts``."""

    def walk(
        self, source: bytes, *, module_qname: str, rel_path: str, abs_path: str
    ) -> FileFacts: ...


#: Build a language's name resolver over its sources (abs path -> text); ``None`` = structural-only.
MakeResolver = Callable[[Path, dict[str, str]], NameResolver]


@dataclass(frozen=True)
class LanguageSpec:
    """How to extract one language."""

    language: Language
    suffixes: tuple[str, ...]  # file extensions, longest-first for matching
    index_stems: tuple[str, ...]  # filenames (no extension) collapsed to their dir in the qname
    make_walker: Callable[[], _Walker]
    make_resolver: MakeResolver | None  # name resolver factory; None = structural only
    #: Whether a file is its own module namespace (Python/TS: ``mod.py`` -> module ``mod``).
    #: Java is False: the file name is the class, so the *directory* is the module (= package),
    #: and several files share one package module (see the pipeline's module-ownership handling).
    file_is_namespace: bool = True
    #: A crate/root module name prepended to every module qname (Rust ``crate``), so a file at the
    #: source root has a non-empty qname and ``crate::`` paths line up. ``""`` = no prefix.
    root_module: str = ""

    @property
    def resolves(self) -> bool:
        return self.make_resolver is not None


LANGUAGES: dict[Language, LanguageSpec] = {
    Language.PYTHON: LanguageSpec(
        language=Language.PYTHON,
        suffixes=(".py",),
        index_stems=("__init__",),
        make_walker=TreeSitterWalker,
        make_resolver=JediResolver,
    ),
    Language.TYPESCRIPT: LanguageSpec(
        language=Language.TYPESCRIPT,
        suffixes=(".tsx", ".ts"),  # .tsx before .ts so it wins the suffix match
        index_stems=("index",),
        make_walker=TypeScriptWalker,
        make_resolver=TypeScriptResolver,
    ),
    Language.JAVA: LanguageSpec(
        language=Language.JAVA,
        suffixes=(".java",),
        index_stems=(),  # no barrel/entry file; the directory is the package
        make_walker=JavaWalker,
        make_resolver=JavaResolver,
        file_is_namespace=False,  # file = class; the package (directory) is the module
    ),
    Language.GO: LanguageSpec(
        language=Language.GO,
        suffixes=(".go",),
        index_stems=(),
        make_walker=GoWalker,
        make_resolver=GoResolver,
        file_is_namespace=False,  # the directory is the package; many files share it
    ),
    Language.RUST: LanguageSpec(
        language=Language.RUST,
        suffixes=(".rs",),
        index_stems=("mod", "lib", "main"),  # mod.rs/lib.rs/main.rs collapse to their dir
        make_walker=RustWalker,
        make_resolver=RustResolver,
        # A Rust file *is* a module (like Python/TS) — point Cartogate at the crate's src root.
        root_module="crate",  # crate root file -> ``crate``; ``crate::x`` paths line up
    ),
    Language.JAVASCRIPT: LanguageSpec(
        language=Language.JAVASCRIPT,
        # JS reuses the TS walker/resolver via the ``tsx`` grammar (a JS superset that parses JSX).
        suffixes=(".jsx", ".mjs", ".cjs", ".js"),  # none is a suffix of another → order is safe
        index_stems=("index",),  # Node resolves a directory to its index.js
        make_walker=JavaScriptWalker,
        make_resolver=JavaScriptResolver,
    ),
    Language.CSHARP: LanguageSpec(
        language=Language.CSHARP,
        suffixes=(".cs",),
        index_stems=(),
        make_walker=CSharpWalker,
        make_resolver=CSharpResolver,
        # A ``.cs`` file is its own module (qnames are file-based); the resolver reads the in-file
        # ``namespace`` + ``using`` directives to bind cross-namespace references.
    ),
    Language.C: LanguageSpec(
        language=Language.C,
        # ``.h`` is parsed as C (the C/C++ header ambiguity defaults to C; C++ owns ``.hpp`` etc.).
        # A ``.c``/``.h`` file is its own module.
        suffixes=(".c", ".h"),
        index_stems=(),
        make_walker=CWalker,
        make_resolver=CResolver,
    ),
    Language.CPP: LanguageSpec(
        language=Language.CPP,
        # C++ owns the non-ambiguous extensions; ``.h`` stays C. None is a string-suffix of another.
        suffixes=(".cpp", ".cxx", ".cc", ".hpp", ".hxx", ".hh"),
        index_stems=(),
        make_walker=CppWalker,
        make_resolver=CppResolver,
    ),
    Language.KOTLIN: LanguageSpec(
        language=Language.KOTLIN,
        suffixes=(".kt", ".kts"),
        index_stems=(),
        make_walker=KotlinWalker,
        make_resolver=KotlinResolver,
        # A ``.kt`` file is its own module (qnames file-based); the resolver reads the in-file
        # ``package`` + ``import`` directives to bind cross-package references.
    ),
    Language.SWIFT: LanguageSpec(
        language=Language.SWIFT,
        suffixes=(".swift",),
        index_stems=(),
        make_walker=SwiftWalker,
        make_resolver=SwiftResolver,
        # Swift has a flat module namespace: a ``.swift`` file is its own module (qnames file-based)
        # and the resolver indexes types/functions repo-wide by name (no in-source package).
    ),
}

#: Lookup a language by file suffix (e.g. ``.ts`` -> TYPESCRIPT).
SUFFIX_TO_LANGUAGE: dict[str, Language] = {
    suffix: spec.language for spec in LANGUAGES.values() for suffix in spec.suffixes
}

#: Every indexable suffix across all languages.
SOURCE_SUFFIXES: tuple[str, ...] = tuple(SUFFIX_TO_LANGUAGE)


def language_of(path: str) -> Language | None:
    """The language for a file path by its suffix, or ``None`` if not an indexable source."""
    for suffix in SOURCE_SUFFIXES:
        if path.endswith(suffix):
            return SUFFIX_TO_LANGUAGE[suffix]
    return None


def module_qname(rel_path: str, spec: LanguageSpec) -> str:
    """Derive a dotted module name from a POSIX relative path for ``spec``'s language.

    Strips the language suffix and collapses an index filename (Python ``__init__`` / TS
    ``index``) to its directory, mirroring how each ecosystem names a package's entry module.
    """
    parts = rel_path.split("/")
    for suffix in spec.suffixes:
        if parts[-1].endswith(suffix):
            parts[-1] = parts[-1][: -len(suffix)]
            break
    if not spec.file_is_namespace:
        # The file name is the type, not a module — the module is its directory (the package).
        parts = parts[:-1]
    elif parts and parts[-1] in spec.index_stems:
        parts = parts[:-1]
    # A crate-root prefix (Rust ``crate``) keeps the root file's qname non-empty and aligns with
    # ``crate::`` paths; it is dropped for empty segments so ``lib.rs`` -> just ``crate``.
    if spec.root_module:
        parts = [spec.root_module, *parts]
    return ".".join(p for p in parts if p)


def symbol_facts_in(source: str, language: Language = Language.PYTHON) -> list[RawSymbol]:
    """The full ``RawSymbol`` facts for every symbol defined in a snippet.

    Qualified names are rooted at the ``<snippet>`` pseudo-module, so two versions of the SAME
    file walked this way yield directly comparable keys. This is the gate surfaces' view of a
    proposed edit: name, signature, body hash, and type-decl-ness in one walk.
    """
    if language is Language.PYTHON:
        from cartogate.extract.ast_walker import TreeSitterWalker

        walker: object = TreeSitterWalker()
    else:
        walker = LANGUAGES[language].make_walker()
    facts = walker.walk(  # type: ignore[attr-defined]
        source.encode("utf-8"),
        module_qname="<snippet>",
        rel_path="<snippet>",
        abs_path="<snippet>",
    )
    return list(facts.symbols)


def named_signatures_in(
    source: str, language: Language = Language.PYTHON
) -> list[tuple[str, str]]:
    """``(qualified_name, raw signature)`` pairs for a snippet (see :func:`symbol_facts_in`)."""
    return [(sym.qualified_name, sym.signature) for sym in symbol_facts_in(source, language)]


def signatures_in(source: str, language: Language = Language.PYTHON) -> list[str]:
    """Raw signatures of every symbol defined in a snippet, for the gate surfaces.

    Python uses the existing ``extract_signatures``; other languages walk via their spec.
    """
    if language is Language.PYTHON:
        return extract_signatures(source)
    facts = LANGUAGES[language].make_walker().walk(
        source.encode("utf-8"),
        module_qname="<snippet>",
        rel_path="<snippet>",
        abs_path="<snippet>",
    )
    return [sym.signature for sym in facts.symbols]
