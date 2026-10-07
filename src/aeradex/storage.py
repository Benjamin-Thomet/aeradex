"""Local book transactions: isolated work copy, durable undo log and recovery.

All entry points hold the same advisory book lock. Editors/cloud sync must not
write during publication; revision checks detect changes before publication.
The undo log remains if recovery meets an unexpected externally changed file.
"""
from __future__ import annotations

import contextvars
import fcntl
import functools
import hashlib
import inspect
import json
import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from .files import atomic_write, atomic_write_text, fsync_directory, FormatError

_RUNTIME = ".aeradex/transaction"
_active = contextvars.ContextVar("aeradex_transaction", default=None)
_held = threading.local()
_mutex = threading.RLock()


def internal(rel: str) -> bool:
    return (rel == ".git" or rel.startswith(".git/") or rel == ".aeradex/write.lock"
            or rel == _RUNTIME or rel.startswith(_RUNTIME + "/")
            or any(p.startswith(".aeradex-tmp-") or p == "__pycache__" for p in Path(rel).parts))


def _files(root):
    out = {}
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not internal((Path(base) / d).relative_to(root).as_posix()))
        for name in sorted(names + [d for d in dirs if (Path(base) / d).is_symlink()]):
            path = Path(base) / name
            rel = path.relative_to(root).as_posix()
            if internal(rel):
                continue
            if path.is_symlink():
                raise FormatError(f"{rel}: symbolische Links im Buch werden nicht unterstützt")
            out[rel] = path
    return out


def _hash(path):
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise FormatError(f"{path}: erwartet eine reguläre Datei")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _state(root):
    return {rel: _hash(path) for rel, path in _files(root).items()}


def revision(root):
    """Hash actual file content, including uncommitted edits; exclude runtime/cache."""
    entries = {p: h for p, h in _state(Path(root)).items()
               if not p.startswith(("berichte/", ".aeradex/requests/", ".aeradex/kurse/", ".aeradex/erfassung/"))}
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def _head(root):
    from . import gitlog
    if not gitlog.is_repo(root):
        return None
    return gitlog._git(root, "rev-parse", "--verify", "HEAD", check=False).stdout.strip() or None


def _cleanup(root):
    path = root / _RUNTIME
    if path.exists():
        shutil.rmtree(path)
        fsync_directory(path.parent)


def _target(root, rel):
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts or internal(rel):
        raise FormatError("Ungültiger Pfad im Transaktionsmanifest")
    target = root / p
    if not target.resolve().is_relative_to(root):
        raise FormatError("Transaktionspfad verlässt das Buch")
    return target


def recover(root):
    """Undo incomplete publication; retain committed publications. Caller holds lock."""
    from . import gitlog
    root = Path(root)
    folder = root / _RUNTIME
    manifest = folder / "manifest.json"
    if not folder.exists():
        return
    if not manifest.exists():  # preparation cannot have touched the live book
        _cleanup(root)
        return
    data = json.loads(manifest.read_text())
    if data.get("version") != 1:
        raise FormatError("Unbekanntes Transaktionsmanifest; Wiederherstellung abgebrochen")
    committed = data.get("phase") == "committed"
    if not committed and _head(root) != data.get("head") and gitlog.is_repo(root):
        msg = gitlog._git(root, "log", "-1", "--format=%B", check=False).stdout
        committed = f"Aeradex-Transaction: {data['id']}" in msg
    if committed:
        _cleanup(root)
        return
    # Check ALL paths before restoring any, preserving external edits on conflict.
    for rel, entry in data["files"].items():
        current = _hash(_target(root, rel))
        if current not in (entry["old"], entry["new"]):
            raise FormatError(f"Wiederherstellung angehalten: {rel} wurde extern geändert. "
                              f"Sicherung unter {_RUNTIME} erhalten.")
        if entry["old"] is not None and _hash(folder / "before" / rel) != entry["old"]:
            raise FormatError(f"Wiederherstellung: Sicherung für {rel} beschädigt")
    for rel, entry in data["files"].items():
        target = _target(root, rel)
        if entry["old"] is None:
            if target.exists():
                target.unlink()
                fsync_directory(target.parent)
        else:
            atomic_write(target, (folder / "before" / rel).read_bytes())
    # git add may already have run; restore the exact original index, not HEAD.
    index = root / ".git" / "index"
    if data.get("index") and (folder / "index").exists():
        atomic_write(index, (folder / "index").read_bytes())
    elif data.get("repo") and index.exists():
        index.unlink()
    _cleanup(root)


@contextmanager
def locked(root):
    root = Path(root).resolve()
    with _mutex:
        roots = getattr(_held, "roots", set())
        if root in roots or os.environ.get("AERADEX_INTERNAL_CHECK_ROOT") == str(root):
            yield
            return
        path = root / ".aeradex/write.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            _held.roots = roots | {root}
            try:
                recover(root)
                yield
            finally:
                _held.roots = roots
                fcntl.flock(handle, fcntl.LOCK_UN)


def defer_commit(root, message, paths):
    tx = _active.get()
    if tx and Path(root) == tx["work"]:
        tx["messages"].append(message)
        if paths is None:
            tx["commit_all"] = True
        else:
            tx["commit_paths"].update(str(Path(p).resolve().relative_to(root)) for p in paths
                                      if Path(p).resolve().is_relative_to(root))
        return True
    return False


def _remap(value, old, new, strings=True):
    if isinstance(value, Path):
        return new / value.relative_to(old) if value.is_relative_to(old) else value
    if strings and isinstance(value, str) and value.startswith(str(old) + os.sep):
        return str(new / Path(value).relative_to(old))
    if isinstance(value, dict):
        return {k: _remap(v, old, new, strings) for k, v in value.items()}
    if isinstance(value, list):
        return [_remap(v, old, new, strings) for v in value]
    if isinstance(value, tuple):
        return tuple(_remap(v, old, new, strings) for v in value)
    return value


def _request(fn, book, args, kwargs, key):
    from .book import BookError
    if key is None:
        return None, None
    if not isinstance(key, str) or not key.strip() or len(key) > 200:
        raise BookError("idempotency_key muss 1–200 Zeichen enthalten")
    bound = inspect.signature(fn).bind(book, *args, **kwargs)
    bound.apply_defaults()
    params = dict(bound.arguments)
    params.pop(next(iter(inspect.signature(fn).parameters)))
    try:
        payload = json.dumps([fn.__name__, params], sort_keys=True, default=_request_value)
    except TypeError as exc:
        raise BookError("Diese Operation unterstützt keinen idempotency_key") from exc
    digest = hashlib.sha256(payload.encode()).hexdigest()
    path = book.root / ".aeradex/requests" / (hashlib.sha256(key.encode()).hexdigest() + ".json")
    return path, digest


def _request_value(value):
    from datetime import date
    from decimal import Decimal
    if isinstance(value, (Path, Decimal, date)):
        return str(value)
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest()}
    raise TypeError(type(value).__name__)


def transactional(fn):
    @functools.wraps(fn)
    def wrapper(book, *args, **kwargs):
        from . import bookformat, check, gitlog
        from .book import Book, BookError
        expected = kwargs.pop("expected_revision", None)
        key = kwargs.pop("idempotency_key", None)
        active = _active.get()
        if active:
            if book.root != active["work"]:
                raise BookError("Eine Transaktion darf nur ein Buch ändern")
            if expected is not None or key is not None:
                raise BookError("Revision und Idempotenz nur am äusseren Aufruf angeben")
            return fn(book, *args, **kwargs)
        root = book.root
        with locked(root):
            book.reload()
            request_path, request_hash = _request(fn, book, args, kwargs, key)
            if request_path and request_path.exists():
                saved = json.loads(request_path.read_text())
                if saved["request"] != request_hash:
                    raise BookError("idempotency_key wurde bereits für andere Parameter verwendet")
                result = saved["result"]
                if isinstance(result, dict):
                    commit = None
                    if gitlog.is_repo(root):
                        commit = gitlog._git(root, "log", "-1", "--format=%h", "--",
                                             str(request_path.relative_to(root)), check=False).stdout.strip() or None
                    result = {**result, "commit": commit, "wiederholt": True}
                return result
            before = _state(root)
            if expected is not None and expected != revision(root):
                raise BookError("Das Buch wurde inzwischen geändert. Stand neu lesen und Änderung erneut prüfen.")
            folder = root / _RUNTIME
            work = folder / "work"
            folder.mkdir(parents=True, mode=0o700)
            work.mkdir(mode=0o700)
            tx = {"work": work, "messages": [], "commit_paths": set(), "commit_all": False}
            token = _active.set(tx)
            published = False
            try:
                for rel, path in _files(root).items():
                    dest = work / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, dest)
                if _state(work) != before or _state(root) != before:
                    raise BookError("Das Buch wurde während des Einlesens extern geändert")
                staged = Book(work)
                bookformat.migrate(staged)
                tx["commit_paths"].update(p for p in ("aeradex.yaml", ".gitignore")
                                          if _hash(work / p) != before.get(p))
                bound = inspect.signature(fn).bind(staged, *args, **kwargs)
                path_params = {"datei", "source", "path", "target", "pdf_out", "out", "beleg_datei"}
                for name, value in list(bound.arguments.items()):
                    if name != "book":
                        bound.arguments[name] = _remap(value, root, work, strings=name in path_params)
                result = fn(*bound.args, **bound.kwargs)
                staged.reload()
                errors = [i for i in check.run(staged) if i.level == "fehler"]
                if errors:
                    raise BookError("Änderung zurückgewiesen:\n" + "\n".join(map(str, errors)))
                result = _remap(result, work, root)
                if isinstance(result, dict):
                    result["revision"] = revision(work)
                if request_path:
                    saved_path = work / request_path.relative_to(root)
                    atomic_write_text(saved_path, json.dumps({"request": request_hash, "result": result}, ensure_ascii=False))
                    tx["commit_paths"].add(str(request_path.relative_to(root)))
                after = _state(work)
                changes = {rel: {"old": before.get(rel), "new": after.get(rel)}
                           for rel in sorted(before.keys() | after.keys()) if before.get(rel) != after.get(rel)}
                if _state(root) != before:
                    raise BookError("Das Buch wurde während der Bearbeitung extern geändert; nichts übernommen")
                if not changes:
                    return result
                for rel, entry in changes.items():
                    if entry["old"] is not None:
                        atomic_write(folder / "before" / rel, (root / rel).read_bytes())
                    if entry["new"] is not None:
                        # fsync stage before publishing its durable manifest
                        with (work / rel).open("rb") as stream:
                            os.fsync(stream.fileno())
                index = root / ".git/index"
                if index.exists():
                    atomic_write(folder / "index", index.read_bytes())
                manifest = {"version": 1, "id": uuid.uuid4().hex, "phase": "prepared",
                            "head": _head(root), "repo": gitlog.is_repo(root), "index": index.exists(), "files": changes}
                # Persist newly created directory entries as well as file content.
                for base, _, _ in os.walk(folder, topdown=False):
                    fsync_directory(Path(base))
                fsync_directory(folder.parent)
                fsync_directory(root)
                atomic_write_text(folder / "manifest.json", json.dumps(manifest, ensure_ascii=False))
                published = True
                for rel, entry in changes.items():
                    target = _target(root, rel)
                    if _hash(target) != entry["old"]:
                        raise BookError(f"{rel} wurde während der Übernahme extern geändert")
                    if entry["new"] is None:
                        target.unlink()
                        fsync_directory(target.parent)
                    else:
                        atomic_write(target, (work / rel).read_bytes())
                message = "\n".join(dict.fromkeys(tx["messages"])) or f"aeradex: {fn.__name__}"
                # Include changes to already versioned files, especially a receipt's
                # old inbox path after a move. Unchanged user edits are not in changes.
                tracked = set(gitlog._git(root, "ls-files", "-z", check=False).stdout.split("\0")) \
                    if gitlog.is_repo(root) else set()
                commit_paths = [root / p for p in changes if p in tracked or tx["commit_all"] or any(
                    wanted == "." or p == wanted or p.startswith(wanted.rstrip("/") + "/")
                    for wanted in tx["commit_paths"])]
                commit = gitlog.commit(root, message + f"\n\nAeradex-Transaction: {manifest['id']}",
                                       commit_paths)
                manifest["phase"] = "committed"
                atomic_write_text(folder / "manifest.json", json.dumps(manifest, ensure_ascii=False))
                if isinstance(result, dict):
                    result["commit"] = commit
                return result
            except BaseException:
                if published:
                    recover(root)
                raise
            finally:
                _active.reset(token)
                book.reload()
                # If recovery failed, preserve manifest and backups for diagnosis.
                if not published or not (folder / "manifest.json").exists():
                    _cleanup(root)
                elif json.loads((folder / "manifest.json").read_text()).get("phase") == "committed":
                    _cleanup(root)
    wrapper._storage_write = True
    return wrapper


def consistent_read(fn):
    @functools.wraps(fn)
    def wrapper(book=None, *args, **kwargs):
        if book is None or (_active.get() and book.root == _active.get()["work"]):
            return fn(book, *args, **kwargs)
        with locked(book.root):
            book.reload()
            return fn(book, *args, **kwargs)
    return wrapper


def install_read_locks(namespace):
    """Public book API calls share the writer lock, including nested calls."""
    for name, fn in list(namespace.items()):
        if name.startswith("_") or not inspect.isfunction(fn) or fn.__module__ != namespace["__name__"]:
            continue
        params = list(inspect.signature(fn).parameters)
        if params and params[0] == "book" and not getattr(fn, "_storage_write", False):
            namespace[name] = consistent_read(fn)
