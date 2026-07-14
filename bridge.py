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
  // defaults take effect. Cache-Control: no-store on bridge-sync.js ensures the
  // browser always fetches the current hash.
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

  // Inject CSS for things that don't need !important fights (layout, hiding TL input, etc.)
  var style = document.createElement('style');
  style.textContent = 'select.w-48{display:block!important}' +
    (UI_DEFAULTS.customCSS ? '\n' + UI_DEFAULTS.customCSS : '');
  document.head.appendChild(style);

  // Touch-friendly fixes via inline setProperty — beats Svelte's scoped !important rules.
  //
  // Root cause: the <header> is `position:fixed; top:0; right:0` with no explicit width.
  // On mobile its content overflows to the LEFT and becomes invisible.
  // Fix: extend the header to full viewport width, allow wrapping, push the timer text
  // (which is the first element) to its own second row below the buttons.
  function _fixHeader(header) {
    if (header._gsmFixed) return;
    header._gsmFixed = true;
    header.style.setProperty('left',            '0',           'important');
    header.style.setProperty('right',           '0',           'important');
    header.style.setProperty('width',           '100%',        'important');
    header.style.setProperty('flex-wrap',       'wrap',        'important');
    header.style.setProperty('justify-content', 'flex-start',  'important');
    header.style.setProperty('align-items',     'center',      'important');
    header.style.setProperty('overflow',        'visible',     'important');
  }
  function _touch44(el) {
    el.style.setProperty('min-height', '44px', 'important');
    el.style.setProperty('min-width',  '44px', 'important');
  }
  function _fixNode(el) {
    if (!el || !el.nodeType || el.nodeType !== 1) return;
    var tag = el.tagName && el.tagName.toUpperCase();
    var parentTag = el.parentElement && el.parentElement.tagName && el.parentElement.tagName.toUpperCase();
    if (tag === 'HEADER') {
      _fixHeader(el);
    }
    // Standard button / role="button" divs
    if (tag === 'BUTTON' || el.getAttribute('role') === 'button') {
      _touch44(el);
      el.style.setProperty('display',      'inline-flex', 'important');
      el.style.setProperty('align-items',  'center',      'important');
      el.querySelectorAll && el.querySelectorAll('svg').forEach(function(svg) {
        svg.style.setProperty('width',  '22px', 'important');
        svg.style.setProperty('height', '22px', 'important');
      });
    }
    // Settings icon: bare <svg> directly inside <header> (not wrapped in a div)
    if (tag === 'SVG' && parentTag === 'HEADER') {
      _touch44(el);
      el.style.setProperty('padding',     '10px',        'important');
      el.style.setProperty('box-sizing',  'border-box',  'important');
      el.style.setProperty('cursor',      'pointer',     'important');
      el.style.setProperty('flex-shrink', '0',           'important');
    }
    // Connection indicator: <div> with no role in the header (wraps the connection SVG)
    if (tag === 'DIV' && parentTag === 'HEADER' && !el.getAttribute('role')) {
      _touch44(el);
      el.style.setProperty('display',         'inline-flex', 'important');
      el.style.setProperty('align-items',     'center',      'important');
      el.style.setProperty('justify-content', 'center',      'important');
      el.style.setProperty('flex-shrink',     '0',           'important');
    }
    if (el.classList && el.classList.contains('hide-on-mobile')) {
      el.style.setProperty('display', 'inline-flex', 'important');
    }
    // Profile quick-switch select
    if (tag === 'SELECT' && el.classList && el.classList.contains('w-48')) {
      el.style.setProperty('display',     'block',   'important');
      el.style.setProperty('height',      '44px',    'important');
      el.style.setProperty('margin',      '0 10px',  'important');
      el.style.setProperty('flex-shrink', '0',       'important');
    }
    // .timer: stats string — flex:1 so it fills remaining space on the current row;
    // if it wraps alone to the next row it naturally spans the full width.
    if (el.classList && el.classList.contains('timer')) {
      el.style.setProperty('flex',        '1 1 auto',     'important');
      el.style.setProperty('min-width',   '0',            'important');
      el.style.setProperty('order',       '999',          'important');
      el.style.setProperty('text-align',  'left',         'important');
      el.style.setProperty('min-height',  '44px',         'important');
      el.style.setProperty('line-height', '44px',         'important');
      el.style.setProperty('padding',     '0 10px',       'important');
      el.style.setProperty('font-size',   '1.25rem',      'important');
    }
  }
  var _SEL = 'header, button, [role="button"], .hide-on-mobile, .timer, select.w-48';
  function _fixAll() {
    document.querySelectorAll(_SEL).forEach(_fixNode);
    // Also catch bare SVGs and unroled divs in header
    var h = document.querySelector('header');
    if (h) {
      Array.prototype.forEach.call(h.children, _fixNode);
    }
  }
  function _fixAdded(mutations) {
    mutations.forEach(function(m) {
      m.addedNodes.forEach(function(node) {
        if (!node || node.nodeType !== 1) return;
        _fixNode(node);
        if (node.querySelectorAll) {
          node.querySelectorAll(_SEL).forEach(_fixNode);
        }
        // If the header itself was added, fix its direct children too
        if (node.tagName && node.tagName.toUpperCase() === 'HEADER') {
          Array.prototype.forEach.call(node.children, _fixNode);
        }
      });
    });
  }
  function _initFixes() {
    _fixAll();
    new MutationObserver(_fixAdded).observe(document.body, {childList: true, subtree: true});
    // Retry after Svelte has had time to render
    setTimeout(_fixAll, 400);
    setTimeout(_fixAll, 1200);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _initFixes);
  } else {
    _initFixes();
  }

  // Stream preview panel — toggle with the camera button, polls /frame at ~2fps
  // Canvas overlay draws profiler bounding boxes: green=dialogue, red=ignored.
  (function() {
    var visible = false;
    var timer = null;

    var panel = document.createElement('div');
    panel.style.cssText = 'position:fixed;top:155px;left:8px;right:8px;z-index:9999;display:none;background:#000;border:1px solid #444;border-radius:6px;overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,.6)';

    // Wrapper: relative so the absolute canvas sits on top of the image.
    var wrap = document.createElement('div');
    wrap.style.cssText = 'position:relative;line-height:0';

    var img = document.createElement('img');
    img.style.cssText = 'display:block;width:100%;height:auto';

    var cvs = document.createElement('canvas');
    cvs.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none';

    var lbl = document.createElement('div');
    lbl.style.cssText = 'position:absolute;bottom:4px;left:4px;font:10px/1.3 monospace;color:#fff;background:rgba(0,0,0,.55);padding:2px 5px;border-radius:3px;pointer-events:none';

    wrap.appendChild(img);
    wrap.appendChild(cvs);
    wrap.appendChild(lbl);
    panel.appendChild(wrap);
    document.body.appendChild(panel);

    function drawBoxes(data) {
      cvs.width  = img.offsetWidth  || 320;
      cvs.height = img.offsetHeight || 180;
      var ctx = cvs.getContext('2d');
      ctx.clearRect(0, 0, cvs.width, cvs.height);
      if (!data || !data.prediction) { lbl.textContent = ''; return; }
      var pred = data.prediction;
      var fw = data.frame_width, fh = data.frame_height;
      if (!fw || !fh) return;
      var sx = cvs.width / fw, sy = cvs.height / fh;

      function box(bbox, fill, stroke) {
        var x = bbox[0]*sx, y = bbox[1]*sy, w = (bbox[2]-bbox[0])*sx, h = (bbox[3]-bbox[1])*sy;
        ctx.fillStyle = fill;   ctx.fillRect(x, y, w, h);
        ctx.strokeStyle = stroke; ctx.lineWidth = 1.5; ctx.strokeRect(x, y, w, h);
      }

      (pred.ocr_regions     || []).forEach(function(r){ box(r.bbox,'rgba(34,220,90,.28)','rgba(34,220,90,.9)'); });
      (pred.ignore_regions  || []).forEach(function(r){ box(r.bbox,'rgba(220,50,50,.28)','rgba(220,50,50,.9)'); });
      (pred.speaker_regions || []).forEach(function(r){ box(r.bbox,'rgba(250,180,0,.28)','rgba(250,180,0,.9)'); });

      var conf = pred.confidence != null ? (pred.confidence*100).toFixed(0)+'%' : '';
      lbl.textContent = (pred.mode || '') + (conf ? '  ' + conf : '');
    }

    function poll() {
      if (!visible) return;
      img.src = '/frame?' + Date.now();
      fetch('/profiler-debug').then(function(r){ return r.ok ? r.json() : null; }).then(drawBoxes).catch(function(){ drawBoxes(null); });
      timer = setTimeout(poll, 500);
    }

    var btn = document.createElement('button');
    btn.title = 'Toggle stream preview';
    btn.textContent = '📷';
    btn.style.cssText = 'font-size:22px;background:none;border:none;cursor:pointer;min-height:44px;min-width:44px;padding:0;display:inline-flex;align-items:center;justify-content:center;color:inherit;flex-shrink:0;';
    btn.onclick = function() {
      visible = !visible;
      panel.style.display = visible ? 'block' : 'none';
      if (visible) poll();
      else { clearTimeout(timer); timer = null; }
    };
    // Insert into header right after the last bare SVG (settings icon), before the profile select.
    function _insertCameraBtn() {
      var header = document.querySelector('header');
      if (!header) { setTimeout(_insertCameraBtn, 200); return; }
      var svgs = Array.prototype.filter.call(header.children, function(c) {
        return c.tagName && c.tagName.toUpperCase() === 'SVG';
      });
      var anchor = svgs.length ? svgs[svgs.length - 1] : null;
      if (anchor && anchor.nextSibling) {
        header.insertBefore(btn, anchor.nextSibling);
      } else {
        header.appendChild(btn);
      }
    }
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', function() { setTimeout(_insertCameraBtn, 600); });
    } else {
      setTimeout(_insertCameraBtn, 600);
    }
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


def _apply_vad_yml_to_profile(profile_dict: dict, vad_yml: dict) -> bool:
    """Merge profiles.yml vad section into a profile dict. Returns True if anything changed."""
    if not vad_yml:
        return False
    vad = profile_dict.setdefault("vad", {})
    changed = False
    for k, v in vad_yml.items():
        if vad.get(k) != v:
            vad[k] = v
            changed = True
    return changed


def _apply_audio_yml_to_profile(profile_dict: dict, audio_yml: dict) -> bool:
    """Merge profiles.yml audio section into a profile dict. Returns True if anything changed."""
    if not audio_yml:
        return False
    audio = profile_dict.setdefault("audio", {})
    changed = False
    for k, v in audio_yml.items():
        if audio.get(k) != v:
            audio[k] = v
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

        # Push anki + vad + audio config from profiles.yml into every profile (profiles.yml is authoritative)
        _yml       = _load_yml()
        anki_yml   = _load_anki_yml()
        vad_yml    = _yml.get("vad") or {}
        audio_yml  = _yml.get("audio") or {}
        if anki_yml or vad_yml or audio_yml:
            dirty = False
            from GameSentenceMiner.util.config.configuration import ProfileConfig
            for name in master.configs:
                pd = master.configs[name].to_dict()
                changed = _apply_anki_yml_to_profile(pd, anki_yml)
                changed |= _apply_vad_yml_to_profile(pd, vad_yml)
                changed |= _apply_audio_yml_to_profile(pd, audio_yml)
                if changed:
                    master.configs[name] = ProfileConfig.from_dict(pd)
                    dirty = True
            if dirty:
                master.save()
                print("[bridge] anki+vad+audio config synced from profiles.yml", flush=True)

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


@_flask_app.route("/profiler-debug")
def _profiler_debug():
    import profiler_bridge as _pb
    from flask import Response
    data = _pb.get_debug_overlay()
    if not data:
        return Response(status=204)
    return _fjson(data)






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
        from GameSentenceMiner.util.config.configuration import get_master_config, switch_profile_and_save, gsm_state
        gsm_state.current_game = target  # used by replay_handler for VAD output filenames
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
    return _fResp(js, mimetype="application/javascript",
                  headers={"Cache-Control": "no-store"})


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


def _patch_gsm_text_normalization():
    """Strip Yomitan furigana (漢字[よみ] → 漢字) before GSM's sentence comparison,
    and fix _match_score() to check per-line within multi-line OCR GameLines.

    Problem 1 — furigana: GSM's normalize_text_for_comparison strips bracket chars
    but leaves the reading text inside, so 月[つき]の王[おう] → 月つきの王おう ≠ 月の王.

    Problem 2 — multi-line scoring: our OCR sends all visible text as one GameLine
    (e.g. "Line A\nLine B\nLine C"). The card sentence is one of those lines. But
    fuzz.ratio("Line A Line B Line C", "Line A") ≈ 40%, so an older GameLine that
    was just "Line A" wins with 100% — wrong timestamp. Fix: take the best per-line
    score so the current multi-line GameLine scores as high as its best matching line.
    """
    import re
    import rapidfuzz.fuzz
    import GameSentenceMiner.util.text_log as _tl

    _orig_normalize = _tl.normalize_text_for_comparison
    _orig_match_score = _tl._match_score
    _furigana_re = re.compile(r'\[[^\]]*\]')

    def _normalize_strip_furigana(text: str) -> str:
        if text:
            text = _furigana_re.sub('', text)
        return _orig_normalize(text)

    def _match_score_multiline(line_text: str, anki_sentence: str) -> float:
        base = _orig_match_score(line_text, anki_sentence)
        if '\n' not in line_text:
            return base
        anki_norm = _tl.normalize_text_for_comparison(anki_sentence)
        if not anki_norm:
            return base
        best = base
        for sub in line_text.split('\n'):
            sub_norm = _tl.normalize_text_for_comparison(sub)
            if not sub_norm:
                continue
            score = rapidfuzz.fuzz.ratio(sub_norm, anki_norm)
            if score > best:
                best = score
                if best >= 100:
                    break
        return best

    _tl.normalize_text_for_comparison = _normalize_strip_furigana
    _tl._match_score = _match_score_multiline
    print("[bridge] GSM text normalization patched (furigana + multi-line scoring)", flush=True)


def _patch_vad_similarity():
    """Replace fuzz.ratio with partial_ratio in the VAD similarity gate.

    GSM's _calculate_similarity uses fuzz.ratio(text_mined, transcript).  When
    our OCR sends a single-sentence mined text but Whisper transcribes multiple
    sentences (because beginning_offset gives it a 5s window), ratio() returns
    ~26% even when the right sentence IS in the audio — punished by length diff.

    partial_ratio finds the best-matching substring of transcript vs text_mined,
    which is exactly the right question: "does the mined sentence appear in what
    Whisper heard?"  We also clip segments to only those needed to cover the mined
    text, so the audio doesn't span the next several lines of dialogue.
    """
    from GameSentenceMiner.vad import WhisperVADProcessor as _WVAP, DetectionResult
    from GameSentenceMiner import mecab as _mecab
    from rapidfuzz import fuzz as _rf_fuzz
    import logging as _log

    _vad_log = _log.getLogger("GameSentenceMiner.VAD")

    @staticmethod
    def _patched_calculate_similarity(text_mined: str, transcript: str) -> float:
        if not text_mined or not transcript:
            return 0.0
        return _rf_fuzz.partial_ratio(
            _mecab.to_hiragana(text_mined),
            _mecab.to_hiragana(transcript),
        )

    _WVAP._calculate_similarity = _patched_calculate_similarity

    _orig_detect = _WVAP._detect_voice_activity

    def _detect_trim_to_match(self, input_audio, text_mined):
        result = _orig_detect(self, input_audio, text_mined)
        segs = result.segments
        if not segs or not text_mined or len(segs) <= 1:
            return result

        # Find the minimum prefix of segments whose cumulative text best covers
        # text_mined via partial_ratio, then stop growing once the score plateaus.
        # This prevents capturing subsequent dialogue lines after the mined sentence.
        mined_h = _mecab.to_hiragana(text_mined)
        best_score = 0
        best_end = 0
        for i in range(len(segs)):
            cum = "".join(s.text for s in segs[:i + 1])
            score = _rf_fuzz.partial_ratio(mined_h, _mecab.to_hiragana(cum))
            if score >= best_score:
                best_score = score
                best_end = i
            if score < best_score - 15:
                # Score dropped significantly — extra segments don't help
                break

        if best_end < len(segs) - 1:
            _vad_log.info(
                f"[bridge] VAD: clipped to {best_end + 1}/{len(segs)} segments "
                f"(partial score {best_score:.0f} for '{text_mined[:20]}')"
            )
            result = DetectionResult(
                segments=segs[:best_end + 1],
                text_similarity=result.text_similarity,
                transcript=result.transcript,
            )
        return result

    _WVAP._detect_voice_activity = _detect_trim_to_match
    print("[bridge] VAD similarity patched (partial_ratio + segment trimming)", flush=True)


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

    # Restore current_game from the saved profile so VAD output filenames are valid
    # even if the user hasn't re-selected the game in the texthooker UI after a restart.
    try:
        from GameSentenceMiner.util.config.configuration import get_master_config, gsm_state
        m = get_master_config()
        if m and m.current_profile and m.current_profile != "Default":
            gsm_state.current_game = m.current_profile
            print(f"[bridge] current_game restored: {m.current_profile!r}", flush=True)
    except Exception as e:
        print(f"[bridge] current_game restore error: {e}", flush=True)

    # VAD processor — GSM normally calls this in post_init_async(); we bypass that entrypoint
    from GameSentenceMiner.vad import vad_processor as _vad
    import GameSentenceMiner.vad as _vad_module
    # short_text_ratio is not a VAD dataclass field so getattr() always returns the module default.
    # Patch it to 0 so multi-line OCR blocks (long text_mined) don't trigger false rejections.
    _vad_module.SHORT_TEXT_RATIO_DEFAULT = 0
    _patch_vad_similarity()
    _vad.init()
    print("[bridge] VAD processor initialized", flush=True)


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
    _patch_gsm_text_normalization()

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
