"""Set up or extend a Yocto project for a BSP release (used by workflow/build/yocto-project and poc-use).

It changes only the project directory:
- clones the release's layers (catalog/bsp_releases.toml) and checks out the pinned revisions;
- installs optional components: a licensed package after its sha256 matches the catalog, or a git layer at its pin;
- writes the baseline build/conf (poc-use --baseline) and fetches the official kernel / U-Boot / TF-A sources;
- records what it did in <project>/.rz-mcp/project.json.

It never deletes or overwrites what it did not create. Work in progress goes to <project>/.rz-mcp/stage/ and is
moved into place only when complete, so an interrupted run leaves nothing half-written where the build looks.
No cache is shared between projects: DL_DIR and SSTATE_DIR stay the template's defaults inside <project>/build.
"""
import datetime as dt
import fcntl
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import zipfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))
from rzmcp import yocto as Y  # noqa: E402

BUILD = ROOT / "workflow" / "build"
FETCH = ["linux-renesas", "u-boot", "trusted-firmware-a"]      # recipes whose git mirrors bsp_source reads
MIN_FREE_GB, WARN_FREE_GB = 30, 100                             # a full build of one image takes about 60 GB


class SetupError(Exception):
    pass


def now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


LOG: Path | None = None                 # <project>/.rz-mcp/setup.log while setup() runs


def say(msg: str) -> None:
    print(msg, flush=True)
    if LOG:
        with open(LOG, "a") as f:
            f.write(f"{now()} {msg}\n")


@contextmanager
def locked(project: Path):
    d = project / Y.STATE_DIR
    d.mkdir(parents=True, exist_ok=True)
    f = open(d / "setup.lock", "a")
    try:
        fcntl.lockf(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise SetupError(f"another yocto-project run is working on {project} ({d / 'setup.lock'})")
    try:
        yield
    finally:
        fcntl.lockf(f, fcntl.LOCK_UN)
        f.close()


def load_state(project: Path) -> dict:
    return Y.read_state(project) or {}


def save_state(project: Path, st: dict) -> None:
    f = Y.state_file(project)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1, ensure_ascii=False))
    tmp.replace(f)


def stage_dir(project: Path, name: str) -> Path:
    d = project / Y.STATE_DIR / "stage" / name
    if d.exists():
        shutil.rmtree(d)                 # ours, left over from an interrupted run
    d.mkdir(parents=True)
    return d


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def git(*args: str) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise SetupError(f"git {' '.join(args)}: {r.stderr.strip()[-500:]}")
    return r.stdout.strip()


# ---------------------------------------------------------------- layers from git

def clone_at(project: Path, url: str, rev: str, dest: Path, label: str, tag: str | None = None) -> None:
    """Clone `url` into `dest` at `rev` (How To Build: git clone; git checkout -b tmp <rev>)."""
    if dest.exists():
        head = Y._git_head(dest)
        if head == rev:
            say(f"  {label}: already at {rev[:12]}")
            return
        raise SetupError(f"{dest} exists but is not a checkout of {rev[:12]} (HEAD {head[:12] if head else 'none'}): not touching it")
    st = stage_dir(project, "git-" + dest.name)
    tmp = st / dest.name
    say(f"  {label}: git clone {url} ...")
    if subprocess.run(["git", "clone", "--quiet", "--no-checkout", url, str(tmp)]).returncode != 0:
        raise SetupError(f"git clone {url} failed (network?)")
    if tag:
        t = git("-C", str(tmp), "rev-parse", f"{tag}^{{commit}}")
        if t != rev:
            raise SetupError(f"{url}: tag {tag} is now {t[:12]}, the catalog pins {rev[:12]}; not using it")
    git("-C", str(tmp), "checkout", "--quiet", "-b", "tmp", rev)
    if git("-C", str(tmp), "rev-parse", "HEAD") != rev:
        raise SetupError(f"{label}: HEAD is not {rev}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp.rename(dest)
    shutil.rmtree(st, ignore_errors=True)
    say(f"  {label}: {rev[:12]}" + (f" (tag {tag})" if tag else ""))


# ---------------------------------------------------------------- licensed packages

def _member(zf: zipfile.ZipFile, spec: str):
    """A tar.gz in the package; 'a.zip!b.tar.gz' is b.tar.gz inside a.zip inside the package."""
    if "!" in spec:
        outer, inner = spec.split("!", 1)
        return zipfile.ZipFile(io.BytesIO(zf.read(outer))).open(inner)
    return zf.open(spec)


def _unpack(stream, dest: Path, spec: str) -> int:
    n = 0

    def only_features(member, path):
        member = tarfile.data_filter(member, path)        # no absolute paths, no '..', no links out, no devices
        name = member.name.removeprefix("./")
        if name != "meta-rz-features" and not name.startswith("meta-rz-features/"):
            raise SetupError(f"{spec}: unexpected member {member.name!r} (everything must be under meta-rz-features/)")
        return member

    try:
        with tarfile.open(fileobj=stream, mode="r|gz") as tf:
            for m in tf:
                tf.extract(m, dest, filter=only_features)
                n += 1
    except (tarfile.TarError, OSError) as e:
        raise SetupError(f"{spec}: {type(e).__name__}: {e}")
    return n


def install_package(project: Path, c: dict) -> dict:
    src = ROOT / c["file"]
    if not src.exists():
        raise SetupError(f"{c['name']}: {src} is missing. Download {c['part']} {c['version']} from renesas.com "
                         f"({c.get('source', '')}) and put it there.")
    say(f"  {c['name']}: checking {src.name}")
    got = sha256(src)
    if got != c["sha256"]:
        raise SetupError(f"{c['name']}: sha256 of {src} is {got}, the catalog expects {c['sha256']} "
                         "(another version, or damaged): not installing")
    st = stage_dir(project, c["name"])
    try:
        with zipfile.ZipFile(src) as zf:
            for spec in c["extract"]:
                with _member(zf, spec) as s:
                    say(f"  {c['name']}: unpacking {spec.split('/')[-1]} ({_unpack(s, st, spec)} entries)")
        for l in c["layers"]:
            if not (st / l / "conf" / "layer.conf").exists():
                raise SetupError(f"{c['name']}: the package has no {l}/conf/layer.conf")
        items = sorted((st / "meta-rz-features").iterdir())
        clash = [project / "meta-rz-features" / i.name for i in items if (project / "meta-rz-features" / i.name).exists()]
        if clash:
            raise SetupError(f"{c['name']}: {', '.join(map(str, clash))} already exist(s): not overwriting")
    except (SetupError, zipfile.BadZipFile, KeyError) as e:
        shutil.rmtree(st, ignore_errors=True)
        raise e if isinstance(e, SetupError) else SetupError(f"{c['name']}: {src.name}: {e}")
    (project / "meta-rz-features").mkdir(exist_ok=True)
    for i in items:
        i.rename(project / "meta-rz-features" / i.name)
    shutil.rmtree(st, ignore_errors=True)
    return {"kind": "package", "part": c["part"], "version": c["version"], "file": c["file"], "sha256": got}


# ---------------------------------------------------------------- optional components

def component(name: str, rel: dict) -> dict:
    for c in Y.optional_components(rel):
        if c["name"] == name:
            return c
    raise SetupError(f"unknown component {name!r}; known: {', '.join(c['name'] for c in Y.optional_components(rel))}")


def _add(project: Path, name: str, rel: dict) -> str:
    c = component(name, rel)
    present = [l for l in c["layers"] + c.get("legacy_layers", []) if (project / l / "conf" / "layer.conf").exists()]
    if present:
        return f"{name}: already present ({present[0]})"
    if c["kind"] == "package":
        rec = install_package(project, c)
    elif c["kind"] == "git":
        clone_at(project, c["url"], c["rev"], project / c["dest"], name, tag=c.get("tag"))
        rec = {"kind": "git", "url": c["url"], "tag": c.get("tag"), "rev": c["rev"]}
    else:
        raise SetupError(f"{name}: unknown kind {c['kind']!r}")
    rec["installed"] = now()
    st = load_state(project)
    st.setdefault("components", {})[name] = rec
    save_state(project, st)
    return f"{name}: installed {c.get('version') or c.get('tag')} -> {', '.join(c['layers'])}"


def add_components(project: Path, names: list[str], rel_name: str = Y.DEFAULT_RELEASE) -> list[str]:
    rel = Y.release(rel_name)
    for n in names:
        component(n, rel)                 # unknown names fail before anything changes
    with locked(project):
        return [_add(project, n, rel) for n in names]


# ---------------------------------------------------------------- the project

def _baseline_conf(project: Path) -> None:
    r = subprocess.run([sys.executable, str(BUILD / "poc-use"), "--baseline", "--project", str(project)])
    if r.returncode != 0:
        raise SetupError("poc-use --baseline failed")


def _fetch(project: Path) -> None:
    say(f"  bitbake -c fetch {' '.join(FETCH)} (first parse plus the kernel's git history, about 3 GB; 5 minutes on 2026-11-26)")
    r = subprocess.run([sys.executable, str(BUILD / "poc-build"), "--project", str(project), "--task", "fetch", *FETCH])
    if r.returncode != 0:
        raise SetupError("fetching the official sources failed: see the report under data/build-runs/")
    rel = Y.release()
    missing = [n for n, c in rel["components"].items() if not Y.mirror_has(project, c["mirror"], c["rev"])]
    if missing:
        raise SetupError(f"after the fetch the git mirrors still lack the official revision of {', '.join(missing)}")


def _free_gb(path: Path) -> float:
    p = path
    while not p.exists():
        p = p.parent
    return shutil.disk_usage(p).free / 2**30


def setup(project: Path, with_: list[str] | None = None, fetch: bool = True, rel_name: str = Y.DEFAULT_RELEASE) -> str:
    """Make `project` a usable Yocto project for the release: reuse it if it already is one, set it up if it is
    absent / empty, continue an interrupted setup, and refuse anything else. Returns 'reused' or 'created'."""
    rel = Y.release(rel_name)
    with_ = with_ or []
    for n in with_:
        component(n, rel)
    a = Y.assess(project, rel_name)
    say(Y.describe(a))
    if a["state"] == "unusable":
        raise SetupError("not usable, and not empty: choose another directory, or fix the problems above")
    if a["running"]:
        raise SetupError("a setup of this directory is running now")
    if a["state"] == "usable":
        with locked(project):
            for n in with_:
                say("  " + _add(project, n, rel))
            if not (project / "build" / "conf" / "bblayers.conf").exists():
                _baseline_conf(project)
        if fetch and any(not Y.mirror_has(project, c["mirror"], c["rev"]) for c in rel["components"].values()):
            _fetch(project)
        say(f"reused {project}")
        return "reused"

    if Y.container_sees(project) is None:
        raise SetupError(f"docker or the container {Y.CONTAINER} is not available: needed to build")
    free = _free_gb(project)
    if free < MIN_FREE_GB:
        raise SetupError(f"only {free:.0f} GB free: a project needs at least {MIN_FREE_GB} GB (about 60 GB for one image)")
    if free < WARN_FREE_GB:
        say(f"warning: {free:.0f} GB free; a full build of one image takes about 60 GB")
    project.mkdir(parents=True, exist_ok=True)
    global LOG
    LOG = project / Y.STATE_DIR / "setup.log"
    LOG.parent.mkdir(parents=True, exist_ok=True)
    say(f"setup of {project} for {rel_name} (log: {LOG})")
    with locked(project):
        st = load_state(project)
        if st.get("created_by") != "yocto-project":
            st = {"created_by": "yocto-project", "release": rel_name, "created": now(), "done": None,
                  "steps": {}, "components": st.get("components", {})}
        save_state(project, st)
        say(f"[1/4] layers of {rel_name}")
        for l in rel["layers"]:
            clone_at(project, l["url"], l["rev"], project / l["name"], l["name"], tag=l.get("tag"))
        st = load_state(project)
        st["steps"]["layers"] = now()
        save_state(project, st)
        say("[2/4] optional components" + ("" if with_ else ": none requested (poc-use adds what a PoC names)"))
        for n in with_:
            say("  " + _add(project, n, rel))
        say("[3/4] build/conf (official template + rz-mcp standard)")
        if not (project / "build" / "conf" / "bblayers.conf").exists():
            _baseline_conf(project)
        check = Y.assess(project, rel_name, as_finished=True)
        if check["state"] != "usable":
            raise SetupError("after cloning, the project is still not usable:\n" + Y.describe(check))
        st = load_state(project)
        st["steps"]["conf"] = now()
        save_state(project, st)
        if fetch:
            say("[4/4] official kernel / U-Boot / TF-A sources (for bsp_source, bsp_grep, patch_stack)")
            _fetch(project)
            st = load_state(project)
            st["steps"]["fetch"] = now()
        else:
            say("[4/4] skipped (--no-fetch): bsp_source / bsp_grep cannot read this project's sources yet")
            st = load_state(project)
        st["done"] = now()
        save_state(project, st)
    say(f"created {project}")
    return "created"
