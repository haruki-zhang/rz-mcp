"""B1c: version and source map of the RZ/G3L BSP (MCP tools get_bsp_release, bsp_source, bsp_grep, patch_stack,
check_build_env). Everything is read-only.

Official sources are read from the git mirrors bitbake keeps in DL_DIR (git show / git grep): no checkout, and never
the work-shared tree, which carries the last build's PoC patches. With a snapshot (workflow/build/src-snapshot,
rzmcp.snapshot) the default view is "release": the revision plus the patches the official configuration applies
(meta-renesas and installed optional components), byte-identical to bitbake do_patch; more components (out-of-tree
modules, tarball sources, layers), the official build's .config / compiled objects / DT, and resolved recipes.
"""
import importlib.util
import re
import subprocess
from pathlib import Path

from . import ROOT
from . import snapshot as S
from . import yocto as Y

COMPONENTS = ("kernel", "u-boot", "tf-a")
PATCH_DIRS = {"kernel": "recipes-kernel/linux", "u-boot": "recipes-bsp/u-boot", "tf-a": "recipes-bsp/trusted-firmware-a"}
MAX_CHARS = 30000


def _media():
    spec = importlib.util.spec_from_file_location("media", ROOT / "workflow" / "board" / "media.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _git(mirror: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", f"--git-dir={mirror}", *args], capture_output=True, text=True, timeout=timeout)


def _project(project: str | Path | None) -> Path:
    """The project a tool reads: an explicit absolute directory, else the default one."""
    p = Y.resolve_project(project)
    if not p.is_dir():
        raise ValueError(f"no such Yocto project directory: {p}")
    return p


def component(name: str, rel: dict | None = None, project: Path | None = None) -> dict:
    """The component as pinned by the project's meta-renesas, compared with the release catalog."""
    if name not in COMPONENTS:
        raise ValueError(f"component must be one of {', '.join(COMPONENTS)}")
    rel = rel or Y.release()
    project = project or Y.project_dir()
    c = dict(rel["components"][name])
    recipe = project / "meta-renesas" / c["recipe"]
    local = Y.recipe_var(recipe, c["rev_var"]) if recipe.exists() else None
    mirror = Y.dl_dir(project) / "git2" / c["mirror"]
    has = mirror.exists() and _git(mirror, "cat-file", "-e", f"{local or c['rev']}^{{commit}}").returncode == 0
    c.update(local_rev=local, mirror_path=str(mirror), mirror_has_rev=has,
             local_extra={v: Y.recipe_var(recipe, v) for v in c.get("extra_vars", [])} if recipe.exists() else {})
    c["use_rev"] = local or c["rev"]
    return c


def get_bsp_release(name: str = Y.DEFAULT_RELEASE, project: str | Path | None = None) -> str:
    rel = Y.release(name)
    project = _project(project)
    out = [f"{name}: {rel['title']} — device {rel['device']}, MACHINE {rel['machine']}, {rel['yocto']}",
           f"Checked against the Yocto project {project}",
           f"Source: {rel['source']}", f"Note: {rel['note']}", "", "Layers (git):"]
    for l in rel["layers"]:
        out.append(f"  {l['name']:<18} {l['url']}  {'tag ' + l['tag'] + ' = ' if 'tag' in l else ''}{l['rev']}")
    out += [f"TEMPLATECONF: {rel['template']}", "", "Components (pinned for rzg3l-family by meta-renesas):"]
    for n in COMPONENTS:
        c = component(n, rel, project)
        same = "" if c["local_rev"] in (None, c["rev"]) else f"  !! the project's meta-renesas pins {c['local_rev']}"
        extra = ", ".join(f"{k}={v}" for k, v in c["local_extra"].items() if v)
        out.append(f"  {n:<7} rev {c['rev']}{(', ' + extra) if extra else ''}{same}")
        out.append(f"          recipe meta-renesas/{c['recipe']}; mirror {c['mirror']} "
                   f"({'has the revision' if c['mirror_has_rev'] else 'MISSING the revision: run a build once'})")
    out += ["", "Optional components (installed into a project only when its configuration names one of their "
            "layers: workflow/build/yocto-project add, or poc-use):"]
    for c in Y.optional_components(rel):
        what = (f"{c['part']} {c['version']} (licensed package, rz-mcp/{c['file']}, sha256 {c['sha256'][:12]}...)"
                if c["kind"] == "package" else f"git {c['url']} tag {c['tag']} = {c['rev'][:12]}")
        out.append(f"  {c['name']:<9} {what} -> {', '.join(c['layers'])}; {c['needed_for']}")
    m = _media()
    out += ["", "Boot media, Flash Writer " + m.FLASH_WRITER + " (from workflow/board/media.py):",
            "  eMMC boot partition 1 (EM_W): " + "; ".join(f"{f.role} {f.srec} sector {f.dest:#x} load {f.prog:#x}" for f in m.EMMC),
            "  EXT_CSD: " + ", ".join(f"{i:#X}={v:#04x}" for i, v in m.EMMC_EXT_CSD),
            "  QSPI (XLS2): " + "; ".join(f"{f.role} {f.srec} at {f.dest:#x} load {f.prog:#x}" for f in m.QSPI),
            "  SW_MODE[1:3]: " + ", ".join(f"{k} {v}" for k, v in m.SW_MODE.items()),
            "  ({cm33} = the CM33 firmware of the image, e.g. lpdemo-cm33-fw-smarc-rzg3l.srec)",
            "", "Use bsp_source / bsp_grep to read the official sources at these revisions, patch_stack for what the "
            "layers add on top, check_build_env for how the Yocto project differs from this release."]
    return "\n".join(out)


def _safe_path(path: str) -> str:
    path = (path or "").strip().lstrip("/")
    if path.startswith("-") or ".." in Path(path).parts:
        raise ValueError("path must be relative to the repository root, without '..'")
    return path


VIEWS = ("release", "rev")
NO_SNAPSHOT = ("no source snapshot for this project: run workflow/build/src-snapshot --project {p} (about 2 minutes; "
               "add --artifacts for .config / compiled objects / DT)")


class Source:
    """Where a component's view lives: a git repository and a tree, plus what to say about it."""

    def __init__(self, repo, tree, head, comp=None, notes=()):
        self.repo, self.tree, self.head, self.comp, self.notes = Path(repo), tree, head, comp or {}, list(notes)


def source(comp: str, project: str | Path | None = None, view: str = "release") -> Source:
    if view not in VIEWS:
        raise ValueError(f"view must be one of {', '.join(VIEWS)}")
    project = _project(project)
    snap = S.load(project)
    if snap and comp in snap["components"]:
        c = snap["components"][comp]
        if "rev_tree" not in c:
            raise ValueError(f"{comp} ({c.get('recipe')}): {c.get('status', 'no source')}")
        d = Path(snap["_dir"])
        repo = c["repo_path"] if c.get("repo_path") else d / ("views.git" if c.get("repo") == "views" else "local.git")
        tree = c["release_tree"] if view == "release" else c["rev_tree"]
        if c["kind"] == "git":
            what = f"{c['recipe']} rev {c['rev'][:12]}" + (f" ({c['subdir']})" if c.get("subdir") else "")
        elif c["kind"] == "tar":
            what = f"{c['recipe']} {Path(c['tarball']).name}" + (f" ({c['subdir']})" if c.get("subdir") else "")
        else:
            what = f"layer {comp} " + (f"rev {c['rev'][:12]}" if c.get("rev") else "(imported from disk: not a git checkout)")
        n = len(c.get("patches", []))
        if c["kind"] != "layer":
            what += (f" + {n} official patch(es)" if n else " (no patches)") if view == "release" else " [bare, no layer patches]"
        notes = []
        st = S.stale(snap)
        if st:
            notes.append(f"WARNING: snapshot of {snap['created']} is stale ({'; '.join(st[:3])}): re-run workflow/build/src-snapshot")
        return Source(repo, tree, f"{comp} [{view}] {what}", c, notes)
    if comp not in COMPONENTS:
        avail = sorted(snap["components"]) if snap else list(COMPONENTS)
        raise ValueError(f"unknown component {comp!r}; available: {', '.join(avail)}"
                         + ("" if snap else "  (" + NO_SNAPSHOT.format(p=project) + ")"))
    c = component(comp, project=project)
    notes = []
    if view == "release":
        notes.append("NOTE: " + NO_SNAPSHOT.format(p=project) + ". Showing the bare revision: patches that meta-renesas "
                     "or optional components apply are NOT included (patch_stack lists candidates).")
    return Source(c["mirror_path"], c["use_rev"] + "^{tree}", f"{comp} [rev] {c['use_rev'][:12]} ({c['mirror']})", c, notes)


def _open_block(lines: list[str], end: int) -> tuple[int, str] | None:
    """For C-like text: the outermost block still open after line `end` (1-based), as (line, text)."""
    stack, in_comment = [], False
    for i in range(end):
        s = lines[i]
        s = re.sub(r'"(\\.|[^"\\])*"|\'(\\.|[^\'\\])*\'', '""', s)
        if in_comment:
            if "*/" not in s:
                continue
            s, in_comment = s.split("*/", 1)[1], False
        s = re.sub(r"/\*.*?\*/", "", s)
        if "/*" in s:
            s, in_comment = s.split("/*", 1)[0], True
        s = s.split("//", 1)[0]
        for ch in s:
            if ch == "{":
                stack.append(i)
            elif ch == "}" and stack:
                stack.pop()
    if not stack:
        return None
    first = stack[0]
    sig = first
    while sig > 0 and lines[sig].strip() in ("{", "") :
        sig -= 1
    return sig + 1, lines[sig].strip()


def bsp_source(comp: str, path: str, start_line: int = 1, max_lines: int = 300, project: str | Path | None = None,
               view: str = "release") -> str:
    src = source(comp, project, view)
    path = _safe_path(path)
    head = f"{src.head} : {path or '/'}"
    notes = "".join(n + "\n" for n in src.notes)
    obj = f"{src.tree}:{path}" if path else src.tree
    t = S.git(src.repo, "cat-file", "-t", obj, check=False).strip()
    if not t:
        return f"{notes}{head}\nnot found in this view. Use bsp_grep or list the parent directory."
    if t == "tree":
        names = S.git(src.repo, "ls-tree", "--name-only", obj).splitlines()
        return f"{notes}{head}  (directory, {len(names)} entries)\n" + "\n".join(names[:500])
    changed = src.comp.get("changed", {}).get(path) if view == "release" else None
    if changed:
        head += f"  (changed by the official patch(es): {', '.join(changed)})"
    lines = S.git(src.repo, "cat-file", "blob", obj).splitlines()
    start = max(1, start_line)
    end = min(len(lines), start + max(1, min(max_lines, 2000)) - 1)
    out, size = [], 0                       # stop at MAX_CHARS on a line boundary: the header must tell the truth
    for i in range(start, end + 1):
        row = f"{i:>6}  {lines[i - 1]}"
        if out and size + len(row) + 1 > MAX_CHARS:
            end = i - 1
            break
        out.append(row)
        size += len(row) + 1
    body = "\n".join(out)
    more = ""
    if end < len(lines):
        more = f"\n... {len(lines) - end} more lines (start_line={end + 1})"
        if path.endswith(S.C_LIKE):
            ob = _open_block(lines, end)
            if ob:
                more += (f"\n!! The window ends INSIDE a block opened at line {ob[0]} (`{ob[1][:80]}`): what follows can "
                         f"change its meaning. Read on (start_line={end + 1}) before concluding what this code does.")
    return f"{notes}{head}  lines {start}-{end} of {len(lines)}\n{body}{more}"


def _pathspecs(src: Source, path: str | None) -> tuple[list[str], list[str]]:
    """Validate the path argument (several may be given, separated by spaces). A path that does not exist is either
    taken as a prefix of existing entries or reported: a misspelt path must never look like 'no hits'."""
    if not path:
        return [], []
    specs, notes = [], []
    for p in path.split():
        p = _safe_path(p)
        if any(ch in p for ch in "*?["):
            specs.append(p)
            continue
        if S.exists(src.repo, f"{src.tree}:{p}".rstrip("/")) or not p:
            specs.append(p)
            continue
        parent, _, base = p.rstrip("/").rpartition("/")
        ok_parent = not parent or S.exists(src.repo, f"{src.tree}:{parent}")
        names = S.git(src.repo, "ls-tree", "--name-only", f"{src.tree}:{parent}" if parent else src.tree).splitlines() if ok_parent else []
        cand = [f"{parent}/{n}" if parent else n for n in names if n.startswith(base)]
        if cand:
            specs += cand
            notes.append(f"'{p}' is not a path in this view; used as a prefix of {len(cand)} entr{'y' if len(cand) == 1 else 'ies'}: "
                         + ", ".join(cand[:8]) + (" ..." if len(cand) > 8 else ""))
        else:
            near = parent
            while near and not S.exists(src.repo, f"{src.tree}:{near}"):
                near = near.rpartition("/")[0]
            raise ValueError(f"path '{p}' does not exist in {src.head} (nearest existing directory: '{near or '/'}'). "
                             "Nothing was searched.")
    return specs, notes


def bsp_grep(comp: str, pattern: str, path: str | None = None, limit: int = 60, project: str | Path | None = None,
             view: str = "release") -> str:
    src = source(comp, project, view)
    specs, pnotes = _pathspecs(src, path)
    args = ["grep", "-n", "-I", "-E", "-e", pattern, src.tree] + (["--", *specs] if specs else [])
    r = S.git(src.repo, *args, check=False)
    tree = src.tree
    hits = [h.split(":", 1)[1] if h.startswith(tree.split("^")[0]) else h for h in r.splitlines()]
    hits = [h.removeprefix(f"{tree}:") for h in hits]
    lim = max(1, min(limit, 300))
    head = f"{src.head}: {len(hits)} hit(s) for /{pattern}/" + (f" in {' '.join(specs)}" if specs else "")
    shown = "\n".join(h[:240] for h in hits[:lim])
    more = f"\n... {len(hits) - lim} more; narrow the path or the pattern" if len(hits) > lim else ""
    notes = "".join(n + "\n" for n in src.notes + pnotes)
    return f"{notes}{head}\n{shown}{more}" if hits else f"{notes}{head}\n(no hits)"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _patch_meta(p: Path) -> tuple[str | None, frozenset]:
    """(subject, files) of a patch file. The Subject header can be folded over several lines (RFC 2822)."""
    subject, files, in_subj = None, set(), False
    try:
        with open(p, errors="replace") as f:
            for line in f:
                if subject is None and line.startswith("Subject:"):
                    subject, in_subj = line[len("Subject:"):], True
                    continue
                if in_subj and line[:1] in (" ", "\t"):
                    subject += line
                    continue
                in_subj = False
                if line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
                    files.add(line[4:].strip().split("\t")[0].removeprefix("b/"))
    except OSError:
        pass
    if subject is not None:
        subject = _norm(re.sub(r"^\s*(\[[^\]]*\]\s*)?", "", subject))
    return subject, frozenset(files)


def active_layers(project: Path) -> list[Path]:
    return Y.bblayers(project / "build" / "conf" / "bblayers.conf", project / "build")


def _provided(project: Path, comp: str, layers: list[Path]) -> list:
    out = []                                        # (layer, file, subject, files)
    for layer in layers:
        d = layer / PATCH_DIRS.get(comp, "-")
        if d.is_dir():
            for p in sorted(d.rglob("*")):
                if p.suffix in (".patch", ".cfg", ".scc") and p.is_file():
                    out.append((layer, p, *(_patch_meta(p) if p.suffix == ".patch" else (None, frozenset()))))
    return out


def _kernel_trace(project: Path, c: dict, provided: list) -> list[str]:
    work = project / "build" / "tmp" / c["work"]
    if not (work / ".git").exists():
        return []
    r = subprocess.run(["git", "-C", str(work), "log", "--reverse", "--format=%h %s", f"{c['use_rev']}..HEAD"],
                       capture_output=True, text=True, timeout=30)
    commits = r.stdout.splitlines()
    subj = {s: (layer, p) for layer, p, s, _ in provided if s}
    byfiles = {fs: (layer, p) for layer, p, _, fs in provided if fs}
    out = [f"\nApplied in this project's LAST kernel build ({work.relative_to(project)}, includes PoC layers): "
           f"{len(commits)} commit(s) on top of {c['use_rev'][:12]}; each traced by subject, else by the files it changes"]
    for line in commits:
        h, _, s = line.partition(" ")
        src = subj.get(_norm(s))
        how = "subject"
        if src is None:
            fs = subprocess.run(["git", "-C", str(work), "show", "--name-only", "--format=", h],
                                capture_output=True, text=True, timeout=30).stdout.split()
            src, how = byfiles.get(frozenset(fs)), "files"
        where = f"{src[0].name}/{src[1].name}" + (" (by files)" if how == "files" else "") if src else "NOT FOUND in the active layers"
        out.append(f"  {h} {s or '(no subject: a plain diff)'}  <- {where}")
    return out


def patch_stack(comp: str, project: str | Path | None = None) -> str:
    project = _project(project)
    snap = S.load(project)
    layers = active_layers(project)
    if snap and comp in snap["components"] and "rev_tree" in snap["components"][comp]:
        sc = snap["components"][comp]
        base = f"rev {sc['rev'][:12]}" if sc.get("rev") else (Path(sc["tarball"]).name if sc.get("tarball") else "")
        out = [f"{comp} ({sc['recipe']}): what the OFFICIAL configuration applies on top of {base} — the recipe's resolved "
               f"SRC_URI (bitbake -e, snapshot {snap['created']}; release layers + optional components "
               f"{', '.join(snap['optional']) or 'none'}). This is exactly the 'release' view of bsp_source / bsp_grep."]
        for n in S.stale(snap):
            out.append(f"WARNING: snapshot stale: {n}")
        if not sc.get("patches"):
            out.append("  patches: none (the release view equals the bare revision)")
        else:
            out.append(f"  patches ({len(sc['patches'])}, in apply order):")
            files_by_patch = {}
            for f, ps in sc.get("changed", {}).items():
                for pn in ps:
                    files_by_patch.setdefault(pn, []).append(f)
            for p in sc["patches"]:
                subj = _patch_meta(Path(p["path"]))[0] if p.get("path") else None
                fl = files_by_patch.get(p["name"], [])
                out.append(f"    {p['name']}  [{p.get('layer')}]" + (f" — {subj}" if subj else ""))
                out.append(f"        changes: {', '.join(fl[:6])}" + (" ..." if len(fl) > 6 else ""))
        if sc.get("fragments"):
            out.append("  config fragments merged into the configuration (not source changes):")
            out += [f"    {f['name']}  [{f.get('layer')}]" for f in sc["fragments"]]
        official = {Path(l) for l in snap["layers"]}
        extra = [x for x in _provided(project, comp, [l for l in layers if l not in official])]
        if extra:
            out.append("\nNot part of the official view — PoC / extra layers in this project's bblayers.conf provide "
                       f"(applied only in this project's own builds):")
            for layer, p, s, _ in extra:
                out.append(f"    {p.relative_to(layer)}  [{layer.name}, {Y.owner_of(layer, project)}]" + (f" — {s}" if s else ""))
        if comp == "kernel":
            out += _kernel_trace(project, component(comp, project=project), _provided(project, comp, layers))
        return "\n".join(out)
    if comp not in COMPONENTS:
        raise ValueError(f"{comp}: not in a snapshot. " + NO_SNAPSHOT.format(p=project))
    c = component(comp, project=project)
    provided = _provided(project, comp, layers)
    out = ["NOTE: " + NO_SNAPSHOT.format(p=project) + ". Without it this lists every patch FILE the layers contain — "
           "most are NOT applied for rzg3l (other machines, overrides, other recipe versions). Do not read it as the "
           "applied list.",
           f"{comp}: official rev {c['use_rev'][:12]}; files the active layers (build/conf/bblayers.conf) provide in "
           f"{PATCH_DIRS[comp]}/:"]
    by_layer = {}
    for layer, p, s, _ in provided:
        by_layer.setdefault(layer, []).append((p, s))
    for layer, items in by_layer.items():
        out.append(f"\n[{layer.name}] {Y.owner_of(layer, project)}")
        out += [f"  {p.relative_to(layer)}" + (f"  — {s}" if s else "") for p, s in items]
    if comp == "kernel":
        out += _kernel_trace(project, c, provided)
    else:
        out.append(f"\n(The last build's {comp} source tree is not kept: add the recipe to RM_WORK_EXCLUDE "
                   "(workflow/build/poc-use does) to see what was applied.)")
    return "\n".join(out)


# ---------------------------------------------------------------- the official build: .config, objects, DT

def _artifacts(project: Path) -> tuple[dict, Path, str]:
    snap = S.load(project)
    if not snap:
        raise ValueError(NO_SNAPSHOT.format(p=project))
    if not snap.get("artifacts", {}).get("kernel"):
        raise ValueError(f"the snapshot has no build artifacts: run workflow/build/src-snapshot --project {project} --artifacts")
    d = Path(snap["_dir"])
    head = (f"OFFICIAL build of {snap['release']} + optional components {', '.join(snap['optional']) or 'none'} "
            f"(snapshot {snap['created']}; built in a scratch directory, no PoC layer) — not this project's own build")
    st = S.stale(snap)
    if st:
        head += f"\nWARNING: snapshot stale ({'; '.join(st[:3])}): re-run workflow/build/src-snapshot --artifacts"
    return snap, d, head


def _config(path: Path) -> dict:
    d = {}
    for line in path.read_text().splitlines():
        m = re.match(r"(CONFIG_\w+)=(.*)", line) or re.match(r"# (CONFIG_\w+) is not set", line)
        if m:
            d[m.group(1)] = m.group(2) if m.lastindex == 2 else "is not set"
    return d


def _dt_nodes(text: str) -> list[dict]:
    """Nodes of a decompiled DT: path, labels, compatible, status, the node's own text (without children)."""
    nodes, stack = [], []
    for i, line in enumerate(text.splitlines()):
        s = line.strip()
        m = re.match(r"((?:[\w-]+:\s*)*)([\w@,.+\-]+|/)\s*\{$", s)
        if m:
            labels = re.findall(r"([\w-]+):", m.group(1))
            path = "/" if m.group(2) == "/" else ("/".join([stack[-1]["path"].rstrip("/"), m.group(2)]) if stack else m.group(2))
            stack.append({"path": path, "labels": labels, "props": [], "line": i + 1, "compatible": [], "status": None})
            continue
        if s == "};" and stack:
            nodes.append(stack.pop())
            continue
        if stack and s.endswith(";"):
            stack[-1]["props"].append(s)
            if s.startswith("compatible ="):
                stack[-1]["compatible"] = re.findall(r'"([^"]+)"', s)
            mm = re.match(r'status = "(\w+)";', s)
            if mm:
                stack[-1]["status"] = mm.group(1)
    return nodes


def build_facts(kind: str, query: str, component: str = "kernel", project: str | Path | None = None) -> str:
    """kind: config (symbol regex) | compiled (path / regex of source files) | dt (label, node name, path or compatible)."""
    project = _project(project)
    snap, d, head = _artifacts(project)
    a = snap["artifacts"]
    q = query.strip()
    if kind == "config":
        if component == "kernel":
            cfgs = {"kernel": d / a["kernel"]["config"]}
        elif component == "u-boot":
            cfgs = {Path(c).stem: d / c for c in a.get("u-boot", {}).get("config", [])}
            if not cfgs:
                raise ValueError("no U-Boot .config in the snapshot")
        else:
            raise ValueError("config: component must be kernel or u-boot")
        rx = re.compile(q if q.startswith("CONFIG_") or not re.fullmatch(r"[A-Za-z0-9_]+", q) else f"CONFIG_{q}", re.I)
        out = [head, f"{component} .config, symbols matching /{rx.pattern}/:"]
        for name, f in cfgs.items():
            cfg = _config(f)
            hits = [(k, v) for k, v in cfg.items() if rx.search(k)]
            if len(cfgs) > 1:
                out.append(f"[{name}]")
            out += [f"  {k}={v}" if v != "is not set" else f"  # {k} is not set" for k, v in hits[:200]]
            if not hits:
                out.append("  (no such symbol in .config: it does not exist for this architecture / is hidden by a "
                           "disabled dependency, i.e. effectively n)")
            elif len(hits) > 200:
                out.append(f"  ... {len(hits) - 200} more")
        return "\n".join(out)
    if kind == "compiled":
        objs = (d / a["kernel"]["objects"]).read_text().split()
        mods = {Path(x).stem for x in (d / a["kernel"]["modules"]).read_text().split()}
        if re.fullmatch(r"[\w./+-]+\.(c|S)", q):
            o = re.sub(r"\.(c|S)$", ".o", q)
            src = source("kernel", project, "release")
            exists = S.exists(src.repo, f"{src.tree}:{q}")
            yes = o in objs
            out = [head, f"{q}: " + ("COMPILED" if yes else "NOT compiled") + (" (file exists in the source)" if exists else
                   " (no such file in the kernel source)")]
            if yes:
                kos = [m for m in mods if Path(o).stem == m or Path(o).parent.name == m]
                out.append("  part of a loadable module" if kos else "  built into the kernel image or a module object "
                           "(see modules.order)")
            return "\n".join(out)
        rx = re.compile(q)
        hits = [x for x in objs if rx.search(x)]
        return "\n".join([head, f"compiled objects matching /{q}/: {len(hits)} (of {len(objs)})"]
                          + [f"  {x}" for x in hits[:200]] + ([f"  ... {len(hits) - 200} more"] if len(hits) > 200 else []))
    if kind == "dt":
        out = [head]
        for name in a["kernel"]["dtb"]:
            text = (d / "kernel" / f"{name}.dts").read_text()
            nodes = _dt_nodes(text)
            ql = q.lstrip("&")
            hits = [n for n in nodes if ql in n["labels"] or n["path"] == q or n["path"].rsplit("/", 1)[-1] == ql
                    or n["path"].rsplit("/", 1)[-1].split("@")[0] == ql or any(ql == c or (len(ql) > 4 and ql in c) for c in n["compatible"])]
            srcs = (d / "kernel" / f"{name}.sources").read_text().split() if (d / "kernel" / f"{name}.sources").exists() else []
            out.append(f"{name} (compiled; sources {', '.join(Path(s).name for s in srcs if s.endswith(('.dts', '.dtsi')))}): "
                       f"{len(hits)} node(s) for '{q}'")
            for n in hits[:10]:
                st = n["status"] or "okay (no status property = enabled)"
                out.append(f"\n  {n['path']}" + (f"  labels: {', '.join(n['labels'])}" if n["labels"] else "")
                           + f"\n  STATUS: {st}")
                out += [f"    {p[:200]}" for p in n["props"][:40]]
            if len(hits) > 10:
                out.append(f"  ... {len(hits) - 10} more; be more specific")
        out.append("\n(The compiled tree is final: includes, &label overrides and #if switches are already resolved.)")
        return "\n".join(out)
    raise ValueError("kind must be config, compiled or dt")


def bsp_recipe(recipe: str, pattern: str, project: str | Path | None = None, history: bool = True) -> str:
    """Resolved variables of a recipe in the official configuration (bitbake -e), with where each was set."""
    project = _project(project)
    snap = S.load(project)
    if not snap:
        raise ValueError(NO_SNAPSHOT.format(p=project))
    text = S.env_text(snap, recipe)
    if text is None:
        have = sorted(r for r, v in snap["recipes"].items() if v == "ok")
        raise ValueError(f"recipe {recipe!r} is not in the snapshot; available: {', '.join(have)}")
    rx = re.compile(pattern)
    out = [f"{recipe}: resolved by bitbake -e in the OFFICIAL configuration (snapshot {snap['created']}; release layers + "
           f"optional components {', '.join(snap['optional']) or 'none'}; no PoC layer). '#' lines: where it was set"]
    n = 0
    # bitbake -e: "#\n# $VAR [k operations]\n#   <op> <file>:<line>\n#     <value>\n...\n[export ]VAR=\"value\""
    for block in re.split(r"\n#\n", text):
        m = re.search(r'^(?:export )?([A-Za-z0-9_:\-${}.+~]+)="', block, re.M)
        if not m or not rx.search(m.group(1)):
            continue
        n += 1
        if n > 40:
            out.append("... more variables match; narrow the pattern")
            break
        lines = block.splitlines()
        if history:
            ops = [l for l in lines if re.match(r"#   \S", l) and not l.startswith("#   \"")]
            ops = [l.replace(str(project) + "/", "") for l in ops if "documentation.conf" not in l]
            out += [o[:220] for o in ops[:12]] + (["#   ..."] if len(ops) > 12 else [])
        val = "\n".join(l for l in lines[lines.index(next(l for l in lines if l.startswith(m.group(0)[:len(m.group(0))]) )):]
                        if not l.startswith("#"))
        out.append(val[:3000] + ("..." if len(val) > 3000 else ""))
        out.append("")
    if n == 0:
        out.append(f"(no variable matches /{pattern}/)")
    return "\n".join(out)


def check_build_env(project: str | Path | None = None) -> str:
    project = _project(project)
    build = project / "build"
    rel = Y.release()
    out = [f"Yocto project {project} vs {Y.DEFAULT_RELEASE}"]

    def item(level, text):
        out.append(f"  {level:<4} {text}")

    a = Y.assess(project)
    item({"usable": "OK", "unusable": "FAIL"}.get(a["state"], "WARN"), f"project state: {a['state']}"
         + (" (workflow/build/yocto-project setup continues it)" if a["state"] == "incomplete" else ""))
    for p_ in a["problems"]:
        item("FAIL", p_)
    if a["state"] != "usable":
        return "\n".join(out)
    if not (build / "conf" / "local.conf").exists():
        item("FAIL", "no build/conf/local.conf: run workflow/build/yocto-project setup " + str(project))
        return "\n".join(out)
    for c in Y.optional_components(rel):
        v = a["components"].get(c["name"])
        item("INFO", f"optional {c['name']}: {v or 'not installed'}")

    conf = build / "conf"
    local = Y.statements((conf / "local.conf").read_text())
    used = (conf / "templateconf.cfg").read_text().strip() if (conf / "templateconf.cfg").exists() else "?"
    official = project / rel["template"]
    if Path(used).resolve() == official.resolve():
        item("OK", f"TEMPLATECONF {rel['template']}")
    else:
        same = (Path(used) / "local.conf.sample").exists() and Y.statements((Path(used) / "local.conf.sample").read_text()) \
            == Y.statements((official / "local.conf.sample").read_text())
        item("OK" if same else "WARN", f"TEMPLATECONF {used} " +
             ("(differs from the official template only in comments)" if same else f"!= official {rel['template']}"))
    for var, want in (("MACHINE", rel["machine"]), ("DISTRO", None)):
        v = Y.conf_value(local, var)
        if want:
            item("OK" if v == want else "WARN", f"{var} = {v}")
        else:
            defs = [p for l in active_layers(project) for p in [l / "conf" / "distro" / f"{v}.conf"] if p.exists()]
            item("OK" if v == "rz-vlp" else "INFO", f"{var} = {v}" + (f" (defined in {defs[0].parent.parent.parent.name})" if defs else ""))
    tmpl = [p for p in Y.bblayers(official / "bblayers.conf.sample", build)]
    have = active_layers(project)
    missing = [p for p in tmpl if p not in have]
    item("OK" if not missing else "FAIL", "official layers present" if not missing else f"official layers missing: {missing}")
    for p in have:
        if p not in tmpl:
            item("INFO", f"extra layer {p.relative_to(project) if p.is_relative_to(project) else p}  [{Y.owner_of(p, project)}]")
    keep = (Y.conf_value(local, "RM_WORK_EXCLUDE") or "").split()
    lost = [r for r in ("linux-renesas", "u-boot", "trusted-firmware-a") if r not in keep]
    if "rm_work" in " ".join(local):
        item("OK" if not lost else "WARN", "RM_WORK_EXCLUDE keeps " + (" ".join(keep) or "nothing") +
             (f"; rm_work removes the work directories of {', '.join(lost)} after each build (poc-use adds them)" if lost else ""))
    for l in rel["layers"]:
        d = project / l["name"]
        if (d / ".git").exists():
            head = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            item("OK" if head == l["rev"] else "WARN", f"{l['name']} at {head[:12]} (release {l['rev'][:12]})")
        else:
            item("INFO", f"{l['name']}: not a git checkout (from a tarball?): revision not verifiable here")
    for n in COMPONENTS:
        c = component(n, rel, project)
        ok = c["local_rev"] == c["rev"] and c["mirror_has_rev"]
        item("OK" if ok else "WARN", f"{n}: meta-renesas pins {c['local_rev'] and c['local_rev'][:12]} "
             f"(release {c['rev'][:12]}); mirror {'has it' if c['mirror_has_rev'] else 'missing'}")
    item("INFO" if not Y.bitbake_busy(build) else "WARN", "bitbake " + ("is running" if Y.bitbake_busy(build) else "not running"))
    snap = S.load(project)
    if not snap:
        item("WARN", "no source snapshot (bsp_source shows only bare kernel/U-Boot/TF-A revisions; no build_facts / "
                     f"bsp_recipe): workflow/build/src-snapshot --project {project} --artifacts")
    else:
        st = S.stale(snap)
        served = sorted(k for k, v in snap["components"].items() if "rev_tree" in v)
        item("WARN" if st else "OK", f"source snapshot {snap['created']}: {len(served)} components"
             + (", build artifacts" if snap.get("artifacts", {}).get("kernel") else ", NO build artifacts (--artifacts)")
             + (f"; STALE: {'; '.join(st[:3])}" if st else ""))
    return "\n".join(out)
