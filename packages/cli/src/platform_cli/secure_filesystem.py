"""Contained, no-follow filesystem primitives for customer-repository writes."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from platform_cli.errors import CLIError


def lexical_path(path: Path) -> Path:
    """Return an absolute normalized path without following filesystem symlinks."""

    return Path(os.path.abspath(path.expanduser()))


def confined_path(root: Path, path: Path, *, description: str) -> Path:
    """Normalize ``path`` lexically and require it to stay below canonical ``root``."""

    boundary = root.expanduser().resolve()
    candidate = lexical_path(path if path.is_absolute() else boundary / path)
    try:
        candidate.relative_to(boundary)
    except ValueError as exc:
        raise CLIError(f"{description} must stay inside the selected workspace.") from exc
    return candidate


def assert_no_symlink_components(root: Path, path: Path, *, description: str) -> None:
    """Reject any existing symlink from the trusted root through the path leaf."""

    boundary = root.expanduser().resolve()
    candidate = confined_path(boundary, path, description=description)
    current = boundary
    for part in candidate.relative_to(boundary).parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise CLIError(f"Refusing symlinked {description}: {current}")


def ensure_contained_directory(root: Path, path: Path, *, description: str) -> None:
    """Create a contained directory hierarchy without following symlink components."""

    boundary = root.expanduser().resolve()
    candidate = confined_path(boundary, path, description=description)
    descriptor = _open_directory(boundary, candidate.relative_to(boundary).parts, create=True)
    os.close(descriptor)


@contextmanager
def opened_contained_directory(
    root: Path,
    path: Path,
    *,
    description: str,
    create: bool = False,
) -> Iterator[int]:
    """Yield a no-follow descriptor for a contained directory hierarchy."""

    boundary = root.expanduser().resolve()
    candidate = confined_path(boundary, path, description=description)
    descriptor = _open_directory(
        boundary,
        candidate.relative_to(boundary).parts,
        create=create,
    )
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def assert_open_directory_current(
    root: Path,
    path: Path,
    descriptor: int,
    *,
    description: str,
) -> None:
    """Require the lexical directory path to still name ``descriptor``."""

    boundary = root.expanduser().resolve()
    candidate = confined_path(boundary, path, description=description)
    assert_no_symlink_components(boundary, candidate, description=description)
    try:
        current = os.stat(candidate, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise CLIError(f"{description.capitalize()} changed during the operation.") from exc
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
        opened.st_dev,
        opened.st_ino,
    ):
        raise CLIError(f"{description.capitalize()} changed during the operation.")


def read_text_no_follow(
    root: Path,
    path: Path,
    *,
    description: str,
    encoding: str = "utf-8",
) -> str | None:
    """Read a contained regular file while refusing symlink traversal."""

    boundary = root.expanduser().resolve()
    candidate = confined_path(boundary, path, description=description)
    assert_no_symlink_components(boundary, candidate, description=description)
    try:
        parent_descriptor = _open_directory(
            boundary,
            candidate.parent.relative_to(boundary).parts,
            create=False,
        )
    except FileNotFoundError:
        return None
    try:
        flags = os.O_RDONLY | _flag("O_CLOEXEC") | _flag("O_NOFOLLOW")
        try:
            descriptor = os.open(candidate.name, flags, dir_fd=parent_descriptor)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise CLIError(f"Refusing symlinked {description}: {candidate}") from exc
            raise
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise CLIError(f"{description.capitalize()} must be a regular file.")
            with os.fdopen(descriptor, "r", encoding=encoding) as handle:
                descriptor = -1
                return handle.read()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def read_named_text_files_no_follow(
    root: Path,
    path: Path,
    *,
    filename: str,
    description: str,
    encoding: str = "utf-8",
    max_files: int = 128,
    max_total_bytes: int = 64_000,
) -> list[str]:
    """Read matching files from a contained tree without following symlinks."""

    boundary = root.expanduser().resolve()
    candidate = confined_path(boundary, path, description=description)
    assert_no_symlink_components(boundary, candidate, description=description)
    try:
        directory = _open_directory(
            boundary,
            candidate.relative_to(boundary).parts,
            create=False,
        )
    except FileNotFoundError:
        return []
    try:
        rows: list[tuple[str, str, int]] = []
        _read_named_text_files_from_directory(
            directory,
            relative=Path(),
            filename=filename,
            encoding=encoding,
            rows=rows,
            max_files=max_files,
            max_total_bytes=max_total_bytes,
        )
        assert_open_directory_current(
            boundary,
            candidate,
            directory,
            description=description,
        )
        return [content for _, content, _ in sorted(rows, key=lambda row: row[0])]
    finally:
        os.close(directory)


def atomic_write_text_no_follow(
    root: Path,
    path: Path,
    content: str,
    *,
    description: str,
    mode: int = 0o600,
    encoding: str = "utf-8",
) -> None:
    """Atomically replace a contained file through an opened, no-follow parent."""

    boundary = root.expanduser().resolve()
    candidate = confined_path(boundary, path, description=description)
    assert_no_symlink_components(boundary, candidate, description=description)
    parent_descriptor = _open_directory(
        boundary,
        candidate.parent.relative_to(boundary).parts,
        create=True,
    )
    temporary_name = f".{candidate.name}.{uuid4().hex}.tmp"
    descriptor = -1
    try:
        try:
            existing = os.stat(candidate.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if stat.S_ISLNK(existing.st_mode):
                raise CLIError(f"Refusing symlinked {description}: {candidate}")
            if not stat.S_ISREG(existing.st_mode):
                raise CLIError(f"{description.capitalize()} must be a regular file.")
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | _flag("O_CLOEXEC")
            | _flag("O_NOFOLLOW")
        )
        descriptor = os.open(
            temporary_name,
            flags,
            mode,
            dir_fd=parent_descriptor,
        )
        with os.fdopen(descriptor, "w", encoding=encoding) as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary_name,
            candidate.name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.chmod(candidate.name, mode, dir_fd=parent_descriptor, follow_symlinks=False)
        os.fsync(parent_descriptor)
        assert_open_directory_current(
            boundary,
            candidate.parent,
            parent_descriptor,
            description=f"{description} parent",
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        os.close(parent_descriptor)


def _open_directory(root: Path, parts: tuple[str, ...], *, create: bool) -> int:
    flags = os.O_RDONLY | _flag("O_DIRECTORY") | _flag("O_CLOEXEC") | _flag("O_NOFOLLOW")
    descriptor = os.open(root, flags)
    try:
        for part in parts:
            try:
                metadata = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                metadata = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise CLIError(f"Refusing symlinked path component: {part}")
            if not stat.S_ISDIR(metadata.st_mode):
                raise CLIError(f"Path component is not a directory: {part}")
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.EMLINK}:
                    raise CLIError(f"Refusing symlinked path component: {part}") from exc
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_named_text_files_from_directory(
    directory: int,
    *,
    relative: Path,
    filename: str,
    encoding: str,
    rows: list[tuple[str, str, int]],
    max_files: int,
    max_total_bytes: int,
) -> None:
    """Recursively read matching regular files through opened directory descriptors."""

    flags = os.O_RDONLY | _flag("O_CLOEXEC") | _flag("O_NOFOLLOW")
    directory_flags = flags | _flag("O_DIRECTORY")
    for name in sorted(os.listdir(directory)):
        metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
        child_relative = relative / name
        if stat.S_ISLNK(metadata.st_mode):
            raise CLIError(f"Refusing symlinked skill path: {child_relative}")
        if stat.S_ISDIR(metadata.st_mode):
            child = os.open(name, directory_flags, dir_fd=directory)
            try:
                _read_named_text_files_from_directory(
                    child,
                    relative=child_relative,
                    filename=filename,
                    encoding=encoding,
                    rows=rows,
                    max_files=max_files,
                    max_total_bytes=max_total_bytes,
                )
            finally:
                os.close(child)
            continue
        if name.casefold() != filename.casefold():
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise CLIError(f"Skill file must be a regular file: {child_relative}")
        if len(rows) >= max_files:
            raise CLIError(f"Skill directory exceeds the {max_files}-file safety limit.")
        descriptor = os.open(name, flags, dir_fd=directory)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise CLIError(f"Skill file must be a regular file: {child_relative}")
            current_bytes = sum(row[2] for row in rows)
            if opened.st_size > max_total_bytes - current_bytes:
                raise CLIError(
                    f"Skill directory exceeds the {max_total_bytes}-byte safety limit."
                )
            with os.fdopen(descriptor, "r", encoding=encoding) as handle:
                descriptor = -1
                content = handle.read()
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        size = len(content.encode(encoding))
        if size > max_total_bytes - sum(row[2] for row in rows):
            raise CLIError(f"Skill directory exceeds the {max_total_bytes}-byte safety limit.")
        rows.append((child_relative.as_posix(), content, size))


def _flag(name: str) -> int:
    return int(getattr(os, name, 0))
