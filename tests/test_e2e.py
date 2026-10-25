"""Browser end-to-end test: drive the real dashboard in Chromium.

Skipped automatically if Node + Playwright's Chromium are not available, so the
pure-Python suite still runs anywhere. Captures screenshots of every page under
tests/screenshots/ as a side effect.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PW = "/opt/node-tools/node_modules/playwright"
NODE = shutil.which("node")

pytestmark = pytest.mark.e2e


def _have_browser() -> bool:
    return bool(NODE) and os.path.isdir(PW) and os.path.isdir("/opt/pw-browsers")


@pytest.fixture(scope="module")
def server():
    import uvicorn

    from warden.server.app import create_app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="error")
    srv = uvicorn.Server(config)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        try:
            import urllib.request

            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    th.join(timeout=5)


@pytest.mark.skipif(not _have_browser(), reason="node/playwright chromium not available")
def test_dashboard_e2e(server):
    shots = os.path.join(ROOT, "tests", "screenshots")
    os.makedirs(shots, exist_ok=True)
    script = r"""
const { chromium } = require(PW_PATH);
(async () => {
  const base = process.env.BASE, shots = process.env.SHOTS;
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1400, height: 950 } });
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.goto(base, { waitUntil: 'networkidle' });
  await page.evaluate(async () => { await fetch('/v1/scenarios/run-all', {method:'POST'}); });
  await page.waitForTimeout(700);
  const pages = ['overview','feed','agents','approvals','incidents','policy','redteam','sandbox','trust','compliance','connect'];
  const seen = {};
  for (const p of pages) {
    await page.goto(base + '#' + p, { waitUntil: 'networkidle' });
    await page.waitForTimeout(400);
    const h1 = await page.$eval('h1', e => e.textContent).catch(() => null);
    seen[p] = h1;
    await page.screenshot({ path: shots + '/' + p + '.png', fullPage: true });
  }
  console.log(JSON.stringify({ errors, seen }));
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
"""
    script = f"const PW_PATH = {json.dumps(PW)};\n" + script
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False, dir=shots) as fh:
        fh.write(script)
        path = fh.name
    try:
        env = {**os.environ, "BASE": server, "SHOTS": shots}
        out = subprocess.run([NODE, path], capture_output=True, text=True, timeout=120, env=env)
        assert out.returncode == 0, out.stderr
        result = json.loads(out.stdout.strip().splitlines()[-1])
        assert result["errors"] == [], f"console errors: {result['errors']}"
        for p, h1 in result["seen"].items():
            assert h1, f"page {p} rendered no <h1>"
        # verify a key screenshot exists and is non-trivial
        assert os.path.getsize(os.path.join(shots, "redteam.png")) > 5000
    finally:
        os.unlink(path)
