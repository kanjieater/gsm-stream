"""
SysDVR RTSP → meikiocr → Google Lens (owocr) → GSM texthooker.

Entry point. Wires env config, GSM web server, and the stream loop together.
See CLAUDE.md for architecture overview and module descriptions.
"""
import asyncio
import os
import sys
import threading

os.environ.setdefault("GSM_ELECTRON", "1")

SWITCH_HOST   = os.environ.get("SWITCH_HOST", "")
SWITCH_STREAM = os.environ.get("SWITCH_STREAM", f"rtsp://{SWITCH_HOST}:6666" if SWITCH_HOST else "")

import controller
from stream import bridge_loop, mjpeg_server

# --- register bridge routes on GSM's Flask app before start_web_server() runs ---
from GameSentenceMiner.web.texthooking_page import app as _flask_app
from flask import request as _freq, jsonify as _fjson, Response as _fResp
import identity

_SCRIPT_TAG = b'<script src="/bridge-sync.js"></script>'

_BRIDGE_JS = r"""
(function() {
  // Force showPresetQuickSwitch on in localStorage so it survives page reloads
  localStorage.setItem('bannou-texthooker-showPresetQuickSwitch', '1');

  // Svelte updates the DOM before writing to localStorage, so intercept at the
  // DOM level: inject CSS that wins over sm:hidden on the preset select
  const style = document.createElement('style');
  style.textContent = '@media (min-width:640px){select.w-48{display:block!important}}';
  document.head.appendChild(style);

  function notifyGame(name) {
    if (!name) return;
    fetch('/set-game', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:name})});
  }
  notifyGame(localStorage.getItem('bannou-texthooker-lastSettingPreset'));
  const _orig = localStorage.setItem.bind(localStorage);
  localStorage.setItem = function(key, value) {
    _orig(key, value);
    if (key === 'bannou-texthooker-lastSettingPreset') notifyGame(value);
  };
})();
"""


@_flask_app.after_request
def _inject_bridge_script(response):
    if 'text/html' in response.content_type:
        response.direct_passthrough = False  # force buffering of send_from_directory responses
        data = response.get_data()
        if b'</body>' in data:
            response.set_data(data.replace(b'</body>', _SCRIPT_TAG + b'</body>', 1))
    return response


@_flask_app.route("/set-game", methods=["POST"])
def _set_game():
    name = (_freq.json or {}).get("name", "").strip()
    identity.set_runtime_game(name)
    print(f"game identity set: {name!r}", flush=True)
    return _fjson({"ok": True, "game": name or "(cleared)"})


@_flask_app.route("/bridge-sync.js")
def _bridge_sync_js():
    return _fResp(_BRIDGE_JS, mimetype="application/javascript")


def start_gsm_web_server():
    from GameSentenceMiner.util.config.configuration import get_config
    from GameSentenceMiner.web.texthooking_page import start_web_server
    get_config().advanced.localhost_bind_address = "0.0.0.0"
    start_web_server()


async def main():
    controller.bridge_loop = asyncio.get_event_loop()

    import GameSentenceMiner.gametext  # noqa: F401 — must import before start_web_server

    threading.Thread(target=start_gsm_web_server, daemon=True).start()
    print("GSM web server starting on :7275", flush=True)

    if not SWITCH_STREAM:
        print("ERROR: SWITCH_STREAM env var is required", flush=True)
        sys.exit(1)

    controller.get_controller()
    await asyncio.gather(mjpeg_server(), bridge_loop(SWITCH_STREAM))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
