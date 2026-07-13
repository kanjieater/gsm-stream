"""
SysDVR RTSP → meikiocr → Google Lens (owocr) → GSM texthooker.

Entry point. Wires env config, GSM web server, and the stream loop together.
See CLAUDE.md for architecture overview and module descriptions.
"""
import asyncio
import os
import sys
import threading
import yaml

os.environ.setdefault("GSM_ELECTRON", "1")

SWITCH_HOST   = os.environ.get("SWITCH_HOST", "")
SWITCH_STREAM = os.environ.get("SWITCH_STREAM", f"rtsp://{SWITCH_HOST}:6666" if SWITCH_HOST else "")
_PROFILES_PATH = os.environ.get("PROFILES_CONFIG", "/profiles.yml")

import controller
from stream import bridge_loop, mjpeg_server

# --- register bridge routes on GSM's Flask app before start_web_server() runs ---
from GameSentenceMiner.web.texthooking_page import app as _flask_app
from flask import request as _freq, jsonify as _fjson, Response as _fResp
import identity

_SCRIPT_TAG = b'<script src="/bridge-sync.js"></script>'

# __PROFILES__ and __UI_DEFAULTS__ are replaced at serve time with JSON literals.
# filterNonCJKLines is the one preset settings key without a $ suffix in the Svelte bundle.
_BRIDGE_JS_TEMPLATE = r"""
(function() {
  var PROFILES = __PROFILES__;
  var UI_DEFAULTS = __UI_DEFAULTS__;
  var NO_DOLLAR = {filterNonCJKLines: true};

  function presetKey(k) { return NO_DOLLAR[k] ? k : k + '$'; }

  function buildSettings(name) {
    var s = {};
    for (var k in UI_DEFAULTS) s[presetKey(k)] = UI_DEFAULTS[k];
    s['windowTitle$'] = name;
    return s;
  }

  // When ui_defaults change in profiles.yml, wipe stale localStorage so new
  // defaults take effect on any device without needing DevTools.
  var DEFAULTS_VER = __DEFAULTS_VER__;
  try {
    if (localStorage.getItem('bannou-texthooker-__bridge_ver__') !== DEFAULTS_VER) {
      var keys = Object.keys(localStorage).filter(function(k) { return k.startsWith('bannou-texthooker-'); });
      for (var i = 0; i < keys.length; i++) localStorage.removeItem(keys[i]);
      localStorage.setItem('bannou-texthooker-__bridge_ver__', DEFAULTS_VER);
    }
  } catch(e) {}

  // Inject missing preset entries before Svelte initialises its stores.
  // This script runs as a regular <script> and therefore executes before the
  // deferred <script type="module"> Svelte bundle, so localStorage is set
  // before any Svelte store reads it.
  try {
    var raw = localStorage.getItem('bannou-texthooker-settingPresets');
    var presets = [];
    try { presets = JSON.parse(raw) || []; } catch(e) {}
    if (!Array.isArray(presets)) presets = [];
    var changed = false;
    for (var i = 0; i < PROFILES.length; i++) {
      var name = PROFILES[i];
      var found = false;
      for (var j = 0; j < presets.length; j++) { if (presets[j].name === name) { found = true; break; } }
      if (!found) { presets.push({name: name, settings: buildSettings(name)}); changed = true; }
    }
    if (changed) localStorage.setItem('bannou-texthooker-settingPresets', JSON.stringify(presets));

    // Seed global localStorage keys from ui_defaults only when not yet set.
    for (var k in UI_DEFAULTS) {
      var lk = 'bannou-texthooker-' + k;
      if (localStorage.getItem(lk) === null) {
        var v = UI_DEFAULTS[k];
        localStorage.setItem(lk, typeof v === 'string' ? v : JSON.stringify(v));
      }
    }
  } catch(e) { console.warn('[bridge] preset init:', e); }

  // Always force the quick-switch dropdown visible.
  localStorage.setItem('bannou-texthooker-showPresetQuickSwitch', '1');

  // Svelte updates the DOM before writing to localStorage, so intercept at the
  // DOM level: inject CSS that wins over sm:hidden on the preset select.
  var style = document.createElement('style');
  style.textContent = '@media (min-width:640px){select.w-48{display:block!important}}';
  document.head.appendChild(style);

  // Stream preview panel — toggle with the camera button, polls /frame at ~2fps
  (function() {
    var visible = false;
    var timer = null;
    var panel = document.createElement('div');
    panel.style.cssText = 'position:fixed;bottom:60px;right:12px;z-index:9999;display:none;background:#000;border:1px solid #444;border-radius:6px;overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,.6)';
    var img = document.createElement('img');
    img.style.cssText = 'display:block;width:320px;height:auto';
    panel.appendChild(img);
    document.body.appendChild(panel);

    function poll() {
      if (!visible) return;
      img.src = '/frame?' + Date.now();
      timer = setTimeout(poll, 500);
    }

    var btn = document.createElement('button');
    btn.title = 'Toggle stream preview';
    btn.textContent = '📷';
    btn.style.cssText = 'position:fixed;bottom:16px;right:12px;z-index:9999;font-size:20px;background:rgba(0,0,0,.5);border:1px solid #555;border-radius:6px;padding:4px 8px;cursor:pointer;color:#fff';
    btn.onclick = function() {
      visible = !visible;
      panel.style.display = visible ? 'block' : 'none';
      if (visible) poll();
      else { clearTimeout(timer); timer = null; }
    };
    document.body.appendChild(btn);
  })();

  function ensurePreset(name) {
    if (!name) return;
    try {
      var raw = localStorage.getItem('bannou-texthooker-settingPresets');
      var presets = [];
      try { presets = JSON.parse(raw) || []; } catch(e) {}
      if (!Array.isArray(presets)) presets = [];
      var found = false;
      for (var j = 0; j < presets.length; j++) { if (presets[j].name === name) { found = true; break; } }
      if (!found) {
        presets.push({name: name, settings: buildSettings(name)});
        localStorage.setItem('bannou-texthooker-settingPresets', JSON.stringify(presets));
        // Also stamp individual keys so they take effect on next load.
        for (var k in UI_DEFAULTS) {
          localStorage.setItem('bannou-texthooker-' + k, typeof UI_DEFAULTS[k] === 'string' ? UI_DEFAULTS[k] : JSON.stringify(UI_DEFAULTS[k]));
        }
        localStorage.setItem('bannou-texthooker-windowTitle', name);
      }
    } catch(e) {}
  }

  function notifyGame(name) {
    if (!name) return;
    ensurePreset(name);
    fetch('/set-game', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:name})});
  }
  notifyGame(localStorage.getItem('bannou-texthooker-lastSettingPreset'));
  var _orig = localStorage.setItem.bind(localStorage);
  localStorage.setItem = function(key, value) {
    _orig(key, value);
    if (key === 'bannou-texthooker-lastSettingPreset') notifyGame(value);
  };
})();
"""

# ---------------------------------------------------------------------------
# profiles.yml helpers — source of truth for the game profile list + defaults
# ---------------------------------------------------------------------------

def _load_yml():
    """Load the full profiles.yml document, preserving all top-level keys."""
    try:
        with open(_PROFILES_PATH) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _save_yml(data):
    with open(_PROFILES_PATH, "w") as f:
        yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False)


def _load_profiles_yml():
    p = _load_yml().get("profiles", [])
    return list(p) if isinstance(p, list) else []


def _load_anki_yml() -> dict:
    """Return the top-level 'anki' section from profiles.yml, or {}."""
    return _load_yml().get("anki") or {}


def _apply_anki_yml_to_profile(profile_dict: dict, anki_yml: dict) -> bool:
    """Merge profiles.yml anki section into a profile dict. Returns True if anything changed."""
    if not anki_yml:
        return False
    anki = profile_dict.setdefault("anki", {})
    changed = False

    for flat_key in ("url", "note_type", "show_update_confirmation_dialog_v2"):
        if flat_key in anki_yml and anki.get(flat_key) != anki_yml[flat_key]:
            anki[flat_key] = anki_yml[flat_key]
            changed = True

    fields = anki_yml.get("fields", {})
    field_options = anki_yml.get("field_options", {})
    for yml_key in ("word", "picture", "sentence", "sentence_audio"):
        if yml_key in fields:
            obj = anki.setdefault(yml_key, {})
            if obj.get("name") != fields[yml_key]:
                obj["name"] = fields[yml_key]
                changed = True
        opts = field_options.get(yml_key, {})
        if opts:
            obj = anki.setdefault(yml_key, {})
            for opt_key, opt_val in opts.items():
                if obj.get(opt_key) != opt_val:
                    obj[opt_key] = opt_val
                    changed = True

    return changed



def _save_profiles_yml(profiles):
    data = _load_yml()
    data["profiles"] = sorted(profiles)
    _save_yml(data)


def _load_defaults():
    """Return the GSM server-side defaults (everything under 'defaults' except 'ui')."""
    d = _load_yml().get("defaults", {}) or {}
    return {k: v for k, v in d.items() if k != "ui"}


def _add_to_profiles_yml(name):
    profiles = _load_profiles_yml()
    if name not in profiles:
        _save_profiles_yml(profiles + [name])


def _remove_from_profiles_yml(name):
    profiles = _load_profiles_yml()
    if name in profiles:
        _save_profiles_yml([p for p in profiles if p != name])


def _apply_profile_defaults(profile_name):
    """
    Merge profiles.yml 'defaults' into a newly created profile in config.json.

    Only called when a profile was just created (ProfileSwitcher.create_profile
    returned True). Skips silently if no defaults are set.
    """
    from GameSentenceMiner.util.config.configuration import get_master_config, ProfileConfig
    defaults = _load_defaults()
    if not defaults:
        return
    master = get_master_config()
    if profile_name not in master.configs:
        return
    profile_dict = master.configs[profile_name].to_dict()
    for section, overrides in defaults.items():
        if isinstance(overrides, dict) and isinstance(profile_dict.get(section), dict):
            profile_dict[section].update(overrides)
        else:
            profile_dict[section] = overrides
    master.configs[profile_name] = ProfileConfig.from_dict(profile_dict)
    master.save()
    print(f"[bridge] applied defaults to new profile {profile_name!r}", flush=True)


def _remove_profile_from_config(name):
    from GameSentenceMiner.util.config.configuration import get_master_config
    master = get_master_config()
    if name and name != "Default" and name in master.configs:
        del master.configs[name]
        master.save()


def _sync_profiles():
    """
    Reconcile profiles.yml (source of truth) → config.json + gsm.db.

    First run (profiles.yml empty): seed from existing config.json profiles.
    Subsequent runs: add/remove from config.json and gsm.db to match profiles.yml.
    """
    from GameSentenceMiner.util.config.configuration import get_master_config
    from GameSentenceMiner.profile_switcher import ProfileSwitcher
    from GameSentenceMiner.util.database.games_table import GamesTable
    from GameSentenceMiner.util.database.db import GameLinesTable
    try:
        yml_profiles = set(_load_profiles_yml())
        master = get_master_config()
        config_profiles = set(master.get_all_profile_names()) - {"Default"}

        if not yml_profiles:
            # First run: seed profiles.yml from config.json
            _save_profiles_yml(list(config_profiles))
            yml_profiles = config_profiles

        # Add to config.json any profiles missing from it
        for name in yml_profiles - config_profiles:
            if ProfileSwitcher.create_profile(name):
                _apply_profile_defaults(name)

        # Remove from config.json (and DB) any profiles no longer in yml
        config_removed = config_profiles - yml_profiles
        for name in config_removed:
            if name in master.configs:
                del master.configs[name]
            game = GamesTable.get_by_title(name)
            if game:
                GameLinesTable._db.execute(
                    f"UPDATE {GameLinesTable._table} SET game_id = NULL WHERE game_id = ?",
                    (game.id,), commit=True,
                )
                GameLinesTable._db.execute(
                    f"DELETE FROM {GamesTable._table} WHERE id = ?",
                    (game.id,), commit=True,
                )
        if config_removed:
            master.save()

        # Ensure DB has a game record for every yml profile
        for name in yml_profiles:
            GamesTable.get_or_create_by_name(name)

        # Push anki config from profiles.yml into every profile (profiles.yml is authoritative)
        anki_yml = _load_anki_yml()
        if anki_yml:
            dirty = False
            from GameSentenceMiner.util.config.configuration import ProfileConfig
            for name in master.configs:
                pd = master.configs[name].to_dict()
                if _apply_anki_yml_to_profile(pd, anki_yml):
                    master.configs[name] = ProfileConfig.from_dict(pd)
                    dirty = True
            if dirty:
                master.save()
                print("[bridge] anki config synced from profiles.yml", flush=True)

    except Exception as e:
        print(f"[bridge] profile sync error: {e}", flush=True)


# ---------------------------------------------------------------------------
# Flask hooks
# ---------------------------------------------------------------------------

_tls = threading.local()


@_flask_app.after_request
def _inject_bridge_script(response):
    if 'text/html' in response.content_type:
        response.direct_passthrough = False  # force buffering of send_from_directory responses
        data = response.get_data()
        if b'</body>' in data:
            response.set_data(data.replace(b'</body>', _SCRIPT_TAG + b'</body>', 1))
    return response


@_flask_app.before_request
def _before_req():
    # Sync profiles.yml → config.json + DB on every games list load
    if _freq.method == "GET" and _freq.path == "/api/games-management":
        _sync_profiles()

    # Capture game title before DELETE handler removes the record
    _tls.delete_title = None
    if _freq.method == "DELETE":
        parts = _freq.path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "games"]:
            from GameSentenceMiner.util.database.games_table import GamesTable
            game = GamesTable.get(parts[2])
            if game:
                _tls.delete_title = game.title_original


@_flask_app.after_request
def _after_req(response):
    # UI add game → profiles.yml + config.json profile
    if _freq.method == "POST" and _freq.path == "/api/games" and response.status_code in (200, 201):
        name = (_freq.get_json(silent=True) or {}).get("title_original", "").strip()
        if name:
            from GameSentenceMiner.profile_switcher import ProfileSwitcher
            _add_to_profiles_yml(name)
            if ProfileSwitcher.create_profile(name):
                _apply_profile_defaults(name)
            anki_yml = _load_anki_yml()
            if anki_yml:
                from GameSentenceMiner.util.config.configuration import get_master_config, ProfileConfig
                master = get_master_config()
                if name in master.configs:
                    pd = master.configs[name].to_dict()
                    if _apply_anki_yml_to_profile(pd, anki_yml):
                        master.configs[name] = ProfileConfig.from_dict(pd)
                        master.save()

    # UI delete game → remove from profiles.yml + config.json
    title = getattr(_tls, "delete_title", None)
    if title and _freq.method == "DELETE" and response.status_code in (200, 204):
        parts = _freq.path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "games"]:
            _remove_from_profiles_yml(title)
            _remove_profile_from_config(title)

    return response


@_flask_app.route("/frame")
def _current_frame():
    import stream as _stream_mod
    from flask import Response
    frame = _stream_mod.latest_frame
    if not frame:
        return Response(status=204)
    return Response(frame, mimetype="image/jpeg",
                    headers={"Cache-Control": "no-store"})



@_flask_app.route("/set-game", methods=["POST"])
def _set_game():
    name = (_freq.json or {}).get("name", "").strip()
    identity.set_runtime_game(name)
    print(f"game identity set: {name!r}", flush=True)
    if name and name != "Default":
        _add_to_profiles_yml(name)
    # Also switch GSM's internal active profile so card processing uses the right config
    target = name or "Default"
    try:
        from GameSentenceMiner.util.config.configuration import get_master_config, switch_profile_and_save
        m = get_master_config()
        if m and target in m.configs:
            switch_profile_and_save(target)
            print(f"[bridge] GSM profile switched to {target!r}", flush=True)
        elif m:
            print(f"[bridge] profile {target!r} not in GSM configs — staying on {m.current_profile!r}", flush=True)
    except Exception as e:
        print(f"[bridge] GSM profile switch error: {e}", flush=True)
    return _fjson({"ok": True, "game": name or "(cleared)"})


@_flask_app.route("/bridge-sync.js")
def _bridge_sync_js():
    import json as _json
    data = _load_yml()
    profiles = sorted(data.get("profiles", []) or [])
    ui_defaults = dict((data.get("defaults", {}) or {}).get("ui", {}) or {})
    ws_url = os.environ.get("TEXTHOOKER_WS_URL", "")
    if ws_url:
        ui_defaults["websocketUrl"] = ws_url
    import hashlib as _hl
    ver = _hl.md5(_json.dumps(ui_defaults, sort_keys=True).encode()).hexdigest()[:8]
    js = (_BRIDGE_JS_TEMPLATE
          .replace("__PROFILES__", _json.dumps(profiles))
          .replace("__UI_DEFAULTS__", _json.dumps(ui_defaults))
          .replace("__DEFAULTS_VER__", _json.dumps(ver)))
    return _fResp(js, mimetype="application/javascript")


def _patch_gsm_replay():
    import subprocess, tempfile, time as _time
    import GameSentenceMiner.obs as _obs_mod
    import stream as _s
    from datetime import datetime

    def _save_replay():
        from GameSentenceMiner.util.config.configuration import get_config, gsm_state

        line = (getattr(gsm_state, "line_for_screenshot", None)
                or getattr(gsm_state, "line_for_audio", None))
        if line is None:
            try:
                from GameSentenceMiner import anki as _anki
                if _anki.card_queue:
                    line = _anki.card_queue[-1][3]
            except Exception:
                pass

        line_ts = getattr(line, "time", None)
        now     = datetime.now()

        # Build ~30s replay buffer ending at now — matches GSM's timing formula:
        # total_seconds = file_length − (anki_card_creation_time − game_line.time)
        audio_combined = _s.get_audio_replay_buffer(now)
        hq_path        = _s.get_hq_frame_near(line_ts) if line_ts else None
        pipe_frame     = (_s.get_frame_near(line_ts) if line_ts else None) or _s.latest_frame

        if hq_path is None and pipe_frame is None:
            print("replay: no frame available", flush=True)
            return

        watch_dir = get_config().paths.folder_to_watch
        os.makedirs(watch_dir, exist_ok=True)
        uid      = int(now.timestamp() * 1000)
        mkv_path = os.path.join(watch_dir, f"GSM_{uid}.mkv")

        if hq_path:
            jpg_src, cleanup_src = hq_path, False
        else:
            jpg_src = os.path.join(tempfile.gettempdir(), f"gsm_{uid}.jpg")
            with open(jpg_src, "wb") as f:
                f.write(pipe_frame)
            cleanup_src = True

        try:
            if audio_combined:
                cmd = ["ffmpeg", "-y",
                       "-loop", "1", "-framerate", "30", "-i", jpg_src,
                       "-i", audio_combined,
                       "-map", "0:v", "-r", "30", "-c:v", "mjpeg", "-q:v", "3",
                       "-map", "1:a", "-c:a", "copy",
                       "-shortest",
                       mkv_path]
            else:
                cmd = ["ffmpeg", "-y",
                       "-loop", "1", "-framerate", "30", "-i", jpg_src,
                       "-t", "2",
                       "-r", "30", "-c:v", "mjpeg", "-q:v", "3", mkv_path]
            subprocess.run(cmd, capture_output=True, timeout=30)
            src_label = "hq" if hq_path else "pipe"
            print(f"replay: {mkv_path} src={src_label} audio={'yes' if audio_combined else 'no'}", flush=True)
        except Exception as e:
            print(f"replay: failed: {e}", flush=True)
        finally:
            if cleanup_src:
                try:
                    os.unlink(jpg_src)
                except OSError:
                    pass
            if audio_combined:
                try:
                    os.unlink(audio_combined)
                except OSError:
                    pass

    _obs_mod.save_replay_buffer = _save_replay

    # Unlock Anki card polling — normally gated on obs_service != None (OBS connected).
    # With GSM_ELECTRON=1 obs_service is always None, so polling never starts without this.
    import GameSentenceMiner.anki as _anki_mod
    _anki_mod._is_anki_polling_allowed = lambda: True


def _start_gsm_background_services():
    """Start the two GSM background services that gsm.py normally launches."""
    from GameSentenceMiner import anki as _anki_mod, replay_handler
    from GameSentenceMiner.util.config.configuration import get_config
    from watchdog.observers import Observer

    # Replay file watcher — picks up GSM_*.mkv files written by _save_replay()
    watch_dir = get_config().paths.folder_to_watch
    os.makedirs(watch_dir, exist_ok=True)
    extractor = replay_handler.ReplayAudioExtractor()
    observer = Observer()
    observer.schedule(replay_handler.ReplayFileWatcher(extractor), watch_dir, recursive=False)
    observer.start()
    print(f"[bridge] file watcher started: {watch_dir}", flush=True)

    # Anki polling thread — detects new Yomitan cards and calls queue_card_for_processing()
    _anki_mod.start_monitoring_anki()
    print("[bridge] Anki monitor started", flush=True)


def start_gsm_web_server():
    from GameSentenceMiner.util.config.configuration import get_config
    from GameSentenceMiner.web import register_routes
    from GameSentenceMiner.web.texthooking_page import start_web_server
    get_config().advanced.localhost_bind_address = "0.0.0.0"
    register_routes()
    start_web_server()


async def main():
    controller.bridge_loop = asyncio.get_event_loop()

    import GameSentenceMiner.gametext  # noqa: F401 — must import before start_web_server
    _patch_gsm_replay()

    threading.Thread(target=start_gsm_web_server, daemon=True).start()
    print("GSM web server starting on :7275", flush=True)

    _sync_profiles()

    if not SWITCH_STREAM:
        print("ERROR: SWITCH_STREAM env var is required", flush=True)
        sys.exit(1)

    controller.get_controller()

    from GameSentenceMiner.util.cron.run_crons import cron_scheduler
    cron_scheduler.start()

    _start_gsm_background_services()

    await asyncio.gather(mjpeg_server(), bridge_loop(SWITCH_STREAM))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
