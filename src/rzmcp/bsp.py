"""B1c: version and source map of the RZ/G3L BSP (MCP tools get_bsp_release, bsp_source, bsp_grep, patch_stack,
check_build_env). Everything is read-only.

Official sources are read from the git mirrors bitbake keeps in DL_DIR (git show / git grep at the revision the
release's recipe pins): no checkout, no extra disk, and never the work-shared tree, which carries the last build's
PoC patches.
"""
import importlib.util
import re
import subprocess
from pathlib import Path

from . import ROOT
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


def get_bsp_release(name: str = Y.DEFAULT_RELEASE) -> str:
    rel = Y.release(name)
    project = Y.project_dir()
    out = [f"{name}: {rel['title']} — device {rel['device']}, MACHINE {rel['machine']}, {rel['yocto']}",
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
    out += ["", "Proprietary packages (renesas.com, login; not in git):"]
    out += [f"  {p['name']:<9} {p['part']} -> {p['layer']} ({p['needed_for']})" for p in rel["proprietary"]]
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


def bsp_source(comp: str, path: str, start_line: int = 1, max_lines: int = 300) -> str:
    c = component(comp)
    mirror, rev = Path(c["mirror_path"]), c["use_rev"]
    path = _safe_path(path)
    head = f"{comp} @ {rev[:12]} ({c['mirror']}) : {path or '/'}"
    t = _git(mirror, "cat-file", "-t", f"{rev}:{path}")
    if t.returncode != 0:
        return f"{head}\nnot found at this revision. Use bsp_grep or a directory listing (path of the directory)."
    if t.stdout.strip() == "tree":
        r = _git(mirror, "ls-tree", "--name-only", f"{rev}:{path}" if path else rev)
        names = r.stdout.splitlines()
        return f"{head}  (directory, {len(names)} entries)\n" + "\n".join(names[:500])
    r = _git(mirror, "show", f"{rev}:{path}")
    lines = r.stdout.splitlines()
    start = max(1, start_line)
    end = min(len(lines), start + max(1, min(max_lines, 2000)) - 1)
    body = "\n".join(f"{i:>6}  {lines[i - 1]}" for i in range(start, end + 1))
    more = f"\n... {len(lines) - end} more lines (start_line={end + 1})" if end < len(lines) else ""
    return f"{head}  lines {start}-{end} of {len(lines)}\n{body[:MAX_CHARS]}{more}"


def bsp_grep(comp: str, pattern: str, path: str | None = None, limit: int = 60) -> str:
    c = component(comp)
    mirror, rev = Path(c["mirror_path"]), c["use_rev"]
    args = ["grep", "-n", "-I", "-E", "-e", pattern, rev]
    if path:
        args += ["--", _safe_path(path)]
    r = _git(mirror, *args, timeout=60)
    hits = [h.removeprefix(f"{rev}:") for h in r.stdout.splitlines()]
    head = f"{comp} @ {rev[:12]}: {len(hits)} hit(s) for /{pattern}/" + (f" in {path}" if path else "")
    shown = "\n".join(h[:240] for h in hits[:max(1, min(limit, 300))])
    more = f"\n... {len(hits) - limit} more; narrow the path or the pattern" if len(hits) > limit else ""
    return f"{head}\n{shown}{more}" if hits else f"{head}\n(no hits)"


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


def patch_stack(comp: str) -> str:
    project = Y.project_dir()
    c = component(comp, project=project)
    layers = active_layers(project)
    provided = []                                   # (layer, file, subject)
    for layer in layers:
        d = layer / PATCH_DIRS[comp]
        if d.is_dir():
            for p in sorted(d.rglob("*")):
                if p.suffix in (".patch", ".cfg", ".scc") and p.is_file():
                    provided.append((layer, p, *(_patch_meta(p) if p.suffix == ".patch" else (None, frozenset()))))
    out = [f"{comp}: official rev {c['use_rev'][:12]}; files the active layers (build/conf/bblayers.conf) provide in "
           f"{PATCH_DIRS[comp]}/ — whether a bbappend applies them can depend on overrides:"]
    by_layer = {}
    for layer, p, s, _ in provided:
        by_layer.setdefault(layer, []).append((p, s))
    for layer, items in by_layer.items():
        out.append(f"\n[{layer.name}] {Y.owner_of(layer, project)}")
        out += [f"  {p.relative_to(layer)}" + (f"  — {s}" if s else "") for p, s in items]
    work = project / "build" / "tmp" / c["work"]
    if comp == "kernel" and (work / ".git").exists():
        r = subprocess.run(["git", "-C", str(work), "log", "--reverse", "--format=%h %s", f"{c['use_rev']}..HEAD"],
                           capture_output=True, text=True, timeout=30)
        commits = r.stdout.splitlines()
        subj = {s: (layer, p) for layer, p, s, _ in provided if s}
        byfiles = {fs: (layer, p) for layer, p, _, fs in provided if fs}
        out.append(f"\nApplied in the last kernel build ({work.relative_to(project)}): {len(commits)} commit(s) on top "
                   f"of {c['use_rev'][:12]}; each traced by subject, else by the files it changes")
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
    elif comp != "kernel":
        out.append(f"\n(The last build's {comp} source tree is not kept: add the recipe to RM_WORK_EXCLUDE "
                   "(workflow/build/poc-use does) to see what was applied.)")
    return "\n".join(out)


def check_build_env() -> str:
    project = Y.project_dir()
    build = project / "build"
    rel = Y.release()
    out = [f"Yocto project {project} vs {Y.DEFAULT_RELEASE}"]

    def item(level, text):
        out.append(f"  {level:<4} {text}")

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
    return "\n".join(out)
