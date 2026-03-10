"""Tests verifying CLI commands load .env before load_workflow."""

import subprocess
import sys
from pathlib import Path


def test_cleanup_loads_dotenv(tmp_path):
    """cli.py should import and call load_dotenv in main() before dispatching."""
    import host.cli as cli_mod
    source = Path(cli_mod.__file__).read_text()
    assert "from host.env import load_all_dotenv" in source, \
        "cli.py should import load_all_dotenv"
    # load_all_dotenv must be called inside main(), before a.func(a) dispatches
    main_start = source.index("def main():")
    main_body = source[main_start:]
    assert "load_all_dotenv" in main_body, \
        "main() should call load_all_dotenv"
    assert main_body.index("load_all_dotenv") < main_body.index("a.func(a)"), \
        "load_all_dotenv should be called before command dispatch in main()"
