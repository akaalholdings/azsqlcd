"""The release artefact: built from git blobs, named by a manifest digest, read back in memory.

The manifest holds identity only (commit, release_seq, file hashes, the release that added each
chain line). It holds no tool version and no derived data, so two tool versions give one digest
for one commit.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

from azsqlcd import chain, names
from azsqlcd.errors import ToolError, refused

BUNDLE_NAME = "bundle.tar"
MANIFEST_NAME = "manifest.json"
CONFIG_PATH = "azsqlcd.toml"
_ROOTS = ("schema", "migrations", "onboarding")
_TOP_NAMES = (CONFIG_PATH, *_ROOTS)
# The ref that says what main is. A full name: git resolves the short name origin/main to
# refs/tags/origin/main before refs/remotes/origin/main, and anyone who can push a tag can make one.
MAIN_REF = "refs/remotes/origin/main"
_SHA1 = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")

type StrPath = str | os.PathLike[str]


@dataclass(frozen=True)
class Manifest:
    commit: str  # 40 hex
    release_seq: int
    files: tuple[tuple[str, str], ...]  # (path, sha256 of the raw blob bytes), sorted by path
    chain_added_in: dict[str, int]  # migration id -> release_seq of the release that added its chain line


@dataclass(frozen=True)
class Release:
    """What build() read from git."""

    manifest: Manifest
    files: dict[str, bytes]  # path -> raw blob bytes


@dataclass(frozen=True)
class Bundle:
    """What read_bundle() read: files checked against the manifest, the manifest against the digest."""

    manifest: Manifest
    files: dict[str, bytes]


# ------------------------------------------------------------------ git
def git(args: list[str], cwd: StrPath, *, stdin: bytes = b"") -> bytes:
    """Run git (no shell) and return its stdout. The only place in the tool that starts git."""
    # replace refs could put other content behind an object name
    command = [_git_program(cwd), "--no-replace-objects", *args]
    try:
        done = subprocess.run(command, cwd=cwd, input=stdin, capture_output=True, check=False)
    except OSError as e:
        raise refused("GIT_FAILED", f"git could not be started: {e}") from None
    if done.returncode != 0:
        error = done.stderr.decode("utf-8", "replace").strip()[-500:]
        raise refused("GIT_FAILED", f"git {args[0]} failed (exit {done.returncode}): {error}", args=args)
    return done.stdout


def _git_program(cwd: StrPath) -> str:
    """The full path of git, found on PATH. Refused (GIT_FAILED): no git, or a git that lies in
    the repository or in the current directory.

    Started by its bare name, Windows looks for git.exe in the current directory before PATH (and
    shutil.which does the same there). The current directory is the checkout of a pull request, so
    a program of that name in it is content of the repository, never the git of the machine.
    """
    found = shutil.which("git")
    if found is None:
        raise refused("GIT_FAILED", "git could not be started: no program named git is on PATH")
    program = Path(found).absolute()
    folders = {Path.cwd().resolve(), Path(cwd).resolve()}
    # the folder where it was found, and the folder of the file behind a link
    if program.parent.resolve() in folders or program.resolve().parent in folders:
        raise refused(
            "GIT_FAILED",
            f"git could not be started: the program found is {program.parent.resolve() / program.name}, "
            "a file of the repository or of the current directory; it was not run",
        )
    return str(program)


def _commit_sha(repo_dir: StrPath, revision: str) -> str:
    # --end-of-options: a revision that starts with '-' is never read as an option
    try:
        found = git(["rev-parse", "--verify", "--end-of-options", revision + "^{commit}"], repo_dir)
    except ToolError as e:
        # git says only "Needed a single revision": the reader must see which one
        raise refused(
            "GIT_FAILED", f"{revision!r} is not a commit of this repository: {e.message}", revision=revision
        ) from None
    return found.decode().strip()


def _ref_commit(repo_dir: StrPath, ref: str) -> str:
    """The commit of one exact ref. Refused (MAIN_REF_INVALID): a name that is not a full ref name,
    or a tag. `git show-ref --verify` reads the exact ref path only, so no tag or branch with the
    same short name can stand in for it."""
    if not ref.startswith("refs/") or ref.startswith("refs/tags/"):
        raise refused(
            "MAIN_REF_INVALID",
            f"the ref of main must be a full ref name that is not a tag, for example {MAIN_REF}",
            ref=ref,
        )
    try:
        exact = git(["show-ref", "--verify", "--hash", ref], repo_dir).decode().strip()
    except ToolError as e:
        # the first live run: a repository without a fetched origin/main
        raise refused(
            "GIT_FAILED", f"the ref of main, {ref!r}, is not in this repository: {e.message}", revision=ref
        ) from None
    return _commit_sha(repo_dir, exact)


def _objects(repo_dir: StrPath, object_names: list[str]) -> list[bytes | None]:
    """Blob bytes for each object name, in order, from one `git cat-file --batch`. None = no such blob."""
    if not object_names:
        return []
    out = git(["cat-file", "--batch"], repo_dir, stdin="".join(f"{name}\n" for name in object_names).encode())
    blobs: list[bytes | None] = []
    pos = 0
    for _ in object_names:
        end = out.index(b"\n", pos)
        head = out[pos:end].split(b" ")  # '<oid> <type> <size>' or '<name> missing'
        pos = end + 1
        if head[-1] == b"missing":
            blobs.append(None)
            continue
        size = int(head[-1])
        blobs.append(out[pos : pos + size] if head[-2] == b"blob" else None)
        pos += size + 1
    return blobs


def _in_scope(path: str) -> bool:
    return path == CONFIG_PATH or ("/" in path and path.split("/", 1)[0] in _ROOTS)


def root_case_problem(name: str) -> str | None:
    """Why a name at the root of a repository is refused: it is azsqlcd.toml, schema, migrations
    or onboarding in another letter case. None for every other name, and for the exact names.

    A file system without letter case (Windows, the default of macOS) opens Schema/ when the tool
    asks for schema/, so the working-tree commands read the folder. git holds the name as it was
    made, and a release takes the exact names only: the files would be left out with no message.
    """
    if name in _TOP_NAMES:
        return None
    for exact in _TOP_NAMES:
        # upper(): NTFS compares in upper case, where a dotless i is I; casefold(): a long s is s
        if name.casefold() == exact.casefold() or name.upper() == exact.upper():
            return (
                f"the name {name!r} must be {exact!r}, in lower case: a file system without letter "
                f"case reads it as {exact!r}, and a release does not hold it"
            )
    return None


def _path_problem(path: str) -> str | None:
    if "\\" in path or "\0" in path or any(part in ("", ".", "..") for part in path.split("/")):
        return "is not a safe relative path"
    if not _in_scope(path):
        return f"is outside {CONFIG_PATH}, schema/, migrations/ and onboarding/"
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return "is not UTF-8"
    # a path that Git for Windows refuses to check out; the planner reads no object from such a file
    windows = names.path_problem(path)
    if windows:
        return f"cannot be checked out on Windows: {windows}"
    return None


def read_tree(repo_dir: StrPath, revision: str) -> dict[str, bytes]:
    """Raw blob bytes of azsqlcd.toml, schema/**, migrations/** and onboarding/** at a revision.

    Read from git objects only; the working tree and the index take no part.
    """
    sha = _commit_sha(repo_dir, revision)
    wanted: list[tuple[str, str]] = []
    for row in git(["ls-tree", "-r", "-z", "--full-tree", sha], repo_dir).split(b"\0"):
        if not row:
            continue
        meta, _, raw_path = row.partition(b"\t")
        mode, _, oid = meta.decode("ascii").split(" ")
        path = raw_path.decode("utf-8", "surrogateescape")
        if path in _ROOTS and mode not in ("100644", "100755"):
            # ls-tree -r never lists a folder, so this is a symbolic link or a submodule in the
            # place of a release folder. Skipped, the release would silently hold no file of it.
            raise refused(
                "TREE_INVALID", f"commit {sha}: {path!r} is not a folder (git mode {mode})", path=path
            )
        case = root_case_problem(path.split("/", 1)[0])
        if case:
            # skipped as out of scope, the release would silently hold no file of the folder
            raise refused("TREE_INVALID", f"commit {sha}: {path!r}: {case}", path=path)
        if not _in_scope(path):
            continue
        problem = _path_problem(path)
        if mode not in ("100644", "100755"):  # a symbolic link or a submodule
            problem = f"is not a regular file (git mode {mode})"
        if problem:
            raise refused("TREE_INVALID", f"commit {sha}: {path!r} {problem}")
        wanted.append((path, oid))
    blobs = _objects(repo_dir, [oid for _, oid in wanted])
    files: dict[str, bytes] = {}
    for (path, oid), blob in zip(wanted, blobs, strict=True):
        if blob is None:
            raise refused("GIT_FAILED", f"git has no blob {oid} for {path}", path=path)
        files[path] = blob
    return files


# ------------------------------------------------------------------ build
def build(repo_dir: StrPath, commit: str, main_ref: str = MAIN_REF) -> Release:
    """Read one commit of main from git objects into a Release. The working tree is never read.

    main_ref is a full ref name (refs/remotes/origin/main, refs/heads/main), never a short name
    and never a tag: else REFUSED MAIN_REF_INVALID. The commit must be on its first-parent chain.

    release_seq is the number of commits on the first-parent chain that ends at the commit
    (`git rev-list --count --first-parent <commit>`).
    """
    sha = _commit_sha(repo_dir, commit)
    if git(["rev-parse", "--is-shallow-repository"], repo_dir).strip() == b"true":
        raise refused("SHALLOW_REPOSITORY", "a release needs the full history; fetch with depth 0")
    main_chain = (
        git(["rev-list", "--first-parent", _ref_commit(repo_dir, main_ref)], repo_dir).decode().split()
    )
    if sha not in main_chain:
        raise refused(
            "NOT_ON_MAIN", f"commit {sha} is not on the first-parent chain of {main_ref}", commit=sha
        )
    own_chain = main_chain[main_chain.index(sha) :]  # the commit first, the root commit last
    seq_of = {c: len(own_chain) - i for i, c in enumerate(own_chain)}

    files = read_tree(repo_dir, sha)
    added: dict[str, int] = {}
    if chain.SUM_PATH in files:
        try:
            sum_text = files[chain.SUM_PATH].decode("utf-8")
        except UnicodeDecodeError:
            raise refused("CHAIN_INVALID", f"{chain.SUM_PATH} is not UTF-8") from None
        wanted = {entry.file for entry in chain.parse_sum(sum_text).entries}
        # first-parent commits that changed the file, oldest first
        changed = git(["rev-list", "--first-parent", "--reverse", sha, "--", chain.SUM_PATH], repo_dir)
        commits = changed.decode().split()
        for c, blob in zip(
            commits, _objects(repo_dir, [f"{c}:{chain.SUM_PATH}" for c in commits]), strict=True
        ):
            for line in (blob or b"").decode("utf-8", "replace").splitlines()[1:]:
                file = line.split(" ", 1)[0]
                if file in wanted:
                    added.setdefault(file, seq_of[c])
        lost = sorted(wanted - set(added))
        if lost:
            raise refused("GIT_FAILED", f"no first-parent commit adds {lost[0]} to {chain.SUM_PATH}")
    manifest = Manifest(
        commit=sha,
        release_seq=seq_of[sha],
        files=tuple(sorted((path, hashlib.sha256(data).hexdigest()) for path, data in files.items())),
        chain_added_in=added,
    )
    return Release(manifest, files)


# ------------------------------------------------------------------ manifest
def manifest_json(manifest: Manifest) -> bytes:
    """Canonical JSON of the manifest: sorted keys, no white space, UTF-8, one line break at the end."""
    doc = {
        "commit": manifest.commit,
        "release_seq": manifest.release_seq,
        "files": [list(item) for item in sorted(manifest.files)],
        "chain_added_in": manifest.chain_added_in,
    }
    return (json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def digest(manifest: Manifest) -> str:
    """The identity of a release: sha256 of manifest_json(manifest), which is also sha256 of manifest.json."""
    return hashlib.sha256(manifest_json(manifest)).hexdigest()


def write(release: Release, out_dir: StrPath) -> None:
    """Write bundle.tar and manifest.json. The bytes depend on the release only."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out / BUNDLE_NAME, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(release.files):
            info = tarfile.TarInfo(path)
            info.size = len(release.files[path])
            info.mode, info.mtime, info.uid, info.gid, info.uname, info.gname = 0o644, 0, 0, 0, "", ""
            tar.addfile(info, io.BytesIO(release.files[path]))
    (out / MANIFEST_NAME).write_bytes(manifest_json(release.manifest))


def _bundle_invalid(problem: str) -> ToolError:
    return refused("BUNDLE_INVALID", f"the release bundle is refused: {problem}")


def _parse_manifest(raw: bytes) -> Manifest:
    try:
        doc = json.loads(raw)
    except (ValueError, RecursionError):
        doc = None
    if not (
        isinstance(doc, dict)
        and set(doc) == {"commit", "release_seq", "files", "chain_added_in"}
        and isinstance(doc["commit"], str)
        and type(doc["release_seq"]) is int
        and isinstance(doc["files"], list)
        and all(
            isinstance(f, list) and len(f) == 2 and all(isinstance(x, str) for x in f) for f in doc["files"]
        )
        and isinstance(doc["chain_added_in"], dict)
        and all(type(seq) is int for seq in doc["chain_added_in"].values())
    ):
        raise _bundle_invalid(f"{MANIFEST_NAME} is not a manifest")
    manifest = Manifest(
        commit=doc["commit"],
        release_seq=doc["release_seq"],
        files=tuple((path, sha) for path, sha in doc["files"]),
        chain_added_in=doc["chain_added_in"],
    )
    # so that digest(manifest) is the digest that was checked
    if manifest_json(manifest) != raw:
        raise _bundle_invalid(f"{MANIFEST_NAME} is not in canonical form")
    # build never writes any of these; a manifest that holds one was made by something else
    if not _SHA1.fullmatch(manifest.commit):
        raise _bundle_invalid(f"{MANIFEST_NAME}: commit is not 40 lower-case hex characters")
    if manifest.release_seq < 1:
        raise _bundle_invalid(f"{MANIFEST_NAME}: release_seq is lower than 1")
    seen: set[str] = set()
    for path, sha in manifest.files:
        problem = _path_problem(path)
        if problem:
            raise _bundle_invalid(f"{MANIFEST_NAME}: file {path!r} {problem}")
        if path in seen:
            raise _bundle_invalid(f"{MANIFEST_NAME}: file {path!r} is listed more than once")
        if not _SHA256.fullmatch(sha):
            raise _bundle_invalid(f"{MANIFEST_NAME}: file {path!r} has no sha256")
        seen.add(path)
    for file, seq in manifest.chain_added_in.items():
        if not chain.is_migration_file(file):
            raise _bundle_invalid(f"{MANIFEST_NAME}: chain_added_in names {file!r}, which is no migration")
        if not 1 <= seq <= manifest.release_seq:
            raise _bundle_invalid(f"{MANIFEST_NAME}: chain_added_in of {file} is not from 1 to release_seq")
    return manifest


def read_bundle(bundle_dir: StrPath, expected_digest: str) -> Bundle:
    """Read manifest.json and bundle.tar from a directory, in memory, and check them.

    Nothing is extracted to disk. A member must be a regular file that the manifest lists, once,
    with the listed sha256; every listed file must be there. Else ToolError REFUSED BUNDLE_INVALID.
    The sha256 of manifest.json must equal expected_digest, else REFUSED DIGEST_MISMATCH.
    """
    folder = Path(bundle_dir)
    try:
        raw = (folder / MANIFEST_NAME).read_bytes()
    except OSError as e:
        raise _bundle_invalid(f"{MANIFEST_NAME} cannot be read: {e}") from None
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected_digest:
        raise refused(
            "DIGEST_MISMATCH",
            f"{MANIFEST_NAME} has digest {actual}; expected {expected_digest}",
            expected=expected_digest,
            actual=actual,
        )
    manifest = _parse_manifest(raw)
    listed = dict(manifest.files)
    files: dict[str, bytes] = {}
    try:
        with tarfile.open(folder / BUNDLE_NAME, mode="r:") as tar:
            for member in tar:
                if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
                    raise _bundle_invalid(f"member {member.name!r} is not a regular file")
                problem = _path_problem(member.name)
                if problem:
                    raise _bundle_invalid(f"member {member.name!r} {problem}")
                if member.name not in listed:
                    raise _bundle_invalid(f"member {member.name!r} is not in the manifest")
                if member.name in files:
                    raise _bundle_invalid(f"member {member.name!r} is in the bundle more than once")
                stream = tar.extractfile(member)
                data = stream.read() if stream else b""
                if hashlib.sha256(data).hexdigest() != listed[member.name]:
                    raise _bundle_invalid(f"member {member.name!r} does not have the sha256 of the manifest")
                files[member.name] = data
    except (tarfile.TarError, OSError, EOFError) as e:
        raise _bundle_invalid(f"{BUNDLE_NAME} cannot be read: {e}") from None
    missing = sorted(set(listed) - set(files))
    if missing:
        raise _bundle_invalid(f"{missing[0]!r} is in the manifest and not in the bundle")
    return Bundle(manifest, files)


# ------------------------------------------------------------------ tool identity
def tool_digest() -> str:
    """sha256 over the .py files of this package: for each file in order of its relative path
    (forward slashes), the path, a zero byte, the file bytes, a zero byte.

    CRLF counts as LF: a git checkout on Windows that converts line ends gives the digest of the
    LF checkout that CI and the action use."""
    root = Path(__file__).resolve().parent
    sources = sorted((path.relative_to(root).as_posix(), path) for path in root.rglob("*.py"))
    h = hashlib.sha256()
    for relative, path in sources:
        h.update(relative.encode("utf-8") + b"\0" + path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return h.hexdigest()
