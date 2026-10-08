"""Symbolic links in tests. Some Windows accounts may not create one."""

from __future__ import annotations

from pathlib import Path

import pytest

# ERROR_PRIVILEGE_NOT_HELD: the account has no SeCreateSymbolicLinkPrivilege and Developer Mode is off
_PRIVILEGE_NOT_HELD = 1314


def symlink_or_skip(link: Path, target: Path | str, *, target_is_directory: bool = False) -> None:
    """Make `link` a symbolic link to `target`. Skip the test when this account may not make one.

    The rule under test still holds on such a machine; the machine cannot build the input of the
    test. Every other error is raised: a test must not skip because its own set-up is wrong.
    """
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except NotImplementedError as error:
        pytest.skip(f"this platform has no symbolic links: {error}")
    except OSError as error:
        if getattr(error, "winerror", None) != _PRIVILEGE_NOT_HELD:
            raise
        pytest.skip(f"this account may not create a symbolic link (WinError 1314): {error}")
