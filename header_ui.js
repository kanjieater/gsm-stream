(function() {
  // CSS handles everything static: layout, sizing, hidden buttons.
  // Applied before Svelte renders; stays active through all reactive updates.
  var style = document.createElement('style');
  style.textContent =
    // Full-width header, no clip on absolute settings panel
    'header{left:0!important;right:0!important;width:100%!important;' +
      'align-items:center!important;overflow:visible!important}' +

    // Mobile: buttons row 1, timer full-width row 2
    '@media(max-width:799px){' +
      'header{flex-wrap:wrap!important;justify-content:flex-start!important}' +
      'header .timer{order:999!important;flex:0 0 100%!important;min-width:0!important}' +
    '}' +
    // Desktop: single row, timer leftmost
    '@media(min-width:800px){' +
      'header{flex-wrap:nowrap!important;justify-content:flex-end!important}' +
      'header .timer{order:-1!important;margin-right:auto!important;flex:0 1 auto!important;min-width:0!important}' +
    '}' +

    // 44px touch targets for all interactive elements in header
    'header [role="button"],header button{' +
      'min-height:44px!important;min-width:44px!important;' +
      'display:inline-flex!important;align-items:center!important}' +

    // Settings gear: bare SVG direct child of header
    'header>svg{min-height:44px!important;min-width:44px!important;' +
      'padding:10px!important;box-sizing:border-box!important;' +
      'cursor:pointer!important;flex-shrink:0!important}' +

    // Timer sizing
    'header .timer{text-align:left!important;min-height:44px!important;' +
      'line-height:44px!important;padding:0 10px!important;font-size:1.25rem!important}' +

    // Show preset select (GSM hides it on mobile via .hide-on-mobile)
    'select.w-48{display:block!important;height:44px!important;' +
      'margin:0 6px!important;flex-shrink:0!important}' +
    'header .hide-on-mobile{display:inline-flex!important}' +

    // Settings panel uses justify-content:flex-end, which packs content upward.
    // When content > container height, early items overflow ABOVE the visible area.
    // Force flex-start so content starts at the top and overflow goes below (scrollable).
    '.overscroll-contain{justify-content:flex-start!important}' +

    // Hide header buttons the user doesn't need
    'header [role="button"][title="Undo last Action"],' +
    'header [role="button"][title="Delete last Line"],' +
    'header [role="button"][title="Open Statistics Page"],' +
    'header [role="button"][title="Open Floating Window"],' +
    'header [role="button"][title="Create media folder (no Anki card) for selected or last line"]' +
    '{display:none!important}';

  document.head.appendChild(style);

  // ── Settings panel positioning ───────────────────────────────────────────────
  // Svelte sets .overscroll-contain { top: 44px } based on a plain 44px header.
  // Our CSS modifications make the header taller (padding etc), so the panel's
  // top edge ends up behind the header. Fix: push the panel top down to match
  // the actual header height whenever the panel appears.
  function _fixPanelTop(panel) {
    if (!panel) return;
    var header = document.querySelector('header');
    if (!header) return;
    var h = Math.ceil(header.getBoundingClientRect().height);
    panel.style.setProperty('top', h + 'px', 'important');
  }

  function _checkMutations(mutations) {
    mutations.forEach(function(m) {
      m.addedNodes.forEach(function(node) {
        if (!node || node.nodeType !== 1) return;
        var panel = node.classList && node.classList.contains('overscroll-contain')
          ? node
          : (node.querySelector ? node.querySelector('.overscroll-contain') : null);
        if (panel) _fixPanelTop(panel);
      });
    });
  }

  // Gear click capture: catches reopens when panel already exists in DOM
  document.addEventListener('click', function(e) {
    var gear = document.querySelector('header > svg');
    if (gear && (e.target === gear || gear.contains(e.target))) {
      setTimeout(function() { _fixPanelTop(document.querySelector('.overscroll-contain')); }, 60);
    }
  }, true);

  function _init() {
    new MutationObserver(_checkMutations).observe(document.body, {childList: true, subtree: true});
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _init);
  } else {
    _init();
  }

  // ── Stream preview panel ────────────────────────────────────────────────────
  (function() {
    var visible = false;
    var pollTimer = null;

    var panel = document.createElement('div');
    panel.style.cssText = 'position:fixed;top:155px;left:8px;right:8px;z-index:9999;display:none;background:#000;border:1px solid #444;border-radius:6px;overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,.6)';

    var wrap = document.createElement('div');
    wrap.style.cssText = 'position:relative;line-height:0';
    var img = document.createElement('img');
    img.style.cssText = 'display:block;width:100%;height:auto';
    var cvs = document.createElement('canvas');
    cvs.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none';
    var lbl = document.createElement('div');
    lbl.style.cssText = 'position:absolute;bottom:4px;left:4px;font:10px/1.3 monospace;color:#fff;background:rgba(0,0,0,.55);padding:2px 5px;border-radius:3px;pointer-events:none';
    wrap.appendChild(img); wrap.appendChild(cvs); wrap.appendChild(lbl);
    panel.appendChild(wrap);
    document.body.appendChild(panel);

    function drawBoxes(data) {
      cvs.width = img.offsetWidth || 320;
      cvs.height = img.offsetHeight || 180;
      var ctx = cvs.getContext('2d');
      ctx.clearRect(0, 0, cvs.width, cvs.height);
      if (!data || !data.prediction) { lbl.textContent = ''; return; }
      var pred = data.prediction, fw = data.frame_width, fh = data.frame_height;
      if (!fw || !fh) return;
      var sx = cvs.width / fw, sy = cvs.height / fh;
      function box(bbox, fill, stroke) {
        var x = bbox[0]*sx, y = bbox[1]*sy, w = (bbox[2]-bbox[0])*sx, h = (bbox[3]-bbox[1])*sy;
        ctx.fillStyle = fill; ctx.fillRect(x, y, w, h);
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
      fetch('/profiler-debug')
        .then(function(r){ return r.ok ? r.json() : null; })
        .then(drawBoxes)
        .catch(function(){ drawBoxes(null); });
      pollTimer = setTimeout(poll, 500);
    }

    var btn = document.createElement('button');
    btn.title = 'Toggle stream preview';
    btn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="22" height="22" fill="currentColor"><path d="M4,4H7L9,2H15L17,4H20A2,2 0 0,1 22,6V18A2,2 0 0,1 20,20H4A2,2 0 0,1 2,18V6A2,2 0 0,1 4,4M12,7A5,5 0 0,0 7,12A5,5 0 0,0 12,17A5,5 0 0,0 17,12A5,5 0 0,0 12,7M12,9A3,3 0 0,1 15,12A3,3 0 0,1 12,15A3,3 0 0,1 9,12A3,3 0 0,1 12,9Z"/></svg>';
    btn.style.cssText = 'background:none;border:none;cursor:pointer;padding:0;color:inherit;flex-shrink:0;';
    btn.addEventListener('click', function() {
      visible = !visible;
      panel.style.display = visible ? 'block' : 'none';
      if (visible) poll(); else { clearTimeout(pollTimer); pollTimer = null; }
    });

    function _insert() {
      var header = document.querySelector('header');
      if (!header) { setTimeout(_insert, 200); return; }
      if (header.querySelector('[title="Toggle stream preview"]')) return;
      var gear = document.querySelector('header > svg');
      if (gear && gear.nextSibling) header.insertBefore(btn, gear.nextSibling);
      else header.appendChild(btn);
    }
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', function() { setTimeout(_insert, 600); });
    } else {
      setTimeout(_insert, 600);
    }
  })();

  // ── Reset Lines header button ───────────────────────────────────────────────
  (function() {
    var rlBtn = document.createElement('button');
    rlBtn.title = 'Reset Lines';
    rlBtn.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="22" height="22" fill="currentColor"><path d="M19,4H15.5L14.5,3H9.5L8.5,4H5V6H19M6,19A2,2 0 0,0 8,21H16A2,2 0 0,0 18,19V7H6V19Z"/></svg>';
    rlBtn.style.cssText = 'background:none;border:none;cursor:pointer;padding:0;color:inherit;flex-shrink:0;';

    // Mask ID for hiding the settings panel while we automate through it
    var MASK_ID = '__reset-mask';

    function _maskPanel() {
      if (document.getElementById(MASK_ID)) return;
      var s = document.createElement('style');
      s.id = MASK_ID;
      s.textContent = '.overscroll-contain{opacity:0!important;pointer-events:none!important}';
      document.head.appendChild(s);
    }

    function _unmaskPanel() {
      var s = document.getElementById(MASK_ID);
      if (s) s.remove();
    }

    function _closeSettings() {
      if (!document.querySelector('.overscroll-contain')) return;
      var gear = document.querySelector('header > svg');
      if (gear) gear.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
    }

    function _clickResetData(panel) {
      var spans = panel.querySelectorAll('span');
      for (var i = 0; i < spans.length; i++) {
        if (spans[i].textContent && spans[i].textContent.trim() === 'Reset Data') {
          (spans[i].parentElement || spans[i]).click();
          return true;
        }
      }
      return false;
    }

    // After _clickResetData sets the Ze store, Svelte adds the confirmation dialog to DOM.
    // The dialog has position:fixed so it floats above everything — but it's a child of
    // the settings panel, so settings must stay open until we auto-confirm.
    function _watchAndAutoConfirm(onDone) {
      var obs = new MutationObserver(function() {
        var btns = document.querySelectorAll('button.btn-primary.btn-sm');
        for (var i = 0; i < btns.length; i++) {
          if (btns[i].textContent.trim() === 'Confirm') {
            obs.disconnect();
            clearTimeout(giveUp);
            btns[i].click();
            if (onDone) setTimeout(onDone, 50);
            return;
          }
        }
      });
      obs.observe(document.body, {childList: true, subtree: true});
      var giveUp = setTimeout(function() { obs.disconnect(); }, 3000);
    }

    rlBtn.addEventListener('click', function() {
      var panel = document.querySelector('.overscroll-contain');
      if (panel) {
        // Settings already open — don't mask, just auto-confirm and leave settings alone
        _watchAndAutoConfirm(null);
        _clickResetData(panel);
        return;
      }

      // Settings closed — open silently (masked), click Reset Data, auto-confirm, close
      _maskPanel();
      var gear = document.querySelector('header > svg');
      if (!gear) { _unmaskPanel(); return; }
      var done = false;
      var obs2 = new MutationObserver(function() {
        if (done) return;
        var p = document.querySelector('.overscroll-contain');
        if (!p) return;
        // Panel container may appear before Svelte populates its children.
        // Keep watching until the Reset Data span actually exists.
        var rd = Array.from(p.querySelectorAll('span')).find(function(s) {
          return s.textContent.trim() === 'Reset Data';
        });
        if (!rd) return;
        done = true;
        obs2.disconnect();
        _watchAndAutoConfirm(function() {
          _closeSettings();
          _unmaskPanel();
        });
        (rd.parentElement || rd).click();
      });
      obs2.observe(document.body, {childList: true, subtree: true});
      gear.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window}));
      setTimeout(function() {
        if (!done) { obs2.disconnect(); _unmaskPanel(); }
      }, 2000);
    });

    function _insert() {
      var header = document.querySelector('header');
      if (!header) { setTimeout(_insert, 200); return; }
      if (header.querySelector('[title="Reset Lines"]')) return;
      var camera = header.querySelector('[title="Toggle stream preview"]');
      if (camera && camera.nextSibling) header.insertBefore(rlBtn, camera.nextSibling);
      else header.appendChild(rlBtn);
    }
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', function() { setTimeout(_insert, 700); });
    } else {
      setTimeout(_insert, 700);
    }
  })();

})();
