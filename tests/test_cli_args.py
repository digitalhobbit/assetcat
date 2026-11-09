"""Windows shells mangle a quoted path that ends in a backslash; the CLI repairs it."""

from __future__ import annotations

import pytest

from assetcat.cli import unmangle_windows_args as fix


@pytest.mark.parametrize("argv, expected", [
    # what PowerShell tab completion produces: 'D:\Games\Asset Library\' --dry-run
    (["init", 'D:\\Games\\Asset Library" --dry-run'], ["init", "D:\\Games\\Asset Library", "--dry-run"]),
    # nothing after the path
    (["init", 'D:\\Games\\Asset Library"'], ["init", "D:\\Games\\Asset Library"]),
    # a flag value, more arguments after it, one of them quoted with a space
    (["index", "--library", 'D:\\Lib" --workers 4 --profile "E:\\My Profile"'],
     ["index", "--library", "D:\\Lib", "--workers", "4", "--profile", "E:\\My Profile"]),
    # two mangled paths in one command line
    (["stats", "--library", 'D:\\A B" --profile "E:\\C D\\"'], ["stats", "--library", "D:\\A B", "--profile", "E:\\C D\\"]),
    # untouched: normal arguments, trailing backslashes without quotes
    (["show", "D:\\Lib\\pack\\", "--json"], ["show", "D:\\Lib\\pack\\", "--json"]),
    (["search", "forest", "tiles"], ["search", "forest", "tiles"]),
])
def test_unmangle(argv, expected):
    assert fix(argv) == expected


def test_unbalanced_quote_does_not_crash():
    assert fix(['D:\\A" --profile "E:\\B']) == ["D:\\A", "--profile", "E:\\B"]
