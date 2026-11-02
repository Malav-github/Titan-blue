import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from tools import browser, sandbox, web
from tools.archive import check_public_url

TEST_URL = "https://example.com/"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:11434/api/tags",
    "http://localhost/",
    "http://169.254.169.254/latest/meta-data/",
    "file:///C:/Windows/win.ini",
])
def test_rejects_non_public_urls(url):
    with pytest.raises(ValueError):
        check_public_url(url)


@pytest.mark.network
def test_fetch_archives_exact_bytes():
    result = web.fetch_url(TEST_URL)
    assert result["status"] == 200
    assert "documentation examples" in result["text"]
    archived = Path(result["archive_path"]).read_bytes()
    assert hashlib.sha256(archived).hexdigest() == result["sha256"]


@pytest.mark.network
def test_browser_archives_html_and_screenshot():
    result = browser.render_url(TEST_URL)
    assert "documentation examples" in result["text"]
    shot = Path(result["screenshot_path"]).read_bytes()
    assert shot.startswith(b"\x89PNG")
    assert hashlib.sha256(shot).hexdigest() == result["screenshot_sha256"]


@pytest.mark.docker
def test_sandbox_runs_python():
    result = sandbox.run_python("print(sum(range(10)))")
    assert result["exit_code"] == 0, result["stderr"]
    assert result["stdout"].strip() == "45"


@pytest.mark.docker
def test_sandbox_python_reads_stdin_data():
    data = "it's \"quoted\"\nand multi-line"
    result = sandbox.run_python("import sys; print(len(sys.stdin.read().split()))", stdin=data)
    assert result["stdout"].strip() == "4", result["stderr"]


@pytest.mark.docker
def test_sandbox_shell_keeps_unix_newlines():
    result = sandbox.run_shell("a=1\nb=2\necho $((a+b))\n")
    assert result["stdout"].strip() == "3", result["stderr"]


@pytest.mark.docker
def test_sandbox_has_no_network():
    code = "import urllib.request; urllib.request.urlopen('https://example.com', timeout=5)"
    assert sandbox.run_python(code)["exit_code"] != 0


@pytest.mark.docker
def test_sandbox_cannot_see_project_or_write_outside_tmp():
    code = (
        "import os\n"
        "print(os.path.exists('/mnt/c'))\n"
        "try:\n    open('/etc/pwned', 'w')\n    print('wrote')\n"
        "except OSError:\n    print('blocked')\n"
    )
    result = sandbox.run_python(code)
    assert result["stdout"].split() == ["False", "blocked"], result


@pytest.mark.docker
def test_sandbox_timeout_kills_container():
    result = sandbox.run_python("while True: pass", timeout=5)
    assert result["timed_out"]


@pytest.mark.docker
def test_mcp_server_round_trip():
    from tools.client import titan_tools

    async def go():
        async with titan_tools() as tools:
            by_name = {t.name: t for t in tools}
            assert set(by_name) == {"web_search", "resolve_company_domain", "discover_company_pages",
                              "fetch_url", "render_url", "record_claim", "list_claims", "list_connectors", "call_api",
                              "run_python", "run_shell"}
            return await by_name["run_python"].ainvoke({"code": "print(6 * 7)"})

    output = asyncio.run(go())
    text = output if isinstance(output, str) else json.dumps(output)
    assert "42" in text
