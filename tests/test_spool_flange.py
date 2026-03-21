"""
Test: Part 1 - Spool Flange via FreeCAD MCP server.

Starts the server as a subprocess (Streamable HTTP on a fixed port), passes
part1_spool_flange.py via script_path, and renders 4 views.

Run from the repo root:
  ./venv/bin/python tests/test_spool_flange.py
"""

import asyncio
import base64
import os
import socket
import subprocess
import time
from pathlib import Path
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

REPO_ROOT   = Path(__file__).parent.parent.resolve()
TESTS_DIR   = Path(__file__).parent
OUTPUT_DIR  = TESTS_DIR / "output"
SCRIPT_PATH = str(TESTS_DIR / "part1_spool_flange.py")
OUTPUT_DIR.mkdir(exist_ok=True)

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 18002
SERVER_URL  = f"http://{SERVER_HOST}:{SERVER_PORT}/mcp"

# Views to render
RENDERS = [
    {"label": "isometric", "view_angle": "Isometric", "zoom": 1.0, "width": 900, "height": 700},
    {"label": "top",       "view_angle": "Top",        "zoom": 1.0, "width": 900, "height": 700},
    {"label": "front",     "view_angle": "Front",      "zoom": 1.0, "width": 900, "height": 500},
    {"label": "custom_35", "elevation": 35, "azimuth": 120, "zoom": 1.2, "width": 900, "height": 700},
]


def _wait_for_server(host: str, port: int, retries: int = 20, delay: float = 0.5) -> bool:
    """Wait until the server's TCP port accepts connections."""
    for _ in range(retries):
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(delay)
    return False


async def run():
    env = os.environ.copy()
    env["DYLD_LIBRARY_PATH"] = "/Applications/FreeCAD.app/Contents/Resources/lib"

    server_proc = subprocess.Popen(
        [
            str(REPO_ROOT / "venv" / "bin" / "python"),
            str(REPO_ROOT / "server.py"),
            "--host", SERVER_HOST,
            "--port", str(SERVER_PORT),
        ],
        env=env,
        stderr=subprocess.PIPE,
    )

    try:
        print(f"Waiting for server on {SERVER_URL} ...")
        if not _wait_for_server(SERVER_HOST, SERVER_PORT):
            print("ERROR: server did not start in time.")
            return

        async with streamablehttp_client(SERVER_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                print("Connected. Tools:", [t.name for t in tools.tools])
                print(f"Script: {SCRIPT_PATH}")
                print(f"\nRendering spool flange - {len(RENDERS)} view(s) ...\n")

                for view in RENDERS:
                    label = view.pop("label")
                    print(f"-- {label} --")

                    result = await session.call_tool(
                        "execute_freecad_script",
                        arguments={"script_path": SCRIPT_PATH, **view},
                    )

                    for content in result.content:
                        if content.type == "text":
                            print("\n".join(content.text.splitlines()[:10]))
                        elif content.type == "image":
                            out = OUTPUT_DIR / f"spool_flange_{label}.png"
                            out.write_bytes(base64.b64decode(content.data))
                            print(f"Saved -> {out}\n")
    finally:
        server_proc.terminate()
        server_proc.wait()


if __name__ == "__main__":
    asyncio.run(run())
