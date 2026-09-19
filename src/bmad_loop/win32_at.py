"""Handle-relative filesystem primitives for Windows — the ``*at()`` family
the anchored writers are built on, where CPython offers none.

POSIX anchors a write to an open directory descriptor: ``openat``/``renameat``
/``unlinkat`` name a single component *relative to that descriptor*, so a
concurrent rename of any directory along the original path renames something
the writer no longer consults. CPython exposes that family as ``dir_fd=``, and
implements it with the ``*at`` calls only — ``os.supports_dir_fd`` is empty on
Windows. The kernel underneath Win32 has the same primitive, though: every NT
open takes an ``OBJECT_ATTRIBUTES`` whose ``RootDirectory`` is a handle the
name is resolved against, and a rename or delete is a ``NtSetInformationFile``
on the file's own handle, with the destination again spelled relative to a
handle. This module is the thin ``ctypes`` binding of exactly those calls:

- :func:`open_directory` — a directory handle, wrapped into a CRT descriptor;
- :func:`open_at` — ``NtCreateFile`` relative to that descriptor, with the
  ``O_NOFOLLOW`` / ``O_DIRECTORY`` / ``O_CREAT|O_EXCL`` flag vocabulary the
  POSIX callers already speak;
- :func:`stat_at` — an ``lstat`` of one entry, taken through a handle;
- :func:`replace_at` / :func:`unlink_at` — ``FileRenameInformation`` /
  ``FileDispositionInformation`` relative to a directory handle.

Every handle comes back as a CRT file descriptor (``msvcrt.open_osfhandle``),
so the callers keep using ``os.fstat``, ``os.read``, ``os.fsync``,
``os.fdopen`` and ``os.close`` on it unchanged — including
``os.path.samestat``, whose ``st_ino``/``st_dev`` CPython fills from the
handle's file index and volume serial. The one thing no CRT call gives back
is the handle-relative open itself, which is why this module exists.

"Do not follow" is ``FILE_OPEN_REPARSE_POINT`` plus an attribute check on the
opened handle: the open lands on the reparse point itself, and a symlink or
mount point there is refused with ``ELOOP`` exactly as ``O_NOFOLLOW`` refuses
it. Only those two tags count as links — cloud placeholders and dedup stubs
are reparse points too, and refusing them would stall a legitimate run
(``platform_util._LINK_REPARSE_TAGS`` draws the same line).

Errors are ordinary ``OSError``s: the NTSTATUS is mapped through
``RtlNtStatusToDosError`` and handed to ``OSError`` as ``winerror``, which
CPython turns into the errno and subclass Win32 would have produced
(``FileNotFoundError``, ``FileExistsError``, ``PermissionError`` for a sharing
violation, ...). Two are pinned by hand because the Win32 mapping loses them:
``STATUS_FILE_IS_A_DIRECTORY`` is ``IsADirectoryError`` and
``STATUS_NOT_A_DIRECTORY`` is ``NotADirectoryError``, the answers the POSIX
arm's callers branch on.

Importable everywhere; every function raises ``OSError(ENOSYS)`` off
Windows, and :data:`AVAILABLE` says which arm the host is on. The bindings
themselves are only built under ``sys.platform == "win32"``.
"""

from __future__ import annotations

import errno
import os
import stat
import struct
import sys
from pathlib import Path

AVAILABLE = sys.platform == "win32"

# The flag vocabulary `open_at` understands, spelled for the POSIX callers.
# On POSIX these ARE `os.O_NOFOLLOW` etc.; here they are bits the CRT's own
# `O_*` set leaves free (it uses the low 16), so an `os.O_RDWR | AT_NOFOLLOW`
# composed by a caller means the same thing on both arms.
AT_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0x0010_0000)
AT_NONBLOCK = getattr(os, "O_NONBLOCK", 0x0020_0000)
AT_DIRECTORY = getattr(os, "O_DIRECTORY", 0x0040_0000)

_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_IO_REPARSE_TAG_SYMLINK = 0xA000000C
_LINK_TAGS = frozenset({_IO_REPARSE_TAG_MOUNT_POINT, _IO_REPARSE_TAG_SYMLINK})
_FILE_ATTRIBUTE_DIRECTORY = 0x10
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _unsupported() -> OSError:
    return OSError(errno.ENOSYS, "handle-relative file operations are Windows-only")


if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as wt
    import msvcrt

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    # --- access rights / options (winnt.h, ntifs.h) ---------------------------
    _DELETE = 0x0001_0000
    _SYNCHRONIZE = 0x0010_0000
    _FILE_READ_DATA = 0x0001  # == FILE_LIST_DIRECTORY on a directory
    _FILE_WRITE_DATA = 0x0002
    _FILE_APPEND_DATA = 0x0004
    _FILE_READ_EA = 0x0008
    _FILE_WRITE_EA = 0x0010
    _FILE_TRAVERSE = 0x0020
    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_WRITE_ATTRIBUTES = 0x0100
    _READ_CONTROL = 0x0002_0000
    _FILE_GENERIC_READ = (
        _READ_CONTROL | _FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _FILE_READ_EA | _SYNCHRONIZE
    )
    _FILE_GENERIC_WRITE = (
        _READ_CONTROL
        | _FILE_WRITE_DATA
        | _FILE_WRITE_ATTRIBUTES
        | _FILE_WRITE_EA
        | _FILE_APPEND_DATA
        | _SYNCHRONIZE
    )
    _FILE_SHARE_ALL = 0x1 | 0x2 | 0x4  # READ | WRITE | DELETE
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _FILE_OPEN = 1
    _FILE_CREATE = 2
    _FILE_OPEN_IF = 3
    _FILE_DIRECTORY_FILE = 0x0001
    _FILE_NON_DIRECTORY_FILE = 0x0040
    _FILE_SYNCHRONOUS_IO_NONALERT = 0x0020
    _FILE_OPEN_REPARSE_POINT = 0x0020_0000
    _OBJ_CASE_INSENSITIVE = 0x40
    _OPEN_EXISTING = 3
    _FILE_FLAG_BACKUP_SEMANTICS = 0x0200_0000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x0020_0000
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    # NTSTATUS values with a dedicated translation.
    _STATUS_FILE_IS_A_DIRECTORY = 0xC00000BA
    _STATUS_NOT_A_DIRECTORY = 0xC0000103
    # "This information class is not one this kernel/volume takes": the `Ex`
    # rename/dispose classes are Windows 10 1709+ and NTFS/ReFS only.
    _STATUS_INVALID_INFO_CLASS = 0xC0000003
    _STATUS_INVALID_PARAMETER = 0xC000000D
    _STATUS_NOT_SUPPORTED = 0xC00000BB
    _EX_CLASS_UNSUPPORTED = frozenset(
        {_STATUS_INVALID_INFO_CLASS, _STATUS_INVALID_PARAMETER, _STATUS_NOT_SUPPORTED}
    )

    # FILE_INFORMATION_CLASS members (ntifs.h) and the FILE_INFO_BY_HANDLE_CLASS
    # member GetFileInformationByHandleEx takes.
    _FileRenameInformation = 10
    _FileDispositionInformation = 13
    _FileDispositionInformationEx = 64
    _FileRenameInformationEx = 65
    _FileAttributeTagInfo = 9
    _FILE_RENAME_REPLACE_IF_EXISTS = 0x1
    _FILE_RENAME_POSIX_SEMANTICS = 0x2
    _FILE_DISPOSITION_DELETE = 0x1
    _FILE_DISPOSITION_POSIX_SEMANTICS = 0x2

    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = (
            ("Length", wt.USHORT),
            ("MaximumLength", wt.USHORT),
            ("Buffer", wt.LPWSTR),
        )

    class _OBJECT_ATTRIBUTES(ctypes.Structure):
        _fields_ = (
            ("Length", wt.ULONG),
            ("RootDirectory", wt.HANDLE),
            ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
            ("Attributes", wt.ULONG),
            ("SecurityDescriptor", ctypes.c_void_p),
            ("SecurityQualityOfService", ctypes.c_void_p),
        )

    class _IO_STATUS_BLOCK(ctypes.Structure):
        _fields_ = (("Status", ctypes.c_void_p), ("Information", ctypes.c_void_p))

    class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
        _fields_ = (("FileAttributes", wt.DWORD), ("ReparseTag", wt.DWORD))

    _ntdll.NtCreateFile.restype = ctypes.c_long
    _ntdll.NtCreateFile.argtypes = (
        ctypes.POINTER(wt.HANDLE),
        wt.DWORD,
        ctypes.POINTER(_OBJECT_ATTRIBUTES),
        ctypes.POINTER(_IO_STATUS_BLOCK),
        ctypes.c_void_p,
        wt.ULONG,
        wt.ULONG,
        wt.ULONG,
        wt.ULONG,
        ctypes.c_void_p,
        wt.ULONG,
    )
    _ntdll.NtSetInformationFile.restype = ctypes.c_long
    _ntdll.NtSetInformationFile.argtypes = (
        wt.HANDLE,
        ctypes.POINTER(_IO_STATUS_BLOCK),
        ctypes.c_void_p,
        wt.ULONG,
        ctypes.c_int,
    )
    _ntdll.RtlNtStatusToDosError.restype = wt.ULONG
    _ntdll.RtlNtStatusToDosError.argtypes = (wt.ULONG,)
    _kernel32.CreateFileW.restype = wt.HANDLE
    _kernel32.CreateFileW.argtypes = (
        wt.LPCWSTR,
        wt.DWORD,
        wt.DWORD,
        ctypes.c_void_p,
        wt.DWORD,
        wt.DWORD,
        wt.HANDLE,
    )
    _kernel32.GetFileInformationByHandleEx.restype = wt.BOOL
    _kernel32.GetFileInformationByHandleEx.argtypes = (
        wt.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wt.DWORD,
    )
    _kernel32.CloseHandle.restype = wt.BOOL
    _kernel32.CloseHandle.argtypes = (wt.HANDLE,)

    def _nt_error(status: int, name: str) -> OSError:
        """The ``OSError`` Win32 would have raised for this NTSTATUS."""
        status &= 0xFFFFFFFF
        if status == _STATUS_FILE_IS_A_DIRECTORY:
            return IsADirectoryError(errno.EISDIR, "Is a directory", name)
        if status == _STATUS_NOT_A_DIRECTORY:
            return NotADirectoryError(errno.ENOTDIR, "Not a directory", name)
        winerror = int(_ntdll.RtlNtStatusToDosError(status))
        # `winerror` decides the errno AND the subclass; the 0 is discarded.
        return OSError(0, ctypes.FormatError(winerror).strip(), name, winerror)

    def _win_error(name: str) -> OSError:
        winerror = ctypes.get_last_error()
        return OSError(0, ctypes.FormatError(winerror).strip(), name, winerror)

    def _handle_of(fd: int) -> int:
        return msvcrt.get_osfhandle(fd)

    def _wrap(handle: int, *, writable: bool) -> int:
        """Hand a raw handle to the CRT; from here on ``os.close`` owns it."""
        flags = os.O_BINARY | os.O_NOINHERIT | (os.O_RDWR if writable else os.O_RDONLY)
        try:
            return msvcrt.open_osfhandle(handle, flags)
        except OSError:
            _kernel32.CloseHandle(handle)
            raise

    def _single_component(name: str) -> None:
        if not name or name in (".", "..") or "\\" in name or "/" in name or "\x00" in name:
            raise ValueError(f"not a single path component: {name!r}")

    def _attribute_tag(handle: int, name: str) -> tuple[int, int]:
        info = _FILE_ATTRIBUTE_TAG_INFO()
        if not _kernel32.GetFileInformationByHandleEx(
            handle, _FileAttributeTagInfo, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise _win_error(name)
        return int(info.FileAttributes), int(info.ReparseTag)

    def _is_link(attributes: int, tag: int) -> bool:
        return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT) and tag in _LINK_TAGS

    def _nt_open(
        root_handle: int | None,
        name: str,
        *,
        access: int,
        disposition: int,
        options: int,
        attributes: int = _FILE_ATTRIBUTE_NORMAL,
    ) -> int:
        buf = ctypes.create_unicode_buffer(name)
        length = len(name) * 2
        unicode = _UNICODE_STRING(length, length + 2, ctypes.cast(buf, wt.LPWSTR))
        attrs = _OBJECT_ATTRIBUTES(
            ctypes.sizeof(_OBJECT_ATTRIBUTES),
            root_handle,
            ctypes.pointer(unicode),
            _OBJ_CASE_INSENSITIVE,
            None,
            None,
        )
        iosb = _IO_STATUS_BLOCK()
        handle = wt.HANDLE()
        status = _ntdll.NtCreateFile(
            ctypes.byref(handle),
            access | _SYNCHRONIZE,
            ctypes.byref(attrs),
            ctypes.byref(iosb),
            None,
            attributes,
            _FILE_SHARE_ALL,
            disposition,
            options | _FILE_SYNCHRONOUS_IO_NONALERT,
            None,
            0,
        )
        if status < 0:
            raise _nt_error(status, name)
        assert handle.value is not None
        return handle.value

    def _refuse_link(handle: int, name: str) -> None:
        """Close ``handle`` and raise ``ELOOP`` if it is a symlink or mount point —
        the answer ``O_NOFOLLOW`` gives for the same entry."""
        try:
            attributes, tag = _attribute_tag(handle, name)
        except OSError:
            _kernel32.CloseHandle(handle)
            raise
        if _is_link(attributes, tag):
            _kernel32.CloseHandle(handle)
            raise OSError(errno.ELOOP, "Too many levels of symbolic links", name)

    def open_directory(path: Path, *, follow: bool = True) -> int:
        """A descriptor on the directory at ``path`` — the anchor every other
        call here is relative to. ``follow=False`` refuses a symlink or junction
        AT ``path`` with ``ELOOP``; the default follows it, as the POSIX walk's
        root open does (the operator chose where the project lives)."""
        flags = _FILE_FLAG_BACKUP_SEMANTICS | (0 if follow else _FILE_FLAG_OPEN_REPARSE_POINT)
        handle = _kernel32.CreateFileW(
            str(path),
            _FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _FILE_TRAVERSE,
            _FILE_SHARE_ALL,
            None,
            _OPEN_EXISTING,
            flags,
            None,
        )
        if handle == _INVALID_HANDLE_VALUE or handle is None:
            raise _win_error(str(path))
        try:
            attributes, tag = _attribute_tag(handle, str(path))
        except OSError:
            _kernel32.CloseHandle(handle)
            raise
        if not follow and _is_link(attributes, tag):
            _kernel32.CloseHandle(handle)
            raise OSError(errno.ELOOP, "Too many levels of symbolic links", str(path))
        if not attributes & _FILE_ATTRIBUTE_DIRECTORY:
            _kernel32.CloseHandle(handle)
            raise NotADirectoryError(errno.ENOTDIR, "Not a directory", str(path))
        return _wrap(handle, writable=False)

    def open_at(dir_fd: int, name: str, flags: int, mode: int = 0o600) -> int:
        """``os.open(name, flags, mode, dir_fd=dir_fd)`` for one component.

        ``flags`` is the POSIX vocabulary: ``O_RDONLY``/``O_WRONLY``/``O_RDWR``,
        ``O_CREAT``, ``O_EXCL``, plus :data:`AT_NOFOLLOW`, :data:`AT_DIRECTORY`
        and :data:`AT_NONBLOCK` (accepted and ignored — nothing in an NTFS
        namespace blocks an open the way a reader-less FIFO does). ``O_TRUNC``
        and ``O_APPEND`` are refused: no anchored caller uses them, and a silent
        approximation is worse than a loud gap. ``mode`` has no Windows meaning
        and is accepted for signature parity.

        A read-only open of a directory succeeds, as it does on POSIX, so a
        caller's ``S_ISREG`` check sees the directory; a write open refuses one
        with ``IsADirectoryError``, the ``EISDIR`` ``open(2)`` gives."""
        del mode
        _single_component(name)
        accmode = flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR)
        if flags & (os.O_TRUNC | os.O_APPEND):
            raise ValueError("O_TRUNC/O_APPEND are not supported relative to a handle")
        writable = accmode in (os.O_WRONLY, os.O_RDWR)
        # FILE_READ_ATTRIBUTES on every open, as CreateFileW grants implicitly:
        # `_refuse_link`'s GetFileInformationByHandleEx needs it, and a write-only
        # open (FILE_GENERIC_WRITE carries only WRITE_ATTRIBUTES) is otherwise
        # answered ERROR_ACCESS_DENIED at the attribute read, not at the open.
        access = _FILE_READ_ATTRIBUTES
        if accmode in (os.O_RDONLY, os.O_RDWR):
            access |= _FILE_GENERIC_READ
        if writable:
            access |= _FILE_GENERIC_WRITE
        if flags & os.O_CREAT:
            disposition = _FILE_CREATE if flags & os.O_EXCL else _FILE_OPEN_IF
        else:
            disposition = _FILE_OPEN
        options = 0
        if flags & AT_DIRECTORY:
            options |= _FILE_DIRECTORY_FILE
            access |= _FILE_TRAVERSE
        elif writable:
            options |= _FILE_NON_DIRECTORY_FILE
        if flags & AT_NOFOLLOW:
            options |= _FILE_OPEN_REPARSE_POINT
        handle = _nt_open(
            _handle_of(dir_fd), name, access=access, disposition=disposition, options=options
        )
        if flags & AT_NOFOLLOW:
            _refuse_link(handle, name)
        return _wrap(handle, writable=writable)

    def stat_at(dir_fd: int, name: str) -> os.stat_result:
        """``os.stat(name, dir_fd=dir_fd, follow_symlinks=False)``: the entry's
        own metadata through a handle, never a path. A symlink or mount point
        reports ``S_IFLNK`` so ``S_ISREG``/``S_ISDIR`` answer False for it, as
        ``lstat`` answers on POSIX; CPython's ``fstat`` would otherwise describe
        the opened reparse point as the file or directory it stands in for."""
        _single_component(name)
        handle = _nt_open(
            _handle_of(dir_fd),
            name,
            access=_FILE_READ_ATTRIBUTES,
            disposition=_FILE_OPEN,
            options=_FILE_OPEN_REPARSE_POINT,
        )
        fd = _wrap(handle, writable=False)
        try:
            observed = os.fstat(fd)
            attributes, tag = _attribute_tag(handle, name)
        finally:
            os.close(fd)
        if not _is_link(attributes, tag):
            return observed
        link_mode = stat.S_IFLNK | stat.S_IMODE(observed.st_mode)
        return os.stat_result(
            (
                link_mode,
                observed.st_ino,
                observed.st_dev,
                observed.st_nlink,
                observed.st_uid,
                observed.st_gid,
                observed.st_size,
                int(observed.st_atime),
                int(observed.st_mtime),
                int(observed.st_ctime),
            ),
            {
                "st_atime": observed.st_atime,
                "st_mtime": observed.st_mtime,
                "st_ctime": observed.st_ctime,
                "st_atime_ns": observed.st_atime_ns,
                "st_mtime_ns": observed.st_mtime_ns,
                "st_ctime_ns": observed.st_ctime_ns,
                "st_file_attributes": attributes,
                "st_reparse_tag": tag,
            },
        )

    def _rename_information(flags: int, root_handle: int, name: str) -> bytes:
        # FILE_RENAME_INFORMATION: union{BOOLEAN ReplaceIfExists; ULONG Flags};
        # <pad to 8>; HANDLE RootDirectory; ULONG FileNameLength; WCHAR FileName[].
        # A ULONG 1 in the union reads as ReplaceIfExists=TRUE for the classic
        # class and as REPLACE_IF_EXISTS for the Ex one, so one buffer serves both.
        encoded = name.encode("utf-16-le")
        return struct.pack("<I4xQI", flags, root_handle, len(encoded)) + encoded + b"\0\0"

    def _set_information(handle: int, info_class: int, payload: bytes, name: str) -> int:
        buf = ctypes.create_string_buffer(payload, len(payload))
        iosb = _IO_STATUS_BLOCK()
        return int(
            _ntdll.NtSetInformationFile(handle, ctypes.byref(iosb), buf, len(payload), info_class)
        )

    def replace_at(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
        """``os.replace(src, dst, src_dir_fd=..., dst_dir_fd=...)``: rename the
        entry ``src`` names under ``src_dir_fd`` onto ``dst`` under ``dst_dir_fd``,
        replacing an existing ``dst``. The source entry is renamed AS the entry
        (a link at ``src`` moves as a link, never its target), and the rename is
        performed on that entry's own handle — nothing resolves a path.

        ``FileRenameInformationEx`` with POSIX semantics first (Windows 10 1709+,
        NTFS/ReFS): the old ``dst`` file is unlinked from the namespace even while
        another handle keeps it open, which is what ``rename(2)`` does. Where the
        kernel or volume refuses the class, the classic ``FileRenameInformation``
        with ``ReplaceIfExists`` is used, whose replace fails with a sharing
        violation while ``dst`` is open elsewhere — the retry the path-based
        writer already makes applies to both."""
        _single_component(src)
        _single_component(dst)
        handle = _nt_open(
            _handle_of(src_dir_fd),
            src,
            access=_DELETE,
            disposition=_FILE_OPEN,
            options=_FILE_OPEN_REPARSE_POINT,
        )
        try:
            root = _handle_of(dst_dir_fd)
            status = _set_information(
                handle,
                _FileRenameInformationEx,
                _rename_information(
                    _FILE_RENAME_REPLACE_IF_EXISTS | _FILE_RENAME_POSIX_SEMANTICS, root, dst
                ),
                dst,
            )
            if status & 0xFFFFFFFF in _EX_CLASS_UNSUPPORTED:
                status = _set_information(
                    handle,
                    _FileRenameInformation,
                    _rename_information(_FILE_RENAME_REPLACE_IF_EXISTS, root, dst),
                    dst,
                )
            if status < 0:
                raise _nt_error(status, dst)
        finally:
            _kernel32.CloseHandle(handle)

    def unlink_at(dir_fd: int, name: str) -> None:
        """``os.unlink(name, dir_fd=dir_fd)``: delete the entry itself (a link is
        removed, not followed) through its own handle."""
        _single_component(name)
        handle = _nt_open(
            _handle_of(dir_fd),
            name,
            access=_DELETE,
            disposition=_FILE_OPEN,
            options=_FILE_OPEN_REPARSE_POINT | _FILE_NON_DIRECTORY_FILE,
        )
        try:
            status = _set_information(
                handle,
                _FileDispositionInformationEx,
                struct.pack("<I", _FILE_DISPOSITION_DELETE | _FILE_DISPOSITION_POSIX_SEMANTICS),
                name,
            )
            if status & 0xFFFFFFFF in _EX_CLASS_UNSUPPORTED:
                status = _set_information(
                    handle, _FileDispositionInformation, struct.pack("<B", 1), name
                )
            if status < 0:
                raise _nt_error(status, name)
        finally:
            _kernel32.CloseHandle(handle)

else:

    def open_directory(path: Path, *, follow: bool = True) -> int:
        del path, follow
        raise _unsupported()

    def open_at(dir_fd: int, name: str, flags: int, mode: int = 0o600) -> int:
        del dir_fd, name, flags, mode
        raise _unsupported()

    def stat_at(dir_fd: int, name: str) -> os.stat_result:
        del dir_fd, name
        raise _unsupported()

    def replace_at(src_dir_fd: int, src: str, dst_dir_fd: int, dst: str) -> None:
        del src_dir_fd, src, dst_dir_fd, dst
        raise _unsupported()

    def unlink_at(dir_fd: int, name: str) -> None:
        del dir_fd, name
        raise _unsupported()
