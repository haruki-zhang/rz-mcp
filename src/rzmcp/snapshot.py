"""Source snapshot of a Yocto project: what the official release (plus the optional components installed in the
project, minus every PoC layer) actually compiles. Written by workflow/build/src-snapshot, read by rzmcp.bsp.

data/snapshots/<slug>/
  snapshot.json   components (git / tar / binary / layer), their rev tree and release tree, applied patches,
                  config fragments, layer fingerprints (staleness), artifact paths
  views.git       bare; objects/info/alternates -> the project's DL_DIR git mirrors. Holds ONLY the objects the
                  official patches create: existing objects are never re-written, so git never "freshens" (touches)
                  the project's packs. Nothing in the project is written.
  local.git       bare, no alternates: tarball sources and non-git layers imported from disk, and their views
  env/<recipe>.env.gz     `bitbake -e <recipe>` of the official configuration
  kernel/, u-boot/        build artifacts of the official configuration (.config, compiled objects, DT)

Views (what bsp_source / bsp_grep read):
  release  the source as the official build compiles it: rev (or tarball) + the patches the recipe's resolved
           SRC_URI applies (meta-renesas and installed optional components). Byte-identical to bitbake do_patch
           (checked 2026-11-26 for kernel, U-Boot, TF-A, Mali).
  rev      the bare upstream revision / tarball, no layer patches.
"""
import gzip
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
from pathlib import Path

from . import ROOT

SNAP_ROOT = ROOT / "data" / "snapshots"
C_LIKE = (".c", ".h", ".S", ".dts", ".dtsi", ".cpp", ".cc")


def slug(project: Path) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(Path(project).resolve()).strip("/"))


def snap_dir(project: Path) -> Path:
    return SNAP_ROOT / slug(project)


_cache: dict = {}


def load(project: Path) -> dict | None:
    f = snap_dir(project) / "snapshot.json"
    try:
        m = f.stat().st_mtime
    except OSError:
        return None
    c = _cache.get(f)
    if c and c[0] == m:
        return c[1]
    s = json.loads(f.read_text())
    s["_dir"] = str(f.parent)
    _cache[f] = (m, s)
    return s


# ---------------------------------------------------------------- bitbake -e output

def env_text(snap: dict, recipe: str) -> str | None:
    f = Path(snap["_dir"]) / "env" / f"{recipe}.env.gz"
    return gzip.open(f, "rt", errors="replace").read() if f.exists() else None


def env_var(text: str, name: str) -> str | None:
    m = re.search(rf'^(?:export )?{re.escape(name)}="(.*?)"$', text, re.M | re.S)
    return m.group(1) if m else None


def resolve_src_uri(text: str) -> dict:
    """Split a recipe's resolved SRC_URI into the fetched source, patches (in apply order) and config fragments.
    file:// entries are resolved on FILESPATH like bitbake does (first directory that has the file)."""
    fp = (env_var(text, "FILESPATH") or "").split(":")
    out = {"fetch": [], "tarballs": [], "patches": [], "fragments": [], "files": []}
    for u in (env_var(text, "SRC_URI") or "").split():
        if not u.startswith("file://"):
            out["fetch"].append(u)
            continue
        name, *params = u[7:].split(";")
        p = dict(x.split("=", 1) for x in params if "=" in x)
        path = next((os.path.join(d, name) for d in fp if d and os.path.exists(os.path.join(d, name))), None)
        item = {"name": name, "path": path, "striplevel": p.get("striplevel", "1"), "patchdir": p.get("patchdir", "")}
        is_patch = (re.search(r"\.(patch|diff)(\.gz)?$", name) and p.get("apply") != "no") or p.get("apply") == "yes"
        if is_patch:
            out["patches"].append(item)
        elif re.search(r"\.(cfg|scc)$", name):
            out["fragments"].append(item)
        elif re.search(r"\.(tar\.gz|tgz|tar\.bz2|tar\.xz|tar)$", name) and p.get("unpack", "1") != "0":
            out["tarballs"].append(item)
        else:
            out["files"].append(item)
    return out


def mirror_name(url: str) -> str:
    """bitbake's git mirror directory name for a git:// URL (fetch2/git.py: host + path, '/' -> '.')."""
    base = url.split(";")[0]
    host, _, path = re.sub(r"^\w+://", "", base).partition("/")
    return (host.replace(":", ".") + "." + path.replace("/", ".").replace("*", ".")).replace(" ", "_")


# ---------------------------------------------------------------- git objects without touching alternates

def git(repo, *a, inp=None, text=True, check=True):
    r = subprocess.run(["git", f"--git-dir={repo}", *a], capture_output=True, text=text, input=inp)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(map(str, a[:3]))}: {(r.stderr if text else r.stderr.decode()).strip()[:400]}")
    return r.stdout


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def tree_sha(entries: list[tuple[str, str, str]]) -> str:
    """entries: (mode, name, sha). Git's order: names compared as bytes, a tree as if it ended with '/'."""
    def key(e):
        return e[1].encode() + (b"/" if e[0] == "40000" else b"")
    body = b"".join(m.encode() + b" " + n.encode() + b"\0" + bytes.fromhex(s) for m, n, s in sorted(entries, key=key))
    return hashlib.sha1(b"tree %d\0" % len(body) + body).hexdigest()


def exists(repo, sha: str) -> bool:
    return subprocess.run(["git", f"--git-dir={repo}", "cat-file", "-e", sha], capture_output=True).returncode == 0


def put_blob(repo, data: bytes) -> str:
    sha = blob_sha(data)
    if not exists(repo, sha):
        got = git(repo, "hash-object", "-w", "--stdin", inp=data, text=False).decode().strip()
        assert got == sha, (got, sha)
    return sha


def put_tree(repo, entries: list[tuple[str, str, str]]) -> str:
    sha = tree_sha(entries)
    if not exists(repo, sha):
        lines = "".join(f"{m} {'tree' if m == '40000' else 'blob'} {s}\t{n}\n" for m, n, s in entries)
        got = git(repo, "mktree", inp=lines).strip()
        assert got == sha, (got, sha)
    return sha


def ls_tree(repo, tree: str) -> list[tuple[str, str, str]]:
    out = []
    for line in git(repo, "ls-tree", "-z", tree).split("\0"):
        if line:
            meta, name = line.split("\t", 1)
            mode, _, sha = meta.split()
            out.append(("40000" if mode == "040000" else mode, name, sha))   # the in-object form of a tree mode
    return out


def replace_paths(repo, tree: str, changes: dict[str, tuple[str, str] | None]) -> str:
    """New tree = `tree` with changes {path: (mode, blob sha) or None (delete)}. Only changed trees are written."""
    here, sub = {}, {}
    for p, v in changes.items():
        head, _, rest = p.partition("/")
        (sub.setdefault(head, {}).__setitem__(rest, v)) if rest else here.__setitem__(head, v)
    entries = {n: (m, s) for m, n, s in (ls_tree(repo, tree) if tree else [])}
    for n, v in here.items():
        if v is None:
            entries.pop(n, None)
        else:
            entries[n] = v
    for n, ch in sub.items():
        old = entries.get(n)
        new = replace_paths(repo, old[1] if old and old[0] == "40000" else None, ch)
        if new:
            entries[n] = ("40000", new)
        else:
            entries.pop(n, None)
    if not entries:
        return ""
    return put_tree(repo, [(m, n, s) for n, (m, s) in entries.items()])


def _patch_files(patch: str, strip: str, pdir: str) -> list[str]:
    """Paths a patch touches (after -p<strip>, under patchdir), via `git apply --numstat` (no repository needed)."""
    r = subprocess.run(["git", "apply", "--numstat", "-z", f"-p{strip}", patch], capture_output=True, text=True,
                       cwd=tempfile.gettempdir(), env=_noenv())
    if r.returncode:
        raise RuntimeError(f"cannot read patch {patch}: {r.stderr.strip()[:300]}")
    files, toks = [], r.stdout.split("\0")
    i = 0
    while i < len(toks):
        t = toks[i]
        if not t:
            i += 1
            continue
        parts = t.split("\t")
        if len(parts) == 3 and parts[2]:
            files.append(parts[2])
            i += 1
        else:                       # rename: "a\tb\t" then old, new
            files += [toks[i + 1], toks[i + 2]]
            i += 3
    return [os.path.normpath(os.path.join(pdir, f)) if pdir else f for f in files]


def _noenv():
    e = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    e["GIT_CEILING_DIRECTORIES"] = "/"
    return e


def apply_patches(repo, base_tree: str, patches: list[dict]) -> tuple[str, dict]:
    """Apply patches (bitbake order, `git apply` semantics) to base_tree; return (view tree, {path: [patch names]}).
    Files are materialised in a temporary directory only for the paths the patches touch."""
    content: dict[str, tuple[str, bytes] | None] = {}       # path -> (mode, data) | None (deleted)
    touched: dict[str, list[str]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for p in patches:
            if not p.get("path"):
                raise RuntimeError(f"patch not found on FILESPATH: {p['name']}")
            files = _patch_files(p["path"], p["striplevel"], p["patchdir"])
            for f in files:
                touched.setdefault(f, []).append(p["name"])
                dst = Path(tmp, f)
                if dst.exists() or f in content:
                    continue
                r = subprocess.run(["git", f"--git-dir={repo}", "cat-file", "blob", f"{base_tree}:{f}"], capture_output=True)
                if r.returncode == 0:
                    mode = next((m for m, n, s in ls_tree(repo, f"{base_tree}:{os.path.dirname(f)}" if "/" in f else base_tree)
                                 if n == os.path.basename(f)), "100644")
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(r.stdout)
                    if mode == "100755":
                        dst.chmod(0o755)
            a = ["git", "apply", f"-p{p['striplevel']}"] + ([f"--directory={p['patchdir']}"] if p["patchdir"] else []) + [p["path"]]
            r = subprocess.run(a, capture_output=True, text=True, cwd=tmp, env=_noenv())
            if r.returncode:
                raise RuntimeError(f"{p['name']} does not apply: {r.stderr.strip()[:300]}")
        changes = {}
        for f in touched:
            fp = Path(tmp, f)
            if fp.is_symlink():
                changes[f] = ("120000", put_blob(repo, os.readlink(fp).encode()))
            elif fp.is_file():
                changes[f] = ("100755" if os.access(fp, os.X_OK) else "100644", put_blob(repo, fp.read_bytes()))
            else:
                changes[f] = None
    return (replace_paths(repo, base_tree, changes) if changes else base_tree), touched


# ---------------------------------------------------------------- imports into local.git (no alternates)

def import_dir(repo, d: Path, max_size: int | None = 1 << 20, text_only: bool = True) -> tuple[str, list[str]]:
    """Files under d -> tree in `repo` (local.git only: batch writes, no existence check needed). Returns
    (tree, skipped paths): files over max_size or binary (NUL in the first 8 KB) are skipped when text_only."""
    paths, links, skipped = [], [], []
    for root, dirs, names in os.walk(d):
        dirs[:] = sorted(x for x in dirs if x not in (".git", "__pycache__"))
        for n in sorted(names) + [x for x in dirs if os.path.islink(os.path.join(root, x))]:
            p = Path(root, n)
            rel = str(p.relative_to(d))
            if p.is_symlink():
                links.append((rel, os.readlink(p).encode()))
            elif p.is_file():
                if text_only and ((max_size and p.stat().st_size > max_size) or b"\0" in p.open("rb").read(8192)):
                    skipped.append(rel)
                else:
                    paths.append((rel, p))
    shas = subprocess.run(["git", f"--git-dir={repo}", "hash-object", "-w", "--stdin-paths", "--no-filters"],
                          input="".join(f"{p}\n" for _, p in paths), capture_output=True, text=True, check=True).stdout.split()
    lines = [f"{'100755' if os.access(p, os.X_OK) else '100644'} {s}\t{rel}" for (rel, p), s in zip(paths, shas)]
    lines += [f"120000 {put_blob(repo, target)}\t{rel}" for rel, target in links]
    idx = tempfile.mktemp(prefix="rzsnap-idx-")
    env = dict(os.environ, GIT_INDEX_FILE=idx)
    try:
        subprocess.run(["git", f"--git-dir={repo}", "update-index", "--add", "--index-info"], input="\n".join(lines) + "\n",
                       text=True, env=env, check=True, capture_output=True)
        tree = subprocess.run(["git", f"--git-dir={repo}", "write-tree"], env=env, capture_output=True, text=True,
                              check=True).stdout.strip()
    finally:
        Path(idx).unlink(missing_ok=True)
    return tree, skipped


def import_tar(repo, tf: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(tf) as z:
            z.extractall(tmp, filter="data")
        return import_dir(repo, Path(tmp), max_size=None, text_only=False)[0]


# ---------------------------------------------------------------- staleness

def fingerprint(d: Path) -> str:
    """Git HEAD for a checkout, else a digest of (path, size, mtime) of every file: cheap and change-sensitive."""
    if (d / ".git").exists():
        r = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True)
        dirty = subprocess.run(["git", "-C", str(d), "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True).stdout
        return "git:" + r.stdout.strip() + ("+dirty" if dirty.strip() else "")
    h = hashlib.sha1()
    for root, dirs, names in os.walk(d):
        dirs.sort()
        for n in sorted(names):
            try:
                st = os.stat(os.path.join(root, n))
            except OSError:
                continue
            h.update(f"{os.path.relpath(os.path.join(root, n), d)}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return "files:" + h.hexdigest()


_stale_cache: dict = {}


def stale(snap: dict, ttl: float = 60.0) -> list[str]:
    """Official / optional layers that changed (or appeared / disappeared) since the snapshot was taken."""
    import time
    key = snap["_dir"]
    c = _stale_cache.get(key)
    if c and time.time() - c[0] < ttl and c[1] == snap["created"]:
        return c[2]
    project = Path(snap["project"])
    out = []
    for rel, fp in snap.get("layer_fingerprints", {}).items():
        d = project / rel
        if not d.exists():
            out.append(f"{rel} is gone")
        elif fingerprint(d) != fp:
            out.append(f"{rel} changed")
    from . import yocto as Y
    for c_ in Y.optional_components(Y.release(snap["release"])):
        for l in c_["layers"] + c_.get("legacy_layers", []):
            if (project / l / "conf" / "layer.conf").exists() and l not in snap.get("layer_fingerprints", {}):
                out.append(f"optional layer {l} was installed after the snapshot")
    _stale_cache[key] = (time.time(), snap["created"], out)
    return out
