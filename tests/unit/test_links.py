"""The helper that makes a symbolic link for a test, or skips the test on an account that may not."""

import os
from pathlib import Path

import pytest

from support.links import symlink_or_skip


def refuse_with(error: BaseException, monkeypatch) -> list[tuple[Path, object, bool]]:
    """Path.symlink_to raises error from now on. Returns the calls that it got."""
    calls: list[tuple[Path, object, bool]] = []

    def symlink_to(self, target, target_is_directory=False):
        calls.append((self, target, target_is_directory))
        raise error

    monkeypatch.setattr(Path, "symlink_to", symlink_to)
    return calls


def privilege_not_held() -> OSError:
    """What Windows raises for an account without the right to create symbolic links. On Windows
    the fourth argument of OSError is winerror; on other systems the attribute is set by hand."""
    error = OSError(22, "A required privilege is not held by the client", "link", 1314)
    error.winerror = 1314
    return error


def test_an_account_that_may_not_create_a_symbolic_link_skips_the_test_with_the_reason(tmp_path, monkeypatch):
    calls = refuse_with(privilege_not_held(), monkeypatch)
    with pytest.raises(pytest.skip.Exception) as skipped:
        symlink_or_skip(tmp_path / "link", tmp_path / "target", target_is_directory=True)
    reason = str(skipped.value)
    assert "may not create a symbolic link" in reason and "WinError 1314" in reason
    assert "A required privilege is not held by the client" in reason
    assert calls == [(tmp_path / "link", tmp_path / "target", True)]


@pytest.mark.parametrize(
    "error",
    [
        FileExistsError(17, "File exists"),
        FileNotFoundError(2, "No such file or directory"),
        PermissionError(13, "Permission denied"),
        OSError(22, "Invalid argument"),
    ],
    ids=lambda error: type(error).__name__,
)
def test_any_other_error_of_the_link_is_raised_so_a_wrong_set_up_never_skips(tmp_path, monkeypatch, error):
    refuse_with(error, monkeypatch)
    with pytest.raises(type(error)) as raised:
        symlink_or_skip(tmp_path / "link", tmp_path / "target")
    assert raised.value is error


def test_another_windows_error_is_raised(tmp_path, monkeypatch):
    error = OSError(13, "Access is denied", "link", 5)
    error.winerror = 5
    refuse_with(error, monkeypatch)
    with pytest.raises(OSError, match="Access is denied"):
        symlink_or_skip(tmp_path / "link", tmp_path / "target")


def test_a_platform_without_symbolic_links_skips_the_test(tmp_path, monkeypatch):
    refuse_with(NotImplementedError("symlink() is not available"), monkeypatch)
    with pytest.raises(pytest.skip.Exception, match="no symbolic links"):
        symlink_or_skip(tmp_path / "link", tmp_path / "target")


def link_can_be_made(folder: Path) -> bool:
    try:
        os.symlink(folder / "probe-target", folder / "probe")
    except (OSError, NotImplementedError):
        return False
    return True


def test_the_link_is_made_where_the_account_may_make_one(tmp_path):
    if not link_can_be_made(tmp_path):
        pytest.skip("this account may not create a symbolic link: the helper has no link to make")
    (tmp_path / "target.txt").write_bytes(b"x")
    (tmp_path / "folder").mkdir()
    symlink_or_skip(tmp_path / "link.txt", tmp_path / "target.txt")
    symlink_or_skip(tmp_path / "relative", "folder", target_is_directory=True)  # a str, as os.symlink takes
    assert (tmp_path / "link.txt").is_symlink() and (tmp_path / "link.txt").read_bytes() == b"x"
    assert (tmp_path / "relative").is_symlink() and (tmp_path / "relative").is_dir()
    assert os.readlink(tmp_path / "relative") == "folder"
