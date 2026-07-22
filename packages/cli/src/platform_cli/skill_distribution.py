"""Versioned, integrity-checked distribution for the official launch skill."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.resources import as_file, files
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import uuid4

from platform_cli.errors import CLIError
from platform_cli.secure_filesystem import (
    assert_no_symlink_components,
    assert_open_directory_current,
    confined_path,
    ensure_contained_directory,
    opened_contained_directory,
)

LAUNCH_SKILL_NAME = "generalaugment-launch"
LAUNCH_SKILL_VERSION = "1.2.0"
SKILL_INTEGRITY_SCHEMA_VERSION = "general-augment-skill-integrity/v1"
INSTALLATION_MARKER = ".generalaugment-install.json"
AgentTarget = Literal["codex", "claude"]
InstallScope = Literal["project", "user"]


@contextmanager
def bundled_launch_skill() -> Iterator[Path]:
    """Yield the bundled launch-skill directory from source or an installed wheel."""

    resource = files("platform_cli").joinpath("bundled_skills", LAUNCH_SKILL_NAME)
    with as_file(resource) as resolved:
        path = Path(resolved)
        if path.is_dir():
            yield path
            return
    source_fallback = Path(__file__).resolve().parents[4] / "skills" / LAUNCH_SKILL_NAME
    if not source_fallback.is_dir():
        raise CLIError("The installed CLI artifact does not contain the launch skill bundle.")
    yield source_fallback


def install_launch_skill(
    *,
    source: Path,
    agent: AgentTarget,
    scope: InstallScope,
    workspace: Path,
    requested_version: str,
    force: bool = False,
    home: Path | None = None,
) -> dict[str, Any]:
    """Verify and atomically install one managed launch skill."""

    integrity = verify_skill_bundle(source)
    available_version = str(integrity.get("skill_version") or "")
    if requested_version != available_version:
        raise CLIError(
            f"Requested launch skill {requested_version}, but this CLI bundles "
            f"{available_version}. "
            "Install a compatible general-augment-cli version."
        )
    if scope == "project":
        return _install_project_launch_skill(
            source=source,
            agent=agent,
            workspace=workspace,
            available_version=available_version,
            integrity=integrity,
            force=force,
        )
    target = skill_target_path(
        agent=agent,
        scope=scope,
        workspace=workspace,
        home=home,
    )
    _prepare_skill_parent(scope=scope, workspace=workspace, target=target)
    previous = _installation_marker(target)
    if target.exists() and previous is None and not force:
        raise CLIError(
            f"Refusing to replace unmanaged skill directory: {target}. "
            "Move it first or rerun with --force after reviewing its contents."
        )
    _assert_project_skill_path(scope=scope, workspace=workspace, target=target)
    staged = Path(tempfile.mkdtemp(prefix=f".{LAUNCH_SKILL_NAME}-", dir=target.parent))
    backup = target.parent / f".{LAUNCH_SKILL_NAME}.backup-{uuid4().hex}"
    try:
        _copy_verified_bundle(source, staged, integrity)
        marker = {
            "schema_version": "general-augment-skill-installation/v1",
            "name": LAUNCH_SKILL_NAME,
            "version": available_version,
            "source": "general-augment-cli",
            "agent": agent,
            "scope": scope,
            "bundle_sha256": _bundle_digest(integrity),
            "installed_at": _iso_now(),
        }
        (staged / INSTALLATION_MARKER).write_text(
            json.dumps(marker, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _restrict_tree(staged)
        _assert_project_skill_path(scope=scope, workspace=workspace, target=target)
        if target.exists():
            _assert_project_skill_path(scope=scope, workspace=workspace, target=backup)
            os.replace(target, backup)
        _assert_project_skill_path(scope=scope, workspace=workspace, target=staged)
        os.replace(staged, target)
        if backup.exists():
            _assert_project_skill_path(scope=scope, workspace=workspace, target=backup)
            shutil.rmtree(backup)
    except Exception:
        if target.exists() and backup.exists():
            _assert_project_skill_path(scope=scope, workspace=workspace, target=target)
            shutil.rmtree(target)
        if backup.exists():
            _assert_project_skill_path(scope=scope, workspace=workspace, target=backup)
            os.replace(backup, target)
        if staged.exists():
            _assert_project_skill_path(scope=scope, workspace=workspace, target=staged)
            shutil.rmtree(staged)
        raise
    return {
        "agent": agent,
        "scope": scope,
        "path": str(target),
        "version": available_version,
        "integrity": "verified",
        "bundle_sha256": _bundle_digest(integrity),
        "action": "upgraded" if previous else "installed",
    }


def remove_launch_skill(
    *,
    agent: AgentTarget,
    scope: InstallScope,
    workspace: Path,
    force: bool = False,
    home: Path | None = None,
) -> dict[str, Any]:
    """Remove one managed launch-skill installation."""

    target = skill_target_path(agent=agent, scope=scope, workspace=workspace, home=home)
    if scope == "project":
        return _remove_project_launch_skill(
            agent=agent,
            workspace=workspace,
            target=target,
            force=force,
        )
    if not target.exists():
        return {
            "agent": agent,
            "scope": scope,
            "path": str(target),
            "action": "already_absent",
        }
    marker = _installation_marker(target)
    if marker is None and not force:
        raise CLIError(
            f"Refusing to remove unmanaged skill directory: {target}. "
            "Use --force only after reviewing its contents."
        )
    _assert_project_skill_path(scope=scope, workspace=workspace, target=target)
    shutil.rmtree(target)
    return {"agent": agent, "scope": scope, "path": str(target), "action": "removed"}


def launch_skill_status(
    *,
    source: Path,
    agent: AgentTarget,
    scope: InstallScope,
    workspace: Path,
    home: Path | None = None,
) -> dict[str, Any]:
    """Report installation and integrity status without changing files."""

    bundled = verify_skill_bundle(source)
    target = skill_target_path(agent=agent, scope=scope, workspace=workspace, home=home)
    if scope == "project":
        return _project_launch_skill_status(
            bundled=bundled,
            agent=agent,
            workspace=workspace,
            target=target,
        )
    marker = _installation_marker(target)
    installed_integrity = "absent"
    if target.is_dir():
        try:
            verify_skill_bundle(target)
        except CLIError:
            installed_integrity = "invalid"
        else:
            installed_integrity = "verified"
    return {
        "agent": agent,
        "scope": scope,
        "path": str(target),
        "installed": target.is_dir(),
        "managed": marker is not None,
        "installed_version": marker.get("version") if marker else None,
        "installed_integrity": installed_integrity,
        "bundled_version": bundled["skill_version"],
        "bundled_sha256": _bundle_digest(bundled),
    }


def skill_target_path(
    *,
    agent: AgentTarget,
    scope: InstallScope,
    workspace: Path,
    home: Path | None = None,
) -> Path:
    """Resolve the documented Codex or Claude Code skill destination."""

    if scope == "project":
        root = workspace.expanduser().resolve()
        vendor = ".codex" if agent == "codex" else ".claude"
        target = confined_path(
            root,
            root / vendor / "skills" / LAUNCH_SKILL_NAME,
            description="project launch skill path",
        )
        assert_no_symlink_components(
            root,
            target,
            description="project launch skill path",
        )
        return target
    home_path = (home or Path.home()).expanduser().resolve()
    if agent == "codex":
        codex_home = Path(os.getenv("CODEX_HOME", str(home_path / ".codex"))).expanduser()
        return codex_home.resolve() / "skills" / LAUNCH_SKILL_NAME
    return home_path / ".claude" / "skills" / LAUNCH_SKILL_NAME


def verify_skill_bundle(source: Path) -> dict[str, Any]:
    """Verify the bundle metadata and every declared file digest."""

    root = source.expanduser().resolve()
    integrity_path = root / "integrity.json"
    try:
        integrity = json.loads(integrity_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CLIError("Launch skill integrity metadata is missing or malformed.") from exc
    if not isinstance(integrity, dict):
        raise CLIError("Launch skill integrity metadata must be a JSON object.")
    if integrity.get("schema_version") != SKILL_INTEGRITY_SCHEMA_VERSION:
        raise CLIError("Launch skill integrity metadata uses an unsupported schema.")
    if integrity.get("skill_version") != LAUNCH_SKILL_VERSION:
        raise CLIError("Launch skill integrity metadata does not match the CLI skill version.")
    declared = integrity.get("files")
    if not isinstance(declared, dict) or not declared:
        raise CLIError("Launch skill integrity metadata does not declare files.")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if (path.is_file() or path.is_symlink())
        and path.name not in {"integrity.json", INSTALLATION_MARKER}
        and not _is_generated_python_cache(path, root)
    }
    if actual != set(declared):
        raise CLIError("Launch skill file set does not match its integrity metadata.")
    for relative, expected in declared.items():
        safe_relative = _safe_relative_path(str(relative))
        path = root / safe_relative
        if path.is_symlink() or not path.is_file():
            raise CLIError(f"Launch skill file is missing or unsafe: {relative}")
        actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_digest != str(expected):
            raise CLIError(f"Launch skill integrity check failed: {relative}")
    return integrity


def _copy_verified_bundle(source: Path, target: Path, integrity: Mapping[str, Any]) -> None:
    declared = integrity.get("files")
    if not isinstance(declared, dict):
        raise CLIError("Launch skill integrity metadata does not declare files.")
    for relative in declared:
        safe_relative = _safe_relative_path(str(relative))
        source_path = source / safe_relative
        target_path = target / safe_relative
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target_path)
    shutil.copyfile(source / "integrity.json", target / "integrity.json")


def _safe_relative_path(value: str) -> Path:
    relative = PurePosixPath(value)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise CLIError(f"Launch skill integrity path is unsafe: {value}")
    return Path(*relative.parts)


def _is_generated_python_cache(path: Path, root: Path) -> bool:
    """Ignore only regular interpreter cache files created during wheel installation."""

    if path.is_symlink() or path.suffix != ".pyc":
        return False
    relative = path.relative_to(root)
    return "__pycache__" in relative.parts


def _installation_marker(target: Path) -> dict[str, Any] | None:
    path = target / INSTALLATION_MARKER
    if not path.is_file() or path.is_symlink():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("name") != LAUNCH_SKILL_NAME or payload.get("source") != "general-augment-cli":
        return None
    return payload


def _bundle_digest(integrity: Mapping[str, Any]) -> str:
    files_value = integrity.get("files")
    file_rows = dict(files_value) if isinstance(files_value, dict) else {}
    canonical = json.dumps(file_rows, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _restrict_tree(root: Path) -> None:
    if os.name != "posix":
        return
    for path in root.rglob("*"):
        if path.is_dir():
            path.chmod(0o700)
        elif path.parent.name == "scripts":
            path.chmod(0o700)
        else:
            path.chmod(0o600)
    root.chmod(0o700)


def _install_project_launch_skill(
    *,
    source: Path,
    agent: AgentTarget,
    workspace: Path,
    available_version: str,
    integrity: Mapping[str, Any],
    force: bool,
) -> dict[str, Any]:
    """Install a project skill using only no-follow descriptor-relative mutations."""

    root = workspace.expanduser().resolve()
    target = skill_target_path(agent=agent, scope="project", workspace=root)
    ensure_contained_directory(
        root,
        target.parent,
        description="project launch skill path",
    )
    stage_name = f".{LAUNCH_SKILL_NAME}.stage-{uuid4().hex}"
    backup_name = f".{LAUNCH_SKILL_NAME}.backup-{uuid4().hex}"
    target_name = target.name
    backup_created = False
    installed = False
    with opened_contained_directory(
        root,
        target.parent,
        description="project launch skill path",
    ) as parent_descriptor:
        assert_open_directory_current(
            root,
            target.parent,
            parent_descriptor,
            description="project launch skill parent",
        )
        initial = _entry_stat(parent_descriptor, target_name)
        if initial is not None and not stat.S_ISDIR(initial.st_mode):
            raise CLIError(f"Refusing unsafe project launch skill target: {target}")
        previous = _installation_marker_at(parent_descriptor, target_name)
        if initial is not None and previous is None and not force:
            raise CLIError(
                f"Refusing to replace unmanaged skill directory: {target}. "
                "Move it first or rerun with --force after reviewing its contents."
            )
        os.mkdir(stage_name, mode=0o700, dir_fd=parent_descriptor)
        try:
            stage_descriptor = _open_directory_at(parent_descriptor, stage_name)
            try:
                _copy_verified_bundle_at(source, stage_descriptor, integrity)
                marker = {
                    "schema_version": "general-augment-skill-installation/v1",
                    "name": LAUNCH_SKILL_NAME,
                    "version": available_version,
                    "source": "general-augment-cli",
                    "agent": agent,
                    "scope": "project",
                    "bundle_sha256": _bundle_digest(integrity),
                    "installed_at": _iso_now(),
                }
                _write_file_at(
                    stage_descriptor,
                    (INSTALLATION_MARKER,),
                    (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode(),
                    mode=0o600,
                )
                os.fsync(stage_descriptor)
            finally:
                os.close(stage_descriptor)

            assert_open_directory_current(
                root,
                target.parent,
                parent_descriptor,
                description="project launch skill parent",
            )
            _assert_entry_unchanged(parent_descriptor, target_name, initial)
            if initial is not None:
                os.replace(
                    target_name,
                    backup_name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
                backup_created = True
            os.replace(
                stage_name,
                target_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            installed = True
            try:
                assert_open_directory_current(
                    root,
                    target.parent,
                    parent_descriptor,
                    description="project launch skill parent",
                )
            except Exception:
                _remove_tree_at(parent_descriptor, target_name)
                installed = False
                if backup_created:
                    os.replace(
                        backup_name,
                        target_name,
                        src_dir_fd=parent_descriptor,
                        dst_dir_fd=parent_descriptor,
                    )
                    backup_created = False
                raise
            if backup_created:
                _remove_tree_at(parent_descriptor, backup_name)
                backup_created = False
            os.fsync(parent_descriptor)
        except Exception:
            if installed:
                _remove_tree_at(parent_descriptor, target_name)
            if backup_created:
                os.replace(
                    backup_name,
                    target_name,
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=parent_descriptor,
                )
            _remove_tree_at(parent_descriptor, stage_name, missing_ok=True)
            raise
    return {
        "agent": agent,
        "scope": "project",
        "path": str(target),
        "version": available_version,
        "integrity": "verified",
        "bundle_sha256": _bundle_digest(integrity),
        "action": "upgraded" if previous else "installed",
    }


def _remove_project_launch_skill(
    *,
    agent: AgentTarget,
    workspace: Path,
    target: Path,
    force: bool,
) -> dict[str, Any]:
    """Remove a project skill without following a replaced parent or target."""

    root = workspace.expanduser().resolve()
    try:
        context = opened_contained_directory(
            root,
            target.parent,
            description="project launch skill path",
        )
        with context as parent_descriptor:
            assert_open_directory_current(
                root,
                target.parent,
                parent_descriptor,
                description="project launch skill parent",
            )
            metadata = _entry_stat(parent_descriptor, target.name)
            if metadata is None:
                return _absent_skill_result(agent, target)
            if not stat.S_ISDIR(metadata.st_mode):
                raise CLIError(f"Refusing unsafe project launch skill target: {target}")
            marker = _installation_marker_at(parent_descriptor, target.name)
            if marker is None and not force:
                raise CLIError(
                    f"Refusing to remove unmanaged skill directory: {target}. "
                    "Use --force only after reviewing its contents."
                )
            _assert_entry_unchanged(parent_descriptor, target.name, metadata)
            _remove_tree_at(parent_descriptor, target.name)
            assert_open_directory_current(
                root,
                target.parent,
                parent_descriptor,
                description="project launch skill parent",
            )
            os.fsync(parent_descriptor)
    except FileNotFoundError:
        return _absent_skill_result(agent, target)
    return {"agent": agent, "scope": "project", "path": str(target), "action": "removed"}


def _project_launch_skill_status(
    *,
    bundled: Mapping[str, Any],
    agent: AgentTarget,
    workspace: Path,
    target: Path,
) -> dict[str, Any]:
    """Inspect a project installation through a stable no-follow parent descriptor."""

    root = workspace.expanduser().resolve()
    marker: dict[str, Any] | None = None
    installed = False
    installed_integrity = "absent"
    try:
        with opened_contained_directory(
            root,
            target.parent,
            description="project launch skill path",
        ) as parent_descriptor:
            assert_open_directory_current(
                root,
                target.parent,
                parent_descriptor,
                description="project launch skill parent",
            )
            metadata = _entry_stat(parent_descriptor, target.name)
            if metadata is not None:
                if not stat.S_ISDIR(metadata.st_mode):
                    raise CLIError(f"Refusing unsafe project launch skill target: {target}")
                installed = True
                marker = _installation_marker_at(parent_descriptor, target.name)
                installed_integrity = (
                    "verified"
                    if _installed_bundle_matches_at(parent_descriptor, target.name)
                    else "invalid"
                )
            assert_open_directory_current(
                root,
                target.parent,
                parent_descriptor,
                description="project launch skill parent",
            )
    except FileNotFoundError:
        pass
    return {
        "agent": agent,
        "scope": "project",
        "path": str(target),
        "installed": installed,
        "managed": marker is not None,
        "installed_version": marker.get("version") if marker else None,
        "installed_integrity": installed_integrity,
        "bundled_version": bundled["skill_version"],
        "bundled_sha256": _bundle_digest(bundled),
    }


def _copy_verified_bundle_at(
    source: Path,
    target_descriptor: int,
    integrity: Mapping[str, Any],
) -> None:
    declared = integrity.get("files")
    if not isinstance(declared, dict):
        raise CLIError("Launch skill integrity metadata does not declare files.")
    for relative in declared:
        safe_relative = _safe_relative_path(str(relative))
        mode = 0o700 if safe_relative.parent.name == "scripts" else 0o600
        _write_file_at(
            target_descriptor,
            safe_relative.parts,
            (source / safe_relative).read_bytes(),
            mode=mode,
        )
    _write_file_at(
        target_descriptor,
        ("integrity.json",),
        (source / "integrity.json").read_bytes(),
        mode=0o600,
    )


def _installed_bundle_matches_at(parent_descriptor: int, target_name: str) -> bool:
    try:
        target_descriptor = _open_directory_at(parent_descriptor, target_name)
    except (FileNotFoundError, NotADirectoryError, OSError):
        return False
    try:
        raw_integrity = _read_file_at(target_descriptor, ("integrity.json",))
        integrity = json.loads(raw_integrity)
        if not isinstance(integrity, dict):
            return False
        if integrity.get("schema_version") != SKILL_INTEGRITY_SCHEMA_VERSION:
            return False
        if integrity.get("skill_version") != LAUNCH_SKILL_VERSION:
            return False
        declared = integrity.get("files")
        if not isinstance(declared, dict) or not declared:
            return False
        actual = _tree_files_at(target_descriptor)
        expected = set(declared) | {"integrity.json"}
        if actual - {INSTALLATION_MARKER} != expected:
            return False
        for relative, expected_digest in declared.items():
            safe_relative = _safe_relative_path(str(relative))
            content = _read_file_at(target_descriptor, safe_relative.parts)
            if hashlib.sha256(content).hexdigest() != str(expected_digest):
                return False
        return True
    except (CLIError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    finally:
        os.close(target_descriptor)


def _installation_marker_at(parent_descriptor: int, target_name: str) -> dict[str, Any] | None:
    try:
        target_descriptor = _open_directory_at(parent_descriptor, target_name)
    except (FileNotFoundError, NotADirectoryError, OSError):
        return None
    try:
        payload = json.loads(_read_file_at(target_descriptor, (INSTALLATION_MARKER,)))
    except (FileNotFoundError, CLIError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    finally:
        os.close(target_descriptor)
    if not isinstance(payload, dict):
        return None
    if payload.get("name") != LAUNCH_SKILL_NAME or payload.get("source") != "general-augment-cli":
        return None
    return payload


def _write_file_at(
    root_descriptor: int,
    parts: tuple[str, ...],
    content: bytes,
    *,
    mode: int,
) -> None:
    if not parts:
        raise CLIError("Launch skill file path is empty.")
    descriptor = os.dup(root_descriptor)
    try:
        for part in parts[:-1]:
            metadata = _entry_stat(descriptor, part)
            if metadata is None:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            elif not stat.S_ISDIR(metadata.st_mode):
                raise CLIError(f"Launch skill path component is unsafe: {part}")
            child = _open_directory_at(descriptor, part)
            os.close(descriptor)
            descriptor = child
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _flag("O_CLOEXEC") | _flag("O_NOFOLLOW")
        file_descriptor = os.open(parts[-1], flags, mode, dir_fd=descriptor)
        try:
            view = memoryview(content)
            while view:
                written = os.write(file_descriptor, view)
                view = view[written:]
            os.fsync(file_descriptor)
        finally:
            os.close(file_descriptor)
    finally:
        os.close(descriptor)


def _read_file_at(root_descriptor: int, parts: tuple[str, ...]) -> bytes:
    descriptor = os.dup(root_descriptor)
    try:
        for part in parts[:-1]:
            child = _open_directory_at(descriptor, part)
            os.close(descriptor)
            descriptor = child
        flags = os.O_RDONLY | _flag("O_CLOEXEC") | _flag("O_NOFOLLOW")
        file_descriptor = os.open(parts[-1], flags, dir_fd=descriptor)
        try:
            if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
                raise CLIError("Launch skill file is not a regular file.")
            chunks: list[bytes] = []
            while chunk := os.read(file_descriptor, 65536):
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(file_descriptor)
    finally:
        os.close(descriptor)


def _tree_files_at(root_descriptor: int, prefix: tuple[str, ...] = ()) -> set[str]:
    files: set[str] = set()
    for name in os.listdir(root_descriptor):
        metadata = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
        relative = (*prefix, name)
        if stat.S_ISDIR(metadata.st_mode):
            child = _open_directory_at(root_descriptor, name)
            try:
                files.update(_tree_files_at(child, relative))
            finally:
                os.close(child)
        elif stat.S_ISREG(metadata.st_mode):
            if name.endswith(".pyc") and "__pycache__" in relative:
                continue
            files.add(PurePosixPath(*relative).as_posix())
        else:
            files.add(PurePosixPath(*relative).as_posix())
    return files


def _remove_tree_at(parent_descriptor: int, name: str, *, missing_ok: bool = False) -> None:
    metadata = _entry_stat(parent_descriptor, name)
    if metadata is None:
        if missing_ok:
            return
        raise FileNotFoundError(name)
    if not stat.S_ISDIR(metadata.st_mode):
        raise CLIError(f"Refusing unsafe project launch skill target: {name}")
    descriptor = _open_directory_at(parent_descriptor, name)
    try:
        for child_name in os.listdir(descriptor):
            child = os.stat(child_name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(child.st_mode):
                _remove_tree_at(descriptor, child_name)
            else:
                os.unlink(child_name, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent_descriptor)


def _open_directory_at(parent_descriptor: int, name: str) -> int:
    flags = os.O_RDONLY | _flag("O_DIRECTORY") | _flag("O_CLOEXEC") | _flag("O_NOFOLLOW")
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise CLIError(f"Refusing symlinked project launch skill path: {name}") from exc
        raise
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise CLIError(f"Project launch skill path is not a directory: {name}")
    return descriptor


def _entry_stat(parent_descriptor: int, name: str) -> os.stat_result | None:
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode):
        raise CLIError(f"Refusing symlinked project launch skill path: {name}")
    return metadata


def _assert_entry_unchanged(
    parent_descriptor: int,
    name: str,
    expected: os.stat_result | None,
) -> None:
    current = _entry_stat(parent_descriptor, name)
    if expected is None:
        if current is not None:
            raise CLIError("Project launch skill target changed during the operation.")
        return
    if current is None or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
        raise CLIError("Project launch skill target changed during the operation.")


def _absent_skill_result(agent: AgentTarget, target: Path) -> dict[str, Any]:
    return {
        "agent": agent,
        "scope": "project",
        "path": str(target),
        "action": "already_absent",
    }


def _flag(name: str) -> int:
    return int(getattr(os, name, 0))


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _prepare_skill_parent(*, scope: InstallScope, workspace: Path, target: Path) -> None:
    """Create a project target safely or retain user-scope compatibility."""

    if scope == "project":
        root = workspace.expanduser().resolve()
        ensure_contained_directory(
            root,
            target.parent,
            description="project launch skill path",
        )
        return
    target.parent.mkdir(parents=True, exist_ok=True)


def _assert_project_skill_path(
    *,
    scope: InstallScope,
    workspace: Path,
    target: Path,
) -> None:
    """Recheck project containment immediately before a filesystem mutation."""

    if scope != "project":
        return
    root = workspace.expanduser().resolve()
    assert_no_symlink_components(
        root,
        target,
        description="project launch skill path",
    )
