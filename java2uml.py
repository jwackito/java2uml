#!/usr/bin/env python3
"""
java2uml - generate Mermaid class diagrams from Java sources.

The tool is intentionally standalone: it only needs `javalang` (Java 8 syntax)
and does not compile the project or resolve a classpath.

See README.md for usage and the output contract.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

try:
    import javalang
    from javalang import tree as jtree
except ImportError:  # pragma: no cover - handled in main()
    javalang = None
    jtree = None

VERSION = "1.0.0"
SCHEMA_VERSION = 1

PRIMITIVES = {
    "boolean", "byte", "char", "short", "int", "long", "float", "double", "void",
}
JDK_PREFIXES = (
    "java.", "javax.", "jdk.", "sun.", "com.sun.java.", "org.w3c.", "org.xml.",
)
JDK_LANG_SIMPLE = {
    "String", "Object", "Integer", "Long", "Double", "Float", "Boolean",
    "Byte", "Short", "Character", "Number", "Void", "Class", "Enum",
    "Throwable", "Exception", "RuntimeException", "Error", "Iterable",
    "Comparable", "CharSequence", "StringBuilder", "StringBuffer",
    "Runnable", "Thread", "Cloneable", "Comparator",
}
COLLECTIONS = {
    "Iterable", "Collection", "List", "ArrayList", "LinkedList", "Vector",
    "Stack", "Set", "HashSet", "LinkedHashSet", "TreeSet", "SortedSet",
    "NavigableSet", "EnumSet", "Queue", "Deque", "ArrayDeque", "PriorityQueue",
    "Map", "HashMap", "LinkedHashMap", "TreeMap", "SortedMap", "NavigableMap",
    "Hashtable", "Properties", "EnumMap", "WeakHashMap", "IdentityHashMap",
    "ConcurrentHashMap", "ConcurrentMap", "BlockingQueue", "BlockingDeque",
    "Optional", "AtomicReference",
}
VISIBILITY = {"public": "+", "private": "-", "protected": "#", "package": "~"}

TOKEN_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\s*\.\s*[A-Za-z_$][A-Za-z0-9_$]*)*")
TYPE_KEYWORDS = {
    "extends", "super", "final", "static", "public", "private", "protected",
    "abstract", "synchronized", "native", "transient", "volatile", "strictfp",
    "default",
}

DEFAULT_EXCLUDES = [
    ".git/**", "**/.git/**", "**/target/**", "**/build/**", "**/out/**",
    "**/node_modules/**", "**/__pycache__/**", "**/.gradle/**", "**/.idea/**",
]


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def utc_now() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        try:
            return _dt.datetime.fromtimestamp(int(epoch), _dt.timezone.utc).isoformat(
                timespec="seconds").replace("+00:00", "Z")
        except (TypeError, ValueError, OSError):
            pass
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sanitize_segment(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return value.strip("._") or "_"


def sanitize_path(value: str) -> str:
    return "/".join(sanitize_segment(part) for part in value.replace("\\", "/").split("/") if part)


def posix_rel(path: str, start: str) -> str:
    return os.path.relpath(path, start).replace(os.sep, "/")


def mermaid_id(repo: str, module: str, fqn: str) -> str:
    raw = "%s__%s__%s" % (repo, module or "_root", fqn)
    return "c_" + re.sub(r"[^A-Za-z0-9_]", "_", raw)


def visibility(modifiers) -> str:
    modifiers = set(modifiers or ())
    if "public" in modifiers:
        return "public"
    if "protected" in modifiers:
        return "protected"
    if "private" in modifiers:
        return "private"
    return "package"


def git(args, cwd: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", cwd] + args,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=30, check=False,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def repo_identity(root: str, name_override=None):
    """Return (repo_root, info) for a source root. info is None when not git."""
    top = git(["rev-parse", "--show-toplevel"], root)
    if not top:
        return None, None
    top = os.path.abspath(top)
    remote = git(["remote", "get-url", "origin"], top)
    if not remote:
        remotes = git(["remote"], top).splitlines()
        remote = git(["remote", "get-url", remotes[0]], top) if remotes else ""
    name = name_override or ""
    if not name and remote:
        name = re.sub(r"\.git$", "", remote.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1])
    if not name:
        name = os.path.basename(top)
    describe = git(["describe", "--tags", "--always", "--dirty"], top) or None
    tracked_dirty = git(["status", "--porcelain", "--untracked-files=no"], top)
    untracked = git(["status", "--porcelain", "--untracked-files=all"], top)
    untracked_count = len([line for line in untracked.splitlines() if line.startswith("??")])
    return top, {
        "name": sanitize_segment(name),
        "root": top,
        "remote": remote,
        "commit": git(["rev-parse", "HEAD"], top) or None,
        "branch": git(["rev-parse", "--abbrev-ref", "HEAD"], top) or None,
        "describe": describe,
        "tags": [t for t in git(["tag", "--points-at", "HEAD"], top).splitlines() if t],
        "dirty": bool(tracked_dirty) or bool(describe and describe.endswith("-dirty")),
        "modified_files": len([line for line in tracked_dirty.splitlines() if line.strip()]),
        "untracked_files": untracked_count,
    }


# ---------------------------------------------------------------------------
# Java AST extraction
# ---------------------------------------------------------------------------

def reference_name(node):
    """Return (dotted name, type arguments, dimensions) for a ReferenceType.

    javalang stores qualified names as a chain of `sub_type` nodes, e.g.
    java.util.Map.Entry becomes java -> util -> Map -> Entry.
    """
    parts = [node.name]
    arguments = getattr(node, "arguments", None)
    dimensions = getattr(node, "dimensions", None)
    sub = getattr(node, "sub_type", None)
    while sub is not None:
        parts.append(sub.name)
        if getattr(sub, "arguments", None):
            arguments = sub.arguments
        if getattr(sub, "dimensions", None):
            dimensions = sub.dimensions
        sub = getattr(sub, "sub_type", None)
    return ".".join(parts), arguments or [], dimensions or []


def render_type(node):
    if node is None:
        return None
    if isinstance(node, jtree.BasicType):
        return node.name + "[]" * len(node.dimensions or [])
    if isinstance(node, jtree.ReferenceType):
        name, args, dimensions = reference_name(node)
        if args:
            rendered = []
            for arg in args:
                if arg.type is not None:
                    inner = render_type(arg.type)
                    if arg.pattern_type == "extends":
                        inner = "? extends " + inner
                    elif arg.pattern_type == "super":
                        inner = "? super " + inner
                    rendered.append(inner)
                else:
                    rendered.append("?")
            name += "<" + ",".join(rendered) + ">"
        return name + "[]" * len(dimensions)
    if isinstance(node, jtree.TypeParameter):
        return node.name
    return str(node)


def decl_kind(decl):
    if isinstance(decl, jtree.ClassDeclaration):
        return "class"
    if isinstance(decl, jtree.InterfaceDeclaration):
        return "interface"
    if isinstance(decl, jtree.EnumDeclaration):
        return "enum"
    if isinstance(decl, jtree.AnnotationDeclaration):
        return "annotation"
    return None


def collect_type(decl, package, outer_fqn, out):
    kind = decl_kind(decl)
    if kind is None:
        return
    name = decl.name
    fqn = (outer_fqn + "." + name) if outer_fqn else ((package + "." + name) if package else name)
    modifiers = set(getattr(decl, "modifiers", None) or ())
    type_parameters = []
    for tp in (getattr(decl, "type_parameters", None) or []):
        bounds = [render_type(b) for b in (getattr(tp, "extends", None) or [])]
        type_parameters.append({"name": tp.name, "bounds": bounds})

    extends = getattr(decl, "extends", None)
    if extends is None:
        extends_list = []
    elif isinstance(extends, list):
        extends_list = [render_type(x) for x in extends if x is not None]
    else:
        extends_list = [render_type(extends)]
    implements_list = [render_type(x) for x in (getattr(decl, "implements", None) or []) if x is not None]

    annotations = []
    for ann in (getattr(decl, "annotations", None) or []):
        ann_name = getattr(ann, "name", None)
        if ann_name:
            annotations.append(str(ann_name))

    record = {
        "name": name,
        "fqn": fqn,
        "outer": outer_fqn,
        "kind": kind,
        "visibility": visibility(modifiers),
        "abstract": "abstract" in modifiers,
        "modifiers": sorted(modifiers),
        "type_parameters": type_parameters,
        "extends": extends_list,
        "implements": implements_list,
        "annotations": annotations,
        "fields": [],
        "constructors": [],
        "methods": [],
        "enum_constants": [],
    }

    # Enum constants are not part of `body` for top-level enums, but nested
    # enums are represented as a list of parser tuples containing EnumBody.
    enum_constants = list(getattr(decl, "constants", None) or [])
    body = getattr(decl, "body", None) or []
    if kind == "enum":
        enum_body = None
        for entry in body:
            candidates = entry if isinstance(entry, tuple) else (entry,)
            for candidate in candidates:
                if isinstance(candidate, jtree.EnumBody):
                    enum_body = candidate
                    break
            if enum_body is not None:
                break
        if enum_body is not None:
            enum_constants = list(getattr(enum_body, "constants", None) or enum_constants)
            body = list(getattr(enum_body, "declarations", None) or [])
    for const in enum_constants:
        record["enum_constants"].append(getattr(const, "name", str(const)))

    default_member_visibility = "public" if kind in ("interface", "annotation") else "package"

    for item in body:
        if isinstance(item, (jtree.FieldDeclaration, jtree.ConstantDeclaration)):
            item_mods = set(getattr(item, "modifiers", None) or ())
            field_type = render_type(getattr(item, "type", None))
            vis = visibility(item_mods) if item_mods else default_member_visibility
            for declarator in (getattr(item, "declarators", None) or []):
                record["fields"].append({
                    "name": declarator.name,
                    "type": field_type,
                    "visibility": vis,
                    "static": "static" in item_mods or kind in ("interface", "annotation"),
                    "final": "final" in item_mods or kind in ("interface", "annotation"),
                    "transient": "transient" in item_mods,
                    "volatile": "volatile" in item_mods,
                })
        elif isinstance(item, jtree.MethodDeclaration):
            item_mods = set(getattr(item, "modifiers", None) or ())
            vis = visibility(item_mods) if item_mods else default_member_visibility
            params = []
            for param in (getattr(item, "parameters", None) or []):
                params.append({
                    "name": param.name,
                    "type": render_type(param.type),
                    "varargs": bool(getattr(param, "varargs", False)),
                })
            is_abstract = "abstract" in item_mods or (
                kind in ("interface", "annotation") and getattr(item, "body", None) is None
            )
            record["methods"].append({
                "name": item.name,
                "return_type": render_type(getattr(item, "return_type", None)),
                "params": params,
                "throws": [render_type(x) for x in (getattr(item, "throws", None) or [])],
                "visibility": vis,
                "static": "static" in item_mods,
                "abstract": is_abstract,
                "final": "final" in item_mods,
                "synchronized": "synchronized" in item_mods,
                "native": "native" in item_mods,
                "type_parameters": [tp.name for tp in (getattr(item, "type_parameters", None) or [])],
            })
        elif isinstance(item, jtree.ConstructorDeclaration):
            item_mods = set(getattr(item, "modifiers", None) or ())
            params = []
            for param in (getattr(item, "parameters", None) or []):
                params.append({
                    "name": param.name,
                    "type": render_type(param.type),
                    "varargs": bool(getattr(param, "varargs", False)),
                })
            record["constructors"].append({
                "name": item.name,
                "params": params,
                "throws": [render_type(x) for x in (getattr(item, "throws", None) or [])],
                "visibility": visibility(item_mods),
            })
        elif decl_kind(item) is not None:
            collect_type(item, package, fqn, out)

    out.append(record)


def extract_file(job):
    """Worker: parse one Java file and return a plain dict (picklable)."""
    absolute, rel_root, rel_repo, repo, module = job
    try:
        with open(absolute, "r", encoding="utf-8", errors="replace") as handle:
            source = handle.read()
        compilation_unit = javalang.parse.parse(source)
        package = compilation_unit.package.name if compilation_unit.package else ""
        explicit = {}
        wildcard = []
        for imp in (compilation_unit.imports or []):
            if imp.wildcard:
                wildcard.append(imp.path)
            else:
                explicit[imp.path.rsplit(".", 1)[-1]] = imp.path
        types = []
        for decl in compilation_unit.types:
            collect_type(decl, package, None, types)
        return {
            "ok": True,
            "path": absolute,
            "rel_root": rel_root,
            "rel_repo": rel_repo,
            "repo": repo,
            "module": module,
            "package": package,
            "explicit_imports": explicit,
            "wildcard_imports": wildcard,
            "types": types,
        }
    except Exception as exc:  # noqa: BLE001 - report parse failures instead of aborting
        return {
            "ok": False,
            "path": absolute,
            "rel_root": rel_root,
            "rel_repo": rel_repo,
            "repo": repo,
            "module": module,
            "error": "%s: %s" % (type(exc).__name__, exc),
        }


# ---------------------------------------------------------------------------
# source discovery / modules / repositories
# ---------------------------------------------------------------------------

class PomModuleResolver:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.cache = {}

    def _artifact_id(self, pom_path: str):
        try:
            tree = ET.parse(pom_path)
            root = tree.getroot()
            namespace = ""
            if root.tag.startswith("{"):
                namespace = root.tag[: root.tag.index("}") + 1]
            node = root.find("%sartifactId" % namespace)
            if node is not None and node.text:
                return node.text.strip()
        except (ET.ParseError, OSError):
            pass
        return None

    def module_for(self, path: str):
        directory = os.path.dirname(os.path.abspath(path))
        while True:
            if directory in self.cache:
                return self.cache[directory]
            pom = os.path.join(directory, "pom.xml")
            if os.path.isfile(pom):
                artifact = self._artifact_id(pom)
                self.cache[directory] = artifact
                return artifact
            if os.path.normpath(directory) == os.path.normpath(self.root):
                self.cache[directory] = None
                return None
            parent = os.path.dirname(directory)
            if parent == directory or not directory.startswith(self.root):
                self.cache[directory] = None
                return None
            directory = parent


def path_module(root: str, path: str, depth: int) -> str:
    rel = os.path.relpath(path, root)
    parts = Path(rel).parts
    if "src" in parts:
        prefix = parts[: parts.index("src")]
        if not prefix:
            return ""
        return "/".join(prefix[:depth] if depth > 0 else prefix)
    if len(parts) > 1:
        return parts[0] if depth == 1 else "/".join(parts[:depth] if depth > 0 else parts[:-1])
    return ""


def glob_regex(pattern: str):
    """Translate a POSIX-style glob into a regex where '*' does not cross '/'."""
    pattern = pattern.replace("\\", "/")
    out = ["^"]
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern.startswith("**/", index):
                out.append("(?:.*/)?")
                index += 3
            elif pattern.startswith("**", index):
                out.append(".*")
                index += 2
            else:
                out.append("[^/]*")
                index += 1
        elif char == "?":
            out.append("[^/]")
            index += 1
        elif char == "[":
            end = pattern.find("]", index + 1)
            if end == -1:
                out.append(r"\[")
                index += 1
            else:
                content = pattern[index + 1:end]
                if content.startswith("!"):
                    content = "^" + content[1:]
                out.append("[" + content + "]")
                index = end + 1
        else:
            out.append(re.escape(char))
            index += 1
    out.append("$")
    return re.compile("".join(out))


_GLOB_CACHE = {}


def glob_match(rel_path: str, pattern: str) -> bool:
    regex = _GLOB_CACHE.get(pattern)
    if regex is None:
        regex = glob_regex(pattern)
        _GLOB_CACHE[pattern] = regex
    return bool(regex.match(rel_path.replace("\\", "/")))


def matches_any(rel_path: str, patterns) -> str:
    for pattern in patterns:
        if glob_match(rel_path, pattern) or glob_match("/" + rel_path, pattern):
            return pattern
    return ""


def discover(args, timestamp):
    roots = [os.path.abspath(src) for src in args.src]
    for root in roots:
        if not os.path.isdir(root):
            raise SystemExit("java2uml: source root is not a directory: %s" % root)

    # Group roots by git repository, preserving argument order.
    repo_infos = {}
    repo_order = []
    root_repo = {}
    for root in roots:
        top, info = repo_identity(root, args.repo_name)
        if top is None:
            key = root
            top = root
            info = {
                "name": sanitize_segment(args.repo_name or os.path.basename(root)),
                "root": root,
                "remote": "",
                "commit": None,
                "branch": None,
                "describe": "nogit-" + timestamp,
                "tags": [],
                "dirty": None,
                "modified_files": 0,
            }
        key = os.path.abspath(top)
        if key not in repo_infos:
            repo_infos[key] = info
            repo_order.append(key)
        root_repo[root] = key
    if len(repo_infos) > 1 and args.repo_name:
        raise SystemExit("java2uml: --repo-name can only be used with a single repository")

    # Keep repository directory names unique when several repositories are
    # passed in a single run and happen to share a basename.
    used_names = {}
    for key in repo_order:
        base = repo_infos[key]["name"]
        name = base
        counter = 2
        while name in used_names:
            name = "%s-%d" % (base, counter)
            counter += 1
        used_names[name] = key
        repo_infos[key]["name"] = name

    excludes = list(DEFAULT_EXCLUDES) + list(args.exclude or [])
    includes = list(args.include or [])

    jobs = []
    exclusions = []
    seen = set()
    pom_cache = {}
    for root in roots:
        repo_key = root_repo[root]
        info = repo_infos[repo_key]
        repo_name = info["name"]
        if args.module_mode == "pom":
            resolver = pom_cache.setdefault(root, PomModuleResolver(root))
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(
                d for d in dirs
                if d not in (".git", "target", "build", "out", "node_modules", "__pycache__", ".gradle", ".idea")
            )
            for filename in sorted(files):
                if not filename.endswith(".java"):
                    continue
                absolute = os.path.join(current, filename)
                rel_root = os.path.relpath(absolute, root).replace(os.sep, "/")
                rel_repo = posix_rel(absolute, info["root"])
                if absolute in seen:
                    continue
                seen.add(absolute)
                reason = matches_any(rel_root, excludes) or matches_any(rel_repo, excludes)
                if reason:
                    exclusions.append({"path": rel_repo, "reason": "excluded by %s" % reason})
                    continue
                if includes and not (matches_any(rel_root, includes) or matches_any(rel_repo, includes)):
                    exclusions.append({"path": rel_repo, "reason": "does not match --include"})
                    continue
                if args.module_mode == "path":
                    module = path_module(root, absolute, args.module_depth)
                elif args.module_mode == "pom":
                    module = resolver.module_for(absolute) or ""
                else:
                    module = ""
                jobs.append((absolute, rel_root, rel_repo, repo_name, module))

    # Detect duplicate fully-qualified type names across repositories/modules.
    return roots, repo_infos, repo_order, root_repo, jobs, exclusions


# ---------------------------------------------------------------------------
# type index / resolution
# ---------------------------------------------------------------------------

class Index:
    def __init__(self, files):
        self.by_key = {}
        self.by_repo_fqn = defaultdict(set)
        self.by_simple = defaultdict(set)
        self.by_repo_simple = defaultdict(set)
        for file_info in files:
            for t in file_info.get("types", []):
                key = (file_info["repo"], file_info["module"], t["fqn"])
                t["_repo"] = file_info["repo"]
                t["_file"] = file_info["path"]
                t["_rel_repo"] = file_info["rel_repo"]
                t["_module"] = file_info["module"]
                t["_package"] = file_info["package"]
                t["_explicit"] = file_info.get("explicit_imports", {})
                t["_wildcard"] = file_info.get("wildcard_imports", [])
                self.by_key[key] = t
                self.by_repo_fqn[(file_info["repo"], t["fqn"])].add(key)
                simple = t["name"]
                self.by_simple[simple].add(key)
                self.by_repo_simple[(file_info["repo"], simple)].add(key)

    def _by_fqn(self, repo, module, fqn):
        """Resolve a fully-qualified name, preferring the current module."""
        keys = self.by_repo_fqn.get((repo, fqn)) or set()
        if len(keys) == 1:
            return next(iter(keys))
        same_module = [key for key in keys if key[1] == module]
        if len(same_module) == 1:
            return same_module[0]
        return None

    def resolve_base(self, raw, t, extra_typevars=()):
        base = base_type(raw)
        if not base:
            return None
        if base in PRIMITIVES or base in TYPE_KEYWORDS:
            return None
        if base in set(t.get("_typevars", ())) | set(extra_typevars):
            return None
        if base.startswith(JDK_PREFIXES):
            return None
        repo = t["_repo"]
        module = t.get("_module", "")

        # Exact fully-qualified name (within the repository).
        result = self._by_fqn(repo, module, base)
        if result:
            return result

        # Dotted names: Outer.Inner or partially qualified.
        if "." in base:
            head, rest = base.split(".", 1)
            head_key = self.resolve_base(head, t, extra_typevars)
            if head_key:
                candidate = (head_key[0], head_key[1], head_key[2] + "." + rest)
                if candidate in self.by_key:
                    return candidate
            package = t.get("_package", "")
            if package:
                candidate = (repo, module, package + "." + base)
                if candidate in self.by_key:
                    return candidate
            for imported in t.get("_explicit", {}).values():
                if imported.endswith("." + head):
                    candidate = (repo, module, imported + "." + rest)
                    if candidate in self.by_key:
                        return candidate

        # Sibling nested type of the current type.
        outer = t.get("outer")
        if outer:
            candidate = (repo, module, outer + "." + base)
            if candidate in self.by_key:
                return candidate

        # Same package.
        package = t.get("_package", "")
        if package:
            candidate = (repo, module, package + "." + base)
            if candidate in self.by_key:
                return candidate

        # Explicit import.
        imported = t.get("_explicit", {}).get(base)
        if imported:
            candidate = (repo, module, imported)
            if candidate in self.by_key:
                return candidate
            candidate = self._by_fqn(repo, module, imported)
            if candidate:
                return candidate

        # Wildcard imports.
        for wildcard in t.get("_wildcard", []):
            candidate = (repo, module, wildcard + "." + base)
            if candidate in self.by_key:
                return candidate

        # java.lang types are intentionally ignored (external to the project).
        if base in JDK_LANG_SIMPLE:
            return None

        # Fall back to a simple name. Prefer the current module, then a unique
        # match inside the repository, then a unique global match.
        matches = self.by_repo_simple.get((repo, base)) or set()
        if len(matches) == 1:
            return next(iter(matches))
        same_module = [key for key in matches if key[1] == module]
        if len(same_module) == 1:
            return same_module[0]
        if not matches:
            matches = self.by_simple.get(base) or set()
            if len(matches) == 1:
                return next(iter(matches))
        return None

    def resolve_refs(self, raw, t, extra_typevars=()):
        if not raw:
            return []
        found = []
        for token in TOKEN_RE.findall(raw):
            token = re.sub(r"\s+", "", token)
            if token in TYPE_KEYWORDS:
                continue
            key = self.resolve_base(token, t, extra_typevars)
            if key and key not in found:
                found.append(key)
        return found


def base_type(raw: str) -> str:
    if not raw:
        return ""
    value = raw.strip()
    value = re.sub(r"\[\s*\]", "", value)
    value = re.sub(r"\.\.\.$", "", value)
    value = re.sub(r"^final\s+", "", value)
    if value.startswith("?"):
        value = re.sub(r"^\?\s*(extends|super)\s+", "", value).strip()
    if "<" in value:
        value = value.split("<", 1)[0]
    return value.strip()


def ignorable_type(raw: str) -> bool:
    base = base_type(raw)
    return base in PRIMITIVES or base in JDK_LANG_SIMPLE or base.startswith(JDK_PREFIXES)


# ---------------------------------------------------------------------------
# Mermaid emission
# ---------------------------------------------------------------------------

def sanitize_type_text(raw: str, separator: str) -> str:
    if not raw:
        return ""
    value = re.sub(r"\s+", " ", raw.strip())
    value = value.replace("<", "~").replace(">", "~")
    value = value.replace(",", separator)
    value = value.replace("{", "~").replace("}", "~")
    return value


def member_visible(vis: str, mode: str) -> bool:
    if mode == "all":
        return True
    if mode == "public":
        return vis == "public"
    return vis in ("public", "protected")


def render_params(params, separator: str) -> str:
    rendered = []
    for param in params:
        ptype = sanitize_type_text(param.get("type"), separator)
        if param.get("varargs"):
            ptype += "..."
        rendered.append("%s %s" % (ptype, param["name"]))
    return ", ".join(rendered)


def render_method(method, separator: str, is_constructor=False, owner_name=""):
    name = owner_name if is_constructor else method["name"]
    line = "%s%s(%s)" % (VISIBILITY[method["visibility"]], name, render_params(method["params"], separator))
    if not is_constructor:
        ret = sanitize_type_text(method.get("return_type"), separator)
        if ret:
            line += " " + ret
    if method.get("throws"):
        line += " throws " + ", ".join(sanitize_type_text(x, separator) for x in method["throws"])
    if not is_constructor:
        if method.get("abstract"):
            line += "*"
        elif method.get("static"):
            line += "$"
    return line


def render_field(field, separator: str):
    line = "%s%s %s" % (
        VISIBILITY[field["visibility"]],
        sanitize_type_text(field.get("type"), separator),
        field["name"],
    )
    if field.get("static"):
        line += "$"
    return line


def class_label(t, class_labels, separator):
    name = t["fqn"] if class_labels == "fqn" else t["name"]
    if t.get("type_parameters"):
        params = []
        for param in t["type_parameters"]:
            if param.get("bounds"):
                params.append("%s extends %s" % (
                    param["name"],
                    separator.join(sanitize_type_text(b, separator) for b in param["bounds"]),
                ))
            else:
                params.append(param["name"])
        # Mermaid strips angle brackets from quoted labels, but HTML entities
        # survive and render as literal <...> in the diagram.
        name += "&lt;" + ", ".join(params) + "&gt;"
    return name.replace('"', "'")


def build_relations(types, index, options):
    """Return (relations, external_types, unresolved)."""
    local = {(t["_repo"], t["_module"], t["fqn"]) for t in types}
    external = {}
    unresolved = set()
    relations = set()

    def add(src, dst, arrow, label=None, multiplicity=None):
        relations.add((src, dst, arrow, label or "", multiplicity or ""))

    for t in types:
        key = (t["_repo"], t["_module"], t["fqn"])
        tvars = {p["name"] for p in t.get("type_parameters", [])}

        # inheritance / realization
        for raw in t.get("extends", []):
            target = index.resolve_base(raw, t, tvars)
            if target:
                add(target, key, "<|--", None)
            elif raw and not ignorable_type(raw):
                unresolved.add(raw)
        for raw in t.get("implements", []):
            target = index.resolve_base(raw, t, tvars)
            if target:
                add(target, key, "<|..", None)
            elif raw and not ignorable_type(raw):
                unresolved.add(raw)

        # nested types
        if t.get("outer"):
            outer_fqn = t["outer"]
            outer_key = (t["_repo"], t["_module"], outer_fqn)
            if outer_key in index.by_key:
                add(outer_key, key, "*--", "nested")

        if options.relations == "none":
            continue

        # fields
        for field in t.get("fields", []):
            raw = field.get("type") or ""
            base = base_type(raw)
            is_many = base in COLLECTIONS or "[" in raw or "..." in raw
            targets = index.resolve_refs(raw, t, tvars)
            for target in targets:
                if target == key:
                    continue
                multi = "*" if is_many else "1"
                add(key, target, "-->", field["name"], multi)

        if options.relations != "full":
            continue

        # method and constructor dependencies
        for method in t.get("methods", []):
            method_tvars = tvars | set(method.get("type_parameters", []))
            raws = [method.get("return_type")] + [p.get("type") for p in method.get("params", [])]
            raws += list(method.get("throws", []))
            for raw in raws:
                for target in index.resolve_refs(raw, t, method_tvars):
                    if target != key:
                        add(key, target, "..>", None)
        for ctor in t.get("constructors", []):
            raws = [p.get("type") for p in ctor.get("params", [])] + list(ctor.get("throws", []))
            for raw in raws:
                for target in index.resolve_refs(raw, t, tvars):
                    if target != key:
                        add(key, target, "..>", None)

    # Method dependencies that duplicate a structural edge add noise without
    # adding information; keep only dependencies that are the sole relation.
    structural_pairs = {(src, dst) for src, dst, arrow, _label, _multi in relations
                        if arrow != "..>"}
    relations = {rel for rel in relations
                 if rel[2] != "..>" or (rel[0], rel[1]) not in structural_pairs}

    # collect stubs for relation endpoints outside the diagram
    for src, dst, _arrow, _label, _multi in relations:
        for endpoint in (src, dst):
            if endpoint not in local and endpoint in index.by_key:
                stub = dict(index.by_key[endpoint])
                stub["_external"] = True
                external[endpoint] = stub

    return relations, external, unresolved


def emit_diagram(types, index, options, provenance):
    relations, external, unresolved = build_relations(types, index, options)
    if not options.external:
        local_keys = {(t["_repo"], t["_module"], t["fqn"]) for t in types}
        relations = {
            rel for rel in relations
            if rel[0] in local_keys and rel[1] in local_keys
        }
        external = {}

    lines = []
    if options.layout != "none" or options.theme:
        lines.append("---")
        lines.append("config:")
        if options.layout != "none":
            lines.append("  layout: %s" % options.layout)
        if options.theme:
            lines.append("  theme: %s" % options.theme)
        lines.append("---")
    for comment in provenance:
        lines.append("%% " + comment)
    lines.append("classDiagram")

    render_types = sorted(types, key=lambda t: (t["fqn"]))
    render_external = sorted(external.values(), key=lambda t: (t["fqn"]))

    for t in render_types + render_external:
        ident = mermaid_id(t["_repo"], t["_module"], t["fqn"])
        label = class_label(t, options.class_labels, options.generic_separator)
        lines.append('  class %s["%s"] {' % (ident, label))
        if t.get("_external"):
            lines.append("    <<external>>")
        elif t["kind"] == "interface":
            lines.append("    <<interface>>")
        elif t["kind"] == "enum":
            lines.append("    <<enumeration>>")
        elif t["kind"] == "annotation":
            lines.append("    <<annotation>>")
        elif t.get("abstract"):
            lines.append("    <<abstract>>")
        for annotation in t.get("annotations", []):
            lines.append("    <<@%s>>" % sanitize_type_text(annotation, options.generic_separator))
        for constant in t.get("enum_constants", []):
            lines.append("    +%s" % constant)
        for field in t.get("fields", []):
            if member_visible(field["visibility"], options.visibility):
                lines.append("    " + render_field(field, options.generic_separator))
        for ctor in t.get("constructors", []):
            if member_visible(ctor["visibility"], options.visibility):
                lines.append("    " + render_method(ctor, options.generic_separator, True, t["name"]))
        for method in t.get("methods", []):
            if member_visible(method["visibility"], options.visibility):
                lines.append("    " + render_method(method, options.generic_separator))
        lines.append("  }")

    ids = {}
    for t in render_types + render_external:
        ids[(t["_repo"], t["_module"], t["fqn"])] = mermaid_id(t["_repo"], t["_module"], t["fqn"])

    def clean_label(value):
        return str(value).replace(":", " -").replace('"', "'").replace("\n", " ")

    for src, dst, arrow, label, multiplicity in sorted(relations, key=lambda r: (r[0], r[1], r[2], r[3], r[4])):
        if src not in ids or dst not in ids:
            continue
        left = ids[src]
        right = ids[dst]
        parts = [left, arrow]
        if multiplicity:
            parts.append('"%s"' % multiplicity)
        parts.append(right)
        line = "  " + " ".join(parts)
        if label:
            line += " : " + clean_label(label)
        lines.append(line)

    stats = {
        "types": len(render_types),
        "external_types": sorted({t["fqn"] for t in render_external}),
        "relations": len(relations),
        "unresolved": sorted(unresolved),
    }
    return "\n".join(lines) + "\n", stats


# ---------------------------------------------------------------------------
# output documents
# ---------------------------------------------------------------------------

def md_table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return "\n".join(lines)


def wrapper_markdown(title, provenance_rows, source_files, mmd_text, back_link):
    parts = ["# `%s`" % title, ""]
    parts.append(md_table(["Field", "Value"], provenance_rows))
    parts.append("")
    if source_files:
        parts.append("## Source files")
        parts.append("")
        for source in source_files:
            parts.append("- `%s`" % source)
        parts.append("")
    parts.append("## Diagram")
    parts.append("")
    parts.append("```mermaid")
    parts.append(mmd_text.rstrip())
    parts.append("```")
    parts.append("")
    if back_link:
        parts.append("[Back to run overview](%s)" % back_link)
        parts.append("")
    return "\n".join(parts)


def readme_markdown(manifest, argv, run_readme_name="README.md"):
    run = manifest["run"]
    repos = manifest["repositories"]
    parts = ["# java2uml run: %s" % run["id"], ""]
    parts.append(md_table(
        ["Field", "Value"],
        [
            ["Generated", run["timestamp"]],
            ["Generator", "%s %s" % (manifest["generator"]["name"], manifest["generator"]["version"])],
            ["Parser", manifest["generator"]["parser"]],
            ["Output", run["output"]],
        ],
    ))
    parts.append("")
    parts.append("## Repositories")
    parts.append("")
    parts.append(md_table(
        ["Repository", "Commit", "Branch", "Describe", "Dirty", "Source roots"],
        [
            [r["name"], r.get("commit") or "-", r.get("branch") or "-",
             r.get("describe") or "-", r.get("dirty"), "<br>".join(r.get("source_roots", []))]
            for r in repos
        ],
    ))
    summary = manifest["summary"]
    parts.append("")
    parts.append("## Summary")
    parts.append("")
    parts.append(md_table(
        ["Metric", "Value"],
        [
            ["Java files scanned", summary["files_scanned"]],
            ["Java files included", summary["files_included"]],
            ["Java files without types", summary.get("files_without_types", 0)],
            ["Java files excluded", summary["files_excluded"]],
            ["Parse failures", summary["parse_failures"]],
            ["Types", summary["types"]],
            ["Diagrams", summary["diagrams"]],
            ["Relations", summary["relations"]],
        ],
    ))
    parts.append("")
    parts.append("## Diagrams")
    parts.append("")
    by_module = defaultdict(list)
    for diagram in manifest["diagrams"]:
        by_module[(diagram["repo"], diagram["module"] or "_root")].append(diagram)
    for (repo, module), diagrams in sorted(by_module.items()):
        parts.append("### %s / %s" % (repo, module))
        parts.append("")
        parts.append(md_table(
            ["Package", "Types", "Relations", "External", "Diagram"],
            [
                [d["package"] or "(default)", d["types"], d["relations"],
                 len(d.get("external_types", [])),
                 " / ".join(link for link in (
                     "[Markdown](%s)" % d["md"] if d.get("md") else "",
                     "[Mermaid](%s)" % d["mmd"] if d.get("mmd") else "",
                 ) if link)]
                for d in sorted(diagrams, key=lambda x: x["package"])
            ],
        ))
        parts.append("")
    parts.append("## Exclusions")
    parts.append("")
    parts.append("%d files were excluded. See `manifest.json` for the full list." % summary["files_excluded"])
    parts.append("")
    parts.append("## Regenerate")
    parts.append("")
    parts.append("```bash")
    parts.append("bash run.sh " + " ".join(argv))
    parts.append("```")
    parts.append("")
    parts.append("## Notes")
    parts.append("")
    parts.append("- Diagrams are generated from source only; initializer bodies and method bodies are not modeled.")
    parts.append("- Mermaid does not support commas inside generic type arguments; ")
    parts.append("  they are rendered with the configured separator (`%s` by default)." % run["options"].get("generic_separator", ";"))
    parts.append("- `<<external>>` classes are referenced from the package but declared elsewhere.")
    parts.append("")
    return "\n".join(parts)


def coverage_markdown(manifest):
    parts = ["# Coverage", ""]
    parts.append(md_table(
        ["Metric", "Value"],
        [
            ["Files included", manifest["summary"]["files_included"]],
            ["Files without types", manifest["summary"].get("files_without_types", 0)],
            ["Files excluded", manifest["summary"]["files_excluded"]],
            ["Parse failures", manifest["summary"]["parse_failures"]],
        ],
    ))
    parts.append("")
    parts.append("## Files by module")
    parts.append("")
    counts = defaultdict(lambda: {"files": 0, "diagrams": set()})
    for source in manifest["sources"]:
        key = (source["repo"], source["module"] or "_root")
        counts[key]["files"] += 1
        if source["diagram"]:
            counts[key]["diagrams"].add(source["diagram"])
    parts.append(md_table(
        ["Repository", "Module", "Files", "Diagrams"],
        [[repo, module, data["files"], len(data["diagrams"])] for (repo, module), data in sorted(counts.items())],
    ))
    parts.append("")
    if manifest["failures"]:
        parts.append("## Parse failures")
        parts.append("")
        parts.append(md_table(
            ["File", "Error"],
            [[f["path"], f["error"]] for f in manifest["failures"]],
        ))
        parts.append("")
    if manifest["exclusions"]:
        parts.append("## Excluded files")
        parts.append("")
        parts.append(md_table(
            ["File", "Reason"],
            [[e["path"], e["reason"]] for e in manifest["exclusions"]],
        ))
        parts.append("")
    without_types = [s["path"] for s in manifest["sources"] if not s["diagram"]]
    if without_types:
        parts.append("## Files without types")
        parts.append("")
        parts.append("These files parsed successfully but declare no types "
                     "(for example fully commented-out source files).")
        parts.append("")
        for path in without_types:
            parts.append("- `%s`" % path)
        parts.append("")
    parts.append("The machine-readable file-to-diagram mapping is in `coverage.json`.")
    parts.append("")
    return "\n".join(parts)


def write_run(run_dir, metadata, timestamp, argv, roots, repo_infos, root_repo, exclusions,
              file_results, diagram_by_file, created_diagrams, options, label):
    sources = []
    failures = []
    for result in file_results:
        if not result["ok"]:
            failures.append({"path": result["rel_repo"], "absolute_path": result["path"],
                             "error": result["error"]})
            continue
        diagram = diagram_by_file.get(result["path"])
        sources.append({
            "path": result["rel_repo"],
            "absolute_path": result["path"],
            "repo": result["repo"],
            "module": result["module"],
            "package": result["package"],
            "diagram": (diagram.get("mmd") or diagram.get("md")) if diagram else None,
            "types": len(result["types"]),
        })
    sources.sort(key=lambda s: s["path"])
    failures.sort(key=lambda f: f["path"])

    diagram_records = sorted(
        ({key: value for key, value in diagram.items() if not key.startswith("_")}
         for diagram in created_diagrams),
        key=lambda d: (d["repo"], d["module"], d["package"], d["mmd"]),
    )

    total_types = sum(d["types"] for d in diagram_records)
    total_relations = sum(d["relations"] for d in diagram_records)
    without_types = [s for s in sources if not s["diagram"]]
    summary = {
        "repositories": len(repo_infos),
        "source_roots": len(roots),
        "files_scanned": len(file_results) + len(exclusions),
        "files_included": len([r for r in file_results if r["ok"]]),
        "files_excluded": len(exclusions),
        "files_without_types": len(without_types),
        "parse_failures": len(failures),
        "types": total_types,
        "diagrams": len(diagram_records),
        "relations": total_relations,
    }

    repo_records = []
    for key in repo_infos:
        info = dict(repo_infos[key])
        info["source_roots"] = sorted({
            os.path.relpath(root, info["root"]).replace(os.sep, "/") if root != info["root"] else "."
            for root in roots if root_repo[root] == key
        })
        repo_records.append(info)

    primary = repo_records[0]
    run_id = "%s/%s" % (primary["name"], label)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generator": {
            "name": "java2uml",
            "version": VERSION,
            "parser": "javalang",
            "parser_version": getattr(javalang, "__version__", "0.13.0"),
            "python": sys.version.split()[0],
        },
        "run": {
            "id": run_id,
            "timestamp": timestamp,
            "argv": argv,
            "options": options,
            "output": run_dir,
            "metadata": metadata or {},
            "label": label,
        },
        "repositories": repo_records,
        "summary": summary,
        "sources": sources,
        "diagrams": diagram_records,
        "exclusions": sorted(exclusions, key=lambda e: e["path"]),
        "failures": failures,
    }

    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=False)
        handle.write("\n")

    if options.get("coverage", True):
        coverage = {
            "schema_version": SCHEMA_VERSION,
            "run": run_id,
            "files": {
                source["path"]: {
                    "diagram": source["diagram"],
                    "repo": source["repo"],
                    "module": source["module"],
                    "package": source["package"],
                }
                for source in sources
            },
            "excluded": {e["path"]: e["reason"] for e in manifest["exclusions"]},
            "failures": {f["path"]: f["error"] for f in failures},
        }
        with open(os.path.join(run_dir, "coverage.json"), "w", encoding="utf-8") as handle:
            json.dump(coverage, handle, indent=2, sort_keys=False)
            handle.write("\n")

    return manifest


# ---------------------------------------------------------------------------
# index
# ---------------------------------------------------------------------------

def rebuild_index(collection, timestamp=None):
    collection = os.path.abspath(collection)
    runs = []
    for manifest_path in sorted(Path(collection).glob("*/*/manifest.json")):
        try:
            with open(manifest_path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("generator", {}).get("name") != "java2uml":
            continue
        primary = (manifest.get("repositories") or [{}])[0]
        repositories = [repo for repo in (manifest.get("repositories") or []) if repo.get("name")]
        runs.append({
            "manifest": manifest_path.relative_to(collection).as_posix(),
            "run": manifest.get("run", {}).get("id"),
            "repository": primary.get("name"),
            "repositories": [repo.get("name") for repo in repositories],
            "remote": primary.get("remote"),
            "remotes": [repo.get("remote") for repo in repositories if repo.get("remote")],
            "revision": manifest.get("run", {}).get("label"),
            "commit": primary.get("commit"),
            "commits": {repo.get("name"): repo.get("commit") for repo in repositories},
            "branch": primary.get("branch"),
            "describe": primary.get("describe"),
            "describes": {repo.get("name"): repo.get("describe") for repo in repositories},
            "generated_at": manifest.get("run", {}).get("timestamp"),
            "diagrams": manifest.get("summary", {}).get("diagrams", 0),
            "types": manifest.get("summary", {}).get("types", 0),
            "relations": manifest.get("summary", {}).get("relations", 0),
        })
    runs.sort(key=lambda r: (r.get("repository") or "", r.get("revision") or ""))
    index = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": timestamp or utc_now(),
        "collection": collection,
        "runs": runs,
    }
    with open(os.path.join(collection, "index.json"), "w", encoding="utf-8") as handle:
        json.dump(index, handle, indent=2, sort_keys=False)
        handle.write("\n")

    parts = ["# Mermaid UML collection", ""]
    parts.append("Generated by java2uml %s on %s." % (VERSION, index["generated_at"]))
    parts.append("")
    parts.append(md_table(
        ["Repository", "Revision", "Commit", "Generated", "Types", "Diagrams", "Run"],
        [
            [", ".join(r.get("repositories") or [r.get("repository") or "-"]),
             r.get("revision") or "-",
             "<br>".join("%s %s" % (name, (commit or "-")[:12])
                         for name, commit in (r.get("commits") or {}).items()) or (r.get("commit") or "-")[:12],
             r.get("generated_at") or "-",
             r.get("types", 0), r.get("diagrams", 0),
             "[overview](%s)" % (r["manifest"].rsplit("/", 1)[0] + "/README.md")]
            for r in runs
        ],
    ))
    parts.append("")
    parts.append("Machine-readable index: [index.json](index.json).")
    parts.append("")
    with open(os.path.join(collection, "README.md"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(parts))
    return index


# ---------------------------------------------------------------------------
# command: generate
# ---------------------------------------------------------------------------

def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="java2uml",
        description="Generate Mermaid class diagrams from Java sources.",
    )
    parser.add_argument("--version", action="version", version="java2uml " + VERSION)
    subparsers = parser.add_subparsers(dest="command")

    gen = subparsers.add_parser("generate", help="generate diagrams")
    gen.add_argument("--src", nargs="+", required=True, metavar="DIR",
                     help="source root(s) to scan")
    target = gen.add_mutually_exclusive_group(required=True)
    target.add_argument("--collection", metavar="DIR",
                        help="collection root; output goes to <collection>/<repo>/<revision>/")
    target.add_argument("--out", metavar="DIR", help="exact output directory for this run")
    gen.add_argument("--run-label", help="override the revision label")
    gen.add_argument("--repo-name", help="override the repository name (single repository only)")
    gen.add_argument("--metadata", action="append", default=[], metavar="KEY=VALUE",
                     help="extra metadata stored in the run manifest")
    gen.add_argument("--timestamp", help="override the run timestamp (ISO-8601)")
    gen.add_argument("--include", action="append", default=[], metavar="GLOB",
                     help="only include matching source paths (repeatable)")
    gen.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                     help="exclude matching source paths (repeatable)")
    gen.add_argument("--module-mode", choices=["path", "pom", "none"], default="path",
                     help="module detection strategy (default: path)")
    gen.add_argument("--module-depth", type=int, default=1,
                     help="path-mode module depth (default: 1)")
    gen.add_argument("--group-by", choices=["package", "module-package", "repo-module-package"],
                     default="module-package", help="diagram grouping (default: module-package)")
    gen.add_argument("--relations", choices=["full", "fields", "none"], default="full",
                     help="relationship detail (default: full)")
    gen.add_argument("--visibility", choices=["all", "public", "protected"], default="all",
                     help="member visibility filter (default: all)")
    gen.add_argument("--format", choices=["mmd", "md", "both"], default="both",
                     help="diagram output format (default: both)")
    gen.add_argument("--index", dest="index", action="store_true", default=True,
                     help="write collection index (default)")
    gen.add_argument("--no-index", dest="index", action="store_false",
                     help="do not write the collection index")
    gen.add_argument("--coverage", dest="coverage", action="store_true", default=True,
                     help="write coverage report (default)")
    gen.add_argument("--no-coverage", dest="coverage", action="store_false",
                     help="do not write the coverage report")
    gen.add_argument("--external", dest="external", action="store_true", default=True,
                     help="include <<external>> stub classes (default)")
    gen.add_argument("--no-external", dest="external", action="store_false",
                     help="omit external stub classes and their relations")
    gen.add_argument("--split-threshold", type=int, default=0, metavar="N",
                     help="split packages with more than N types; 0 = never split (default: 0)")
    gen.add_argument("--class-labels", choices=["simple", "fqn"], default="simple",
                     help="class label style (default: simple)")
    gen.add_argument("--generic-separator", default=";", metavar="CHAR",
                     help="replacement for commas inside generic type arguments (default: ;)")
    gen.add_argument("--layout", choices=["dagre", "elk", "none"], default="dagre",
                     help="Mermaid layout engine (default: dagre)")
    gen.add_argument("--theme", help="Mermaid theme name")
    gen.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 8),
                     help="parallel parser workers (default: min(cpus, 8))")
    gen.add_argument("--overwrite", action="store_true", help="replace an existing run directory")
    gen.add_argument("--force", action="store_true",
                     help="allow --overwrite on directories not created by java2uml")
    gen.add_argument("--dry-run", action="store_true", help="parse and report, write nothing")
    gen.add_argument("--verbose", action="store_true")

    reindex = subparsers.add_parser("reindex", help="rebuild a collection index from manifests")
    reindex.add_argument("--collection", required=True, metavar="DIR")
    return parser


def parse_metadata(items):
    metadata = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit("java2uml: --metadata expects KEY=VALUE, got: %s" % item)
        key, value = item.split("=", 1)
        metadata[key.strip()] = value.strip()
    return metadata


def cmd_generate(args):
    if javalang is None:
        raise SystemExit("java2uml: the 'javalang' package is required. Install with: "
                         "python -m pip install -r requirements.txt")
    if args.jobs < 1:
        raise SystemExit("java2uml: --jobs must be >= 1")
    if args.module_depth < 0:
        raise SystemExit("java2uml: --module-depth must be >= 0")

    timestamp = args.timestamp or utc_now()
    metadata = parse_metadata(args.metadata)
    args.metadata = metadata
    argv = sys.argv[1:]

    roots, repo_infos, repo_order, root_repo, jobs, exclusions = discover(args, timestamp)
    if not jobs:
        raise SystemExit("java2uml: no Java files matched the given roots and filters")

    if args.verbose:
        print("java2uml: %d Java files, %d repositories" % (len(jobs), len(repo_infos)), file=sys.stderr)

    file_results = []
    if args.jobs == 1 or len(jobs) < 20:
        for job in jobs:
            file_results.append(extract_file(job))
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as pool:
            for result in pool.map(extract_file, jobs, chunksize=20):
                file_results.append(result)

    file_results.sort(key=lambda r: r["path"])
    parse_failures = [r for r in file_results if not r["ok"]]
    parsed = [r for r in file_results if r["ok"]]

    index = Index(parsed)
    for file_info in parsed:
        for t in file_info["types"]:
            t["_typevars"] = {p["name"] for p in t.get("type_parameters", [])}

    # Group types into diagrams.
    groups = defaultdict(list)
    for file_info in parsed:
        for t in file_info["types"]:
            if args.group_by == "package":
                key = (file_info["repo"], file_info["package"])
            else:
                key = (file_info["repo"], file_info["module"], file_info["package"])
            groups[key].append(t)

    options = {
        "src": roots,
        "collection": os.path.abspath(args.collection) if args.collection else None,
        "out": os.path.abspath(args.out) if args.out else None,
        "run_label": args.run_label,
        "repo_name": args.repo_name,
        "metadata": metadata,
        "include": args.include,
        "exclude": args.exclude,
        "module_mode": args.module_mode,
        "module_depth": args.module_depth,
        "group_by": args.group_by,
        "relations": args.relations,
        "visibility": args.visibility,
        "format": args.format,
        "index": args.index,
        "coverage": args.coverage,
        "external": args.external,
        "split_threshold": args.split_threshold,
        "class_labels": args.class_labels,
        "generic_separator": args.generic_separator,
        "layout": args.layout,
        "theme": args.theme,
        "jobs": args.jobs,
    }

    primary_info = repo_infos[repo_order[0]]
    label = sanitize_segment(args.run_label or primary_info.get("describe") or ("nogit-" + timestamp))
    run_id = "%s/%s" % (primary_info["name"], label)

    if args.out:
        run_dir = os.path.abspath(args.out)
    else:
        run_dir = os.path.join(os.path.abspath(args.collection), primary_info["name"], label)

    if args.dry_run:
        print("java2uml: run %s" % run_id)
        print("java2uml: output %s" % run_dir)
        print("java2uml: files=%d parsed=%d failed=%d excluded=%d"
              % (len(file_results), len(parsed), len(parse_failures), len(exclusions)))
        for key in sorted(groups):
            print("  %s/%s/%s: %d types" % (key[0], key[1] or "_root", key[2] or "(default)", len(groups[key])))
        return 0

    if os.path.isdir(run_dir) and os.listdir(run_dir):
        if not args.overwrite:
            raise SystemExit("java2uml: output directory exists (use --overwrite): %s" % run_dir)
        existing_manifest = os.path.join(run_dir, "manifest.json")
        generated = False
        try:
            with open(existing_manifest, "r", encoding="utf-8") as handle:
                generated = json.load(handle).get("generator", {}).get("name") == "java2uml"
        except (OSError, json.JSONDecodeError):
            generated = False
        if not generated and not args.force:
            raise SystemExit("java2uml: refusing to overwrite a directory not created by java2uml "
                             "(use --force): %s" % run_dir)
        shutil.rmtree(run_dir)

    repo_by_name = {info["name"]: info for info in repo_infos.values()}

    diagrams = {}
    created_diagrams = []
    diagram_by_file = {}

    for key in sorted(groups):
        types = groups[key]
        if args.group_by == "package":
            repo, package = key
            module = ""
        else:
            repo, module, package = key
        chunks = [types]
        if args.split_threshold > 0 and len(types) > args.split_threshold:
            ordered = sorted(types, key=lambda t: t["fqn"])
            chunks = [ordered[i:i + args.split_threshold]
                      for i in range(0, len(ordered), args.split_threshold)]

        for chunk_index, chunk in enumerate(chunks):
            suffix = "" if len(chunks) == 1 else ".part%02d" % (chunk_index + 1)
            package_path = sanitize_path(package.replace(".", "/")) if package else "_default"
            module_path = sanitize_segment(module) if module else "_root"
            if args.group_by == "package":
                base_rel = "source/%s/%s%s" % (sanitize_segment(repo), package_path, suffix)
            else:
                base_rel = "source/%s/%s/%s%s" % (
                    sanitize_segment(repo), module_path, package_path, suffix)
            mmd_rel = base_rel + ".mmd"
            md_rel = base_rel + ".md"
            info = repo_by_name.get(repo, {})
            provenance = [
                "java2uml %s | repo: %s | revision: %s | commit: %s"
                % (VERSION, repo, label, (info.get("commit") or "-")[:12]),
                "module: %s | package: %s | types: %d"
                % (module or "_root", package or "(default)", len(chunk)),
            ]
            mmd_text, stats = emit_diagram(chunk, index, args, provenance)
            mmd_abs = os.path.join(run_dir, mmd_rel)
            md_abs = os.path.join(run_dir, md_rel)
            os.makedirs(os.path.dirname(mmd_abs), exist_ok=True)

            if args.format in ("mmd", "both"):
                with open(mmd_abs, "w", encoding="utf-8") as handle:
                    handle.write(mmd_text)
            if args.format in ("md", "both"):
                back_link = posix_rel(os.path.join(run_dir, "README.md"), os.path.dirname(md_abs))
                rows = [
                    ["Repository", repo],
                    ["Revision", label],
                    ["Commit", (info.get("commit") or "-")],
                    ["Module", module or "_root"],
                    ["Package", package or "(default)"],
                    ["Types", len(chunk)],
                    ["Relations", stats["relations"]],
                    ["Generator", "java2uml %s" % VERSION],
                ]
                source_files = sorted({t["_rel_repo"] for t in chunk})
                title = (module + " / " if module else "") + (package or "(default)")
                content = wrapper_markdown(title, rows, source_files, mmd_text, back_link)
                with open(md_abs, "w", encoding="utf-8") as handle:
                    handle.write(content)

            diagram_record = {
                "repo": repo,
                "module": module,
                "package": package,
                "mmd": mmd_rel if args.format in ("mmd", "both") else None,
                "md": md_rel if args.format in ("md", "both") else None,
                "types": stats["types"],
                "relations": stats["relations"],
                "external_types": stats["external_types"],
                "unresolved": stats["unresolved"],
                "source_files": sorted({t["_rel_repo"] for t in chunk}),
                "split": len(chunks) > 1,
                "part": chunk_index + 1 if len(chunks) > 1 else None,
            }
            diagram_key = (repo, module, package, chunk_index) if len(chunks) > 1 \
                else (repo, module, package)
            diagrams[diagram_key] = diagram_record
            created_diagrams.append(diagram_record)
            for t in chunk:
                # With splitting, a file's types can span several chunks; map the
                # file to the first diagram that contains any of its types.
                diagram_by_file.setdefault(t["_file"], diagram_record)

    manifest = write_run(
        run_dir, metadata, timestamp, argv, roots, repo_infos, root_repo, exclusions,
        parsed + parse_failures, diagram_by_file, created_diagrams, options, label,
    )

    if args.coverage:
        with open(os.path.join(run_dir, "coverage.md"), "w", encoding="utf-8") as handle:
            handle.write(coverage_markdown(manifest))
    with open(os.path.join(run_dir, "README.md"), "w", encoding="utf-8") as handle:
        handle.write(readme_markdown(manifest, argv))

    if args.collection and args.index:
        rebuild_index(args.collection, timestamp)

    print("java2uml: wrote %d diagrams (%d types, %d relations) to %s"
          % (len(created_diagrams), manifest["summary"]["types"],
             manifest["summary"]["relations"], run_dir))
    return 0


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    if args.command == "generate":
        return cmd_generate(args)
    if args.command == "reindex":
        rebuild_index(args.collection)
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
