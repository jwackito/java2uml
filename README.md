# java2uml

Generate full-detail Mermaid **class diagrams** from Java source code.

`java2uml` is a small, standalone CLI: it walks Java sources, parses them with
[`javalang`](https://github.com/c2nes/javalang), and emits one Mermaid class
diagram per package (configurable) with fields, constructors, methods,
visibility, generics, inheritance, interface realization, field associations
and method-level dependencies.

It does **not** compile the project and does **not** need a classpath.

---

## Requirements

- Python 3.8+
- `javalang==0.13.0` (installed automatically by `run.sh`)

`javalang` understands Java 8 syntax. TuxGuitar targets Java 8 and parses
cleanly. Projects using records, sealed classes, Java modules, switch
expressions or pattern matching will need a different parser.

---

## Quick start

```bash
cd ~/git/java2uml
bash run.sh --help
```

> If `./run.sh` reports `Permission denied`, your filesystem is probably
> mounted `noexec` (common for `/home`). Use `bash run.sh ...` instead; it does
> not require the execute bit.

Generate a collection of diagrams for a checkout:

```bash
bash run.sh generate \
  --collection ~/uml/tuxguitar \
  --src ~/data/analysis/git/tuxguitar-patterns \
  --exclude 'gervill/**' \
  --exclude 'TuxGuitar-android-gervill/**' \
  --group-by module-package \
  --split-threshold 0 \
  --relations full \
  --format both \
  --index \
  --coverage
```

Output:

```
~/uml/tuxguitar/
├── README.md                       # human-readable index of all runs
├── index.json                      # machine-readable index
└── tuxguitar/
    └── 1.6.0/                      # <repo>/<git describe output>
        ├── README.md               # run overview + diagram links
        ├── manifest.json           # full provenance + coverage
        ├── coverage.json           # file -> diagram mapping
        ├── coverage.md             # human coverage report
        └── source/
            └── tuxguitar/
                └── TuxGuitar-lib/
                    └── org/herac/tuxguitar/song/models.mmd
                    └── org/herac/tuxguitar/song/models.md
```

Use `--out DIR` instead of `--collection DIR` for a single flat run.

---

## Why a collection?

You can run the tool over several repositories, or over several tags/checkouts
of the same repository. Each run gets its own directory named after the
repository and the revision (`git describe --tags --always --dirty`).
The collection root keeps a cross-run `README.md` and `index.json`, so both
humans and agents can find every generated revision.

Example: two tags of the same project

```bash
git -C ~/src/app checkout v1.0.0
bash run.sh generate --collection ~/uml/app --src ~/src/app

git -C ~/src/app checkout v2.0.0
bash run.sh generate --collection ~/uml/app --src ~/src/app
```

Result:

```
~/uml/app/app/v1.0.0/
~/uml/app/app/v2.0.0/
~/uml/app/README.md
~/uml/app/index.json
```

If the working tree has modified tracked files, the label gets a `-dirty`
suffix and `manifest.json` records the number of modified and untracked files.
(Untracked files alone do not add `-dirty`, matching `git describe --dirty`.)

Rebuild the collection index without regenerating diagrams:

```bash
bash run.sh reindex --collection ~/uml/app
```

---

## Output contract

Every run directory contains:

| File | Purpose |
| --- | --- |
| `manifest.json` | Complete provenance, options, repositories, sources, diagrams, exclusions and failures. |
| `coverage.json` | Machine-readable mapping of every included Java file to its diagram (`null` when the file declares no types). |
| `coverage.md` | Human summary of the same coverage plus files without types, parse failures and exclusions. |
| `README.md` | Human overview with links to every diagram. |
| `source/...` | One `.mmd` and/or `.md` per diagram. |

### `manifest.json`

```jsonc
{
  "schema_version": 1,
  "generator": {
    "name": "java2uml",
    "version": "1.0.0",
    "parser": "javalang",
    "parser_version": "0.13.0",
    "python": "3.12.14"
  },
  "run": {
    "id": "tuxguitar/1.6.0",
    "timestamp": "2026-11-16T17:30:00Z",
    "argv": ["generate", "..."],
    "options": { "...": "..." },
    "output": "/absolute/path/to/run",
    "metadata": { "ci": "local" },
    "label": "1.6.0"
  },
  "repositories": [
    {
      "name": "tuxguitar",
      "root": "/absolute/repo/root",
      "remote": "https://github.com/helge17/tuxguitar",
      "commit": "9d40a35ff...",
      "branch": "master",
      "describe": "1.6.0",
      "tags": ["1.6.0"],
      "dirty": false,
      "modified_files": 0,
      "untracked_files": 0,
      "source_roots": ["."]
    }
  ],
  "summary": {
    "repositories": 1,
    "source_roots": 1,
    "files_scanned": 2783,
    "files_included": 2525,
    "files_excluded": 258,
    "files_without_types": 4,
    "parse_failures": 0,
    "types": 2654,
    "diagrams": 410,
    "relations": 10180
  },
  "sources": [
    {
      "path": "TuxGuitar-lib/src/org/herac/tuxguitar/song/models/TGSong.java",
      "absolute_path": "/absolute/.../TGSong.java",
      "repo": "tuxguitar",
      "module": "TuxGuitar-lib",
      "package": "org.herac.tuxguitar.song.models",
      "diagram": "source/tuxguitar/TuxGuitar-lib/org/herac/tuxguitar/song/models.mmd",
      "types": 1
    }
  ],
  "diagrams": [
    {
      "repo": "tuxguitar",
      "module": "TuxGuitar-lib",
      "package": "org.herac.tuxguitar.song.models",
      "mmd": "source/.../models.mmd",
      "md": "source/.../models.md",
      "types": 25,
      "relations": 63,
      "external_types": ["org.herac.tuxguitar.song.factory.TGFactory"],
      "unresolved": [],
      "source_files": ["..."],
      "split": false,
      "part": null
    }
  ],
  "exclusions": [{"path": "gervill/...", "reason": "excluded by gervill/**"}],
  "failures": []
}
```

`index.json` at the collection root has the same `schema_version` and a
`runs` array with repository/repositories, revision, commit/commits,
timestamp, counts and the
relative path to each run manifest.

### Provenance inside diagrams

Each `.mmd` starts with comments (after optional Mermaid front matter):

```
%% java2uml 1.0.0 | repo: tuxguitar | revision: 1.6.0 | commit: 9d40a35ff...
%% module: TuxGuitar-lib | package: org.herac.tuxguitar.song.models | types: 25
```

Each `.md` wrapper repeats the same information in a table and links back to
the run README.

---

## CLI reference

```
java2uml.py generate --src DIR [DIR ...] (--collection DIR | --out DIR) [options]
java2uml.py reindex --collection DIR
```

### Source selection

| Option | Description |
| --- | --- |
| `--src DIR ...` | One or more source roots. Repeat the flag or pass several paths. |
| `--include GLOB` | Only include matching paths. Repeatable. |
| `--exclude GLOB` | Exclude matching paths. Repeatable. Defaults exclude `.git`, `target`, `build`, `out`, `node_modules`, `.gradle`, `.idea`, `__pycache__`. |
| `--module-mode {path,pom,none}` | Module naming: first path segment (default), nearest `pom.xml` `artifactId`, or no module. |
| `--module-depth N` | Path-mode depth for module names (default `1`). |

### Output selection

| Option | Description |
| --- | --- |
| `--collection DIR` | Collection root. Output goes to `<collection>/<repo>/<revision>/`. |
| `--out DIR` | Exact output directory for this run (no collection index). |
| `--run-label LABEL` | Override the revision label (default: `git describe --tags --always --dirty`). |
| `--repo-name NAME` | Override the repository name (single repository only). |
| `--metadata KEY=VALUE` | Extra metadata stored in the manifest. Repeatable. |
| `--timestamp ISO-8601` | Override the run timestamp. `SOURCE_DATE_EPOCH` is also honored. |
| `--overwrite` | Replace an existing run directory created by java2uml. |
| `--force` | Allow `--overwrite` for a non-empty directory not created by java2uml. |
| `--index` / `--no-index` | Write the collection index (default: on). |
| `--coverage` / `--no-coverage` | Write coverage reports (default: on). |

### Diagram content

| Option | Description |
| --- | --- |
| `--group-by {package,module-package,repo-module-package}` | Diagram grouping (default `module-package`). |
| `--relations {full,fields,none}` | `full`: inheritance, realization, fields, method dependencies. `fields`: omit method dependencies. `none`: structure only. |
| `--visibility {all,public,protected}` | Filter class members (default `all`). |
| `--external` / `--no-external` | Emit `<<external>>` stubs for referenced out-of-package types (default on). |
| `--split-threshold N` | Split diagrams with more than `N` types. `0` disables splitting (default). |
| `--class-labels {simple,fqn}` | Class title style (default `simple`). |
| `--generic-separator CHAR` | Replacement for commas inside generic arguments (default `;`). |
| `--format {mmd,md,both}` | Output format (default `both`). |
| `--layout {dagre,elk,none}` | Mermaid layout engine (default `dagre`). |
| `--theme NAME` | Mermaid theme name. |
| `--jobs N` | Parallel parser workers (default `min(cpus, 8)`). |
| `--dry-run` | Parse and print the plan without writing files. |
| `--verbose` | Print progress to stderr. |

---

## What is modeled

Per type (classes, interfaces, enums, annotations, nested types):

- kind, package, module, visibility, abstract flag, type parameters, bounds
- type-level annotations
- fields: type, visibility, static/final/transient/volatile
- constructors: parameters, visibility, throws
- methods: parameters (including varargs and generic method type parameters),
  return type, throws, visibility, static/abstract/final/synchronized/native
- enum constants
- nested types

Relationships:

- `Base <|-- Sub` inheritance
- `Interface <|.. Impl` realization
- `A --> "*" B : field` association (`"*"` for collections/arrays, `"1"` otherwise)
- `A ..> B` method parameter/return/throws dependency
- `Outer *-- Inner : nested` nesting

Not modeled: method bodies, initializer blocks, anonymous classes, local
classes, field initializer expressions, annotations on fields/methods/parameters,
and classpath types outside the scanned sources.

Mermaid limitations handled by the generator:

- Mermaid does not support commas inside generic type arguments. `Map<K,V>` is
  rendered as `Map~K;V~` (configurable with `--generic-separator`).
- `<<external>>` stubs represent project types referenced from another
  package. JDK types are ignored.
- Very large packages produce very large diagrams. The tool never splits unless
  you pass `--split-threshold`.

---

## Rendering diagrams

GitHub renders Mermaid fenced blocks in Markdown automatically, so the `.md`
wrappers work without extra tooling. To render images locally:

```bash
npx -y @mermaid-js/mermaid-cli \
  -i source/tuxguitar/TuxGuitar-lib/org/herac/tuxguitar/song/models.mmd \
  -o models.svg
```

The `.mmd` files contain only Mermaid source plus `%%` comments.

---

## Reusing on another project

The tool has no TuxGuitar-specific defaults. Point `--src` at any Java 8
codebase:

```bash
bash run.sh generate \
  --collection ~/uml/other-project \
  --src ~/src/other-project \
  --exclude '**/generated/**' \
  --module-mode pom \
  --group-by module-package
```

If `javalang` cannot parse a file, the file is listed in `manifest.json` under
`failures` and in `coverage.md`; the run continues.

---

## Development

No packaging step. Run directly with the project-local virtualenv:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python java2uml.py --help
```

`run.sh` does this automatically. It calls `python -m pip` instead of the venv
`pip` script, which also works on noexec filesystems such as `/home`.
