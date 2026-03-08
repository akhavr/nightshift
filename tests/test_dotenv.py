"""Tests for host.env.load_dotenv."""

import os
import pytest
from pathlib import Path

from host.env import load_dotenv


@pytest.fixture(autouse=True)
def clean_env():
    """Remove test env vars before/after each test."""
    keys = ["TEST_VAR", "TEST_QUOTED", "TEST_EXPORT", "TEST_EXISTING"]
    saved = {k: os.environ.pop(k, None) for k in keys}
    yield
    for k in keys:
        os.environ.pop(k, None)
        if saved[k] is not None:
            os.environ[k] = saved[k]


def test_basic_key_value(tmp_path):
    (tmp_path / ".env").write_text("TEST_VAR=hello\n")
    load_dotenv(tmp_path / ".env")
    assert os.environ["TEST_VAR"] == "hello"


def test_double_quoted_value(tmp_path):
    (tmp_path / ".env").write_text('TEST_QUOTED="hello world"\n')
    load_dotenv(tmp_path / ".env")
    assert os.environ["TEST_QUOTED"] == "hello world"


def test_single_quoted_value(tmp_path):
    (tmp_path / ".env").write_text("TEST_QUOTED='hello world'\n")
    load_dotenv(tmp_path / ".env")
    assert os.environ["TEST_QUOTED"] == "hello world"


def test_export_prefix(tmp_path):
    (tmp_path / ".env").write_text("export TEST_EXPORT=value\n")
    load_dotenv(tmp_path / ".env")
    assert os.environ["TEST_EXPORT"] == "value"


def test_skips_comments_and_blanks(tmp_path):
    (tmp_path / ".env").write_text("# comment\n\nTEST_VAR=yes\n")
    load_dotenv(tmp_path / ".env")
    assert os.environ["TEST_VAR"] == "yes"


def test_does_not_override_existing(tmp_path):
    os.environ["TEST_EXISTING"] = "original"
    (tmp_path / ".env").write_text("TEST_EXISTING=overwritten\n")
    load_dotenv(tmp_path / ".env")
    assert os.environ["TEST_EXISTING"] == "original"


def test_missing_file_is_noop(tmp_path):
    load_dotenv(tmp_path / "nonexistent")
    # Should not raise


def test_no_equals_line_skipped(tmp_path):
    (tmp_path / ".env").write_text("INVALID_LINE\nTEST_VAR=ok\n")
    load_dotenv(tmp_path / ".env")
    assert os.environ["TEST_VAR"] == "ok"
    assert "INVALID_LINE" not in os.environ
