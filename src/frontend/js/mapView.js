// Map rendering module — supports the dashboard mini-frame and the full-view modal.
// API:
//   mapView.init(canvasId, opts)  — initialize a canvas (opts.interactive = pan/zoom)
//   mapView.refreshPose(pose)     — update robot pose; redraws all active canvases
//   mapView.getState()            — returns { img, gMapDesc, tfROS2Canvas } (read-only)
//   mapView.destroy(canvasId)     — tear down a canvas registration
//
// State:
//   _state.canvases: Map<id, { canvas, ctx, view, interactive, container, handlers }>
//   _state.activeCanvasId: which canvas the current draw cycle targets
//   _state.img: shared map image (loaded once, used by all canvases)
//   _state.gMapDesc: shared map descriptor

(function (global) {
  const _state = {
    canvases: new Map(),
    activeCanvasId: null,
    img: null,
    gMapDesc: {
      w: 1060, h: 827,
      origin: { x: -29.4378, y: -26.3988 },
      resolution: 0.05,
    },
    robotTheta: 0,
    _robotSprite: null,
    _animStarted: false,
  };

  // ───── Coordinate transform ─────
  function tfROS2Canvas(mapDesc, rosPt) {
    if (!mapDesc || !mapDesc.origin || !mapDesc.resolution || !mapDesc.h) return {};
    const xRosOffset = mapDesc.origin.x / mapDesc.resolution;
    const yRosOffset = mapDesc.origin.y / mapDesc.resolution;
    const xCanvas = (rosPt.x / mapDesc.resolution - xRosOffset).toFixed(4);
    let yCanvas = (rosPt.y / mapDesc.resolution - yRosOffset);
    yCanvas = (mapDesc.h - yCanvas).toFixed(4);
    return { x: parseFloat(xCanvas), y: parseFloat(yCanvas) };
  }

  function normalizeAngle(a) {
    while (a > Math.PI) a -= 2 * Math.PI;
    while (a < -Math.PI) a += 2 * Math.PI;
    return a;
  }

  // ───── Map config loader ─────
  async function loadMapConfig() {
    try {
      const res = await dataService.getActiveMapInfo();
      if (res.status === 'ok') {
        _state.gMapDesc.w = res.width;
        _state.gMapDesc.h = res.height;
        _state.gMapDesc.origin = res.origin;
        _state.gMapDesc.resolution = res.resolution;
        return `/api/maps/${res.map_id}/image?v=${Date.now()}`;
      }
    } catch (e) {
      // No active map or error — use fallback
    }
    return 'vac_map.png';
  }

  // ───── Load the map image, apply transforms to all canvases, then redraw.
  //       Returns a Promise that resolves once the image is ready (or fails).
  //       onError may reassign img.src to retry with a fallback — onload still
  //       fires for the fallback before the Promise resolves. ─────
  function loadMapImage(mapSrc, { onError } = {}) {
    return new Promise((resolve) => {
      const img = new Image();
      img.onload = () => {
        _state.img = img;
        for (const e of _state.canvases.values()) applyTransform(e);
        drawAll();
        resolve();
      };
      img.onerror = () => {
        if (onError) {
          const prevSrc = img.src;
          onError(img);
          // If onError swapped to a different src, let onload/onerror fire again.
          if (img.src !== prevSrc) return;
        }
        resolve();
      };
      img.src = mapSrc;
    });
  }

  // ───── Reload the active map (re-fetch metadata + image, redraw all canvases) ─────
  function reload() {
    _state.img = null;
    return loadMapConfig().then(mapSrc => loadMapImage(mapSrc));
  }

  // ───── Auto-fit transform: scales + centres the map to fill the canvas ─────
  function applyFitTransform(entry) {
    const { canvas, view } = entry;
    const desc = _state.gMapDesc;
    if (!canvas.width || !canvas.height || !desc.w || !desc.h) return;
    const scale = Math.min(canvas.width / desc.w, canvas.height / desc.h);
    view.scale = scale;
    view.tx = (canvas.width - desc.w * scale) / 2;
    view.ty = (canvas.height - desc.h * scale) / 2;
  }

  // ───── Centre the map at scale 1 (no fit) ─────
  function applyCenterTransform(entry) {
    const { canvas, view } = entry;
    view.tx = canvas.width / 2 - _state.gMapDesc.w / 2;
    view.ty = canvas.height / 2 - _state.gMapDesc.h / 2;
  }

  // ───── Dispatch to the right transform for an entry ─────
  function applyTransform(entry) {
    if (entry.fit) applyFitTransform(entry);
    else applyCenterTransform(entry);
  }

  // ───── Init a canvas ─────
  function init(canvasId, opts = {}) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const container = canvas.parentElement;
    const w = container.clientWidth || 800;
    const h = container.clientHeight || 600;
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext('2d');
    const view = {
      tx: w / 2 - _state.gMapDesc.w / 2,
      ty: h / 2 - _state.gMapDesc.h / 2,
      scale: 1, minScale: 0.3, maxScale: 5,
      dragging: false, lastX: 0, lastY: 0,
    };
    const entry = { canvas, ctx, view, interactive: !!opts.interactive, fit: !!opts.fit, container, handlers: [] };
    _state.canvases.set(canvasId, entry);
    _state.activeCanvasId = canvasId;
    if (entry.fit) applyFitTransform(entry);

    if (!_state.img) {
      loadMapConfig().then(mapSrc => {
        const onError = (img) => { if (mapSrc !== 'vac_map.png') img.src = 'vac_map.png'; };
        return loadMapImage(mapSrc, { onError });
      }).then(() => {
        const loading = document.getElementById('map-loading');
        if (loading) loading.style.display = 'none';
      });
    } else {
      drawAll();
    }

    if (opts.interactive) attachInteractive(canvasId);

    // Resize observer per canvas
    const ro = new ResizeObserver(() => {
      const nw = container.clientWidth;
      const nh = container.clientHeight;
      if (nw > 0 && nh > 0) {
        canvas.width = nw;
        canvas.height = nh;
        if (entry.fit) applyFitTransform(entry);
        drawCanvas(canvasId);
      }
    });
    ro.observe(container);
    entry.resizeObserver = ro;

    // Start the animation loop once (any canvas)
    if (!_state._animStarted) {
      _state._animStarted = true;
      requestAnimationFrame(animateMap);
    }
  }

  // ───── Pan/zoom listeners (interactive canvases only) ─────
  function attachInteractive(canvasId) {
    const entry = _state.canvases.get(canvasId);
    if (!entry) return;
    const { canvas, view } = entry;

    canvas.style.cursor = 'grab';

    const onMouseDown = e => {
      view.dragging = true;
      view.lastX = e.clientX;
      view.lastY = e.clientY;
      canvas.style.cursor = 'grabbing';
    };
    const onMouseMove = e => {
      if (!view.dragging) return;
      view.tx += e.clientX - view.lastX;
      view.ty += e.clientY - view.lastY;
      view.lastX = e.clientX;
      view.lastY = e.clientY;
      drawCanvas(canvasId);
    };
    const onMouseUp = () => {
      view.dragging = false;
      canvas.style.cursor = 'grab';
    };
    const onWheel = e => {
      e.preventDefault();
      const mx = e.offsetX, my = e.offsetY;
      const mapX = (mx - view.tx) / view.scale;
      const mapY = (my - view.ty) / view.scale;
      let s = view.scale * (e.deltaY < 0 ? 1.1 : 0.9);
      s = Math.max(view.minScale, Math.min(view.maxScale, s));
      view.tx = mx - mapX * s;
      view.ty = my - mapY * s;
      view.scale = s;
      drawCanvas(canvasId);
    };
    const onDblClick = () => {
      view.tx = canvas.width / 2 - _state.gMapDesc.w / 2;
      view.ty = canvas.height / 2 - _state.gMapDesc.h / 2;
      view.scale = 1;
      drawCanvas(canvasId);
    };

    // Touch
    const touchState = { lastDist: 0, lastCenter: { x: 0, y: 0 } };
    const onTouchStart = e => {
      e.preventDefault();
      if (e.touches.length === 1) {
        view.dragging = true;
        view.lastX = e.touches[0].clientX;
        view.lastY = e.touches[0].clientY;
      } else if (e.touches.length === 2) {
        view.dragging = false;
        const dx = e.touches[1].clientX - e.touches[0].clientX;
        const dy = e.touches[1].clientY - e.touches[0].clientY;
        touchState.lastDist = Math.sqrt(dx * dx + dy * dy);
        touchState.lastCenter = {
          x: (e.touches[0].clientX + e.touches[1].clientX) / 2,
          y: (e.touches[0].clientY + e.touches[1].clientY) / 2,
        };
      }
    };
    const onTouchMove = e => {
      e.preventDefault();
      if (e.touches.length === 1 && view.dragging) {
        const t = e.touches[0];
        view.tx += t.clientX - view.lastX;
        view.ty += t.clientY - view.lastY;
        view.lastX = t.clientX;
        view.lastY = t.clientY;
        drawCanvas(canvasId);
      } else if (e.touches.length === 2) {
        const dx = e.touches[1].clientX - e.touches[0].clientX;
        const dy = e.touches[1].clientY - e.touches[0].clientY;
        const dist = Math.sqrt(dx * dx + dy * dy);
        const center = {
          x: (e.touches[0].clientX + e.touches[1].clientX) / 2,
          y: (e.touches[0].clientY + e.touches[1].clientY) / 2,
        };
        const rect = canvas.getBoundingClientRect();
        const cx = center.x - rect.left;
        const cy = center.y - rect.top;
        const mapX = (cx - view.tx) / view.scale;
        const mapY = (cy - view.ty) / view.scale;
        let s = view.scale * (dist / touchState.lastDist);
        s = Math.max(view.minScale, Math.min(view.maxScale, s));
        view.tx = cx - mapX * s;
        view.ty = cy - mapY * s;
        view.scale = s;
        touchState.lastDist = dist;
        touchState.lastCenter = center;
        drawCanvas(canvasId);
      }
    };
    const onTouchEnd = e => {
      e.preventDefault();
      if (e.touches.length === 0) {
        view.dragging = false;
      } else if (e.touches.length === 1) {
        view.dragging = true;
        view.lastX = e.touches[0].clientX;
        view.lastY = e.touches[0].clientY;
      }
    };

    canvas.addEventListener('mousedown', onMouseDown);
    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onMouseUp);
    canvas.addEventListener('wheel', onWheel, { passive: false });
    canvas.addEventListener('dblclick', onDblClick);
    canvas.addEventListener('touchstart', onTouchStart, { passive: false });
    canvas.addEventListener('touchmove', onTouchMove, { passive: false });
    canvas.addEventListener('touchend', onTouchEnd, { passive: false });

    // Track for teardown
    entry.handlers.push(
      { target: canvas, type: 'mousedown', fn: onMouseDown },
      { target: window, type: 'mousemove', fn: onMouseMove },
      { target: window, type: 'mouseup', fn: onMouseUp },
      { target: canvas, type: 'wheel', fn: onWheel, opts: { passive: false } },
      { target: canvas, type: 'dblclick', fn: onDblClick },
      { target: canvas, type: 'touchstart', fn: onTouchStart, opts: { passive: false } },
      { target: canvas, type: 'touchmove', fn: onTouchMove, opts: { passive: false } },
      { target: canvas, type: 'touchend', fn: onTouchEnd, opts: { passive: false } },
    );
  }

  // ───── Draw ─────
  function drawAll() {
    for (const id of _state.canvases.keys()) drawCanvas(id);
  }

  function drawCanvas(canvasId) {
    const entry = _state.canvases.get(canvasId);
    if (!entry) return;
    const { canvas, ctx, view } = entry;
    if (!ctx || !canvas) return;

    // ── Map layer (scaled) ──
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.translate(view.tx, view.ty);
    ctx.scale(view.scale, view.scale);
    if (_state.img) {
      ctx.drawImage(_state.img, 0, 0, _state.gMapDesc.w, _state.gMapDesc.h);
    }
    ctx.restore();

    // ── Overlay layer (screen-space) ──
    // Icons scale with the map when view.scale >= 1 (so they grow when modal
    // is zoomed in) but never shrink below their natural pixel size — this
    // gives a readable icon on the mini auto-fit (scale < 1) AND a zoom-aware
    // icon in the modal.
    const iconScale = Math.max(view.scale, 1);
    const toScreen = (mapPos) => ({
      x: mapPos.x * view.scale + view.tx,
      y: mapPos.y * view.scale + view.ty,
    });

    if (robotData && robotData.pose) {
      const pos = tfROS2Canvas(_state.gMapDesc, robotData.pose);
      if (Number.isFinite(pos.x) && Number.isFinite(pos.y)) {
        const sp = toScreen(pos);
        ctx.save();
        ctx.translate(sp.x, sp.y);
        ctx.rotate(_state.robotTheta || 0);
        ctx.scale(iconScale, iconScale);
        const sprite = _state._robotSprite;
        if (sprite && sprite.complete) {
          ctx.drawImage(sprite, -8, -5, 16, 10);
        } else {
          ctx.fillStyle = '#ff8800';
          ctx.beginPath();
          ctx.moveTo(0, -10);
          ctx.lineTo(-7, 7);
          ctx.lineTo(7, 7);
          ctx.closePath();
          ctx.fill();
        }
        ctx.restore();
      }
    }

    if (typeof shelfDropPose !== 'undefined' && shelfDropPose) {
      const dropPos = tfROS2Canvas(_state.gMapDesc, shelfDropPose);
      if (Number.isFinite(dropPos.x) && Number.isFinite(dropPos.y)) {
        const sp = toScreen(dropPos);
        ctx.save();
        ctx.translate(sp.x, sp.y);
        ctx.scale(iconScale, iconScale);
        ctx.beginPath();
        ctx.arc(0, 0, 10, 0, 2 * Math.PI);
        ctx.fillStyle = 'rgba(255, 0, 0, 0.3)';
        ctx.fill();
        ctx.beginPath();
        ctx.arc(0, 0, 5, 0, 2 * Math.PI);
        ctx.fillStyle = 'red';
        ctx.fill();
        ctx.strokeStyle = '#fff';
        ctx.lineWidth = 2 / iconScale;
        ctx.beginPath();
        ctx.moveTo(-3, -3); ctx.lineTo(3, 3);
        ctx.moveTo(3, -3); ctx.lineTo(-3, 3);
        ctx.stroke();
        ctx.restore();
      }
    }
  }

  // ───── Animation loop ─────
  function animateMap() {
    // Skip drawing when no registered canvas is currently attached/visible —
    // avoids per-frame draws against detached or hidden canvases.
    const anyVisible = [..._state.canvases.values()].some(e => e.canvas?.offsetParent !== null);
    if (!anyVisible) {
      requestAnimationFrame(animateMap);
      return;
    }
    if (robotData && robotData.pose && robotData.pose.theta !== undefined) {
      const target = -robotData.pose.theta;
      const current = _state.robotTheta || 0;
      const diff = normalizeAngle(target - current);
      if (Math.abs(diff) > 0.017) {
        _state.robotTheta = normalizeAngle(current + diff * 0.1);
      } else {
        _state.robotTheta = target;
      }
    }
    drawAll();
    requestAnimationFrame(animateMap);
  }

  function refreshPose(_pose) { drawAll(); }

  // ───── Mini-canvas + full-view modal ─────
  function initMini() {
    init('robot-mini-canvas', { interactive: false, fit: true });
  }

  function openModal() {
    const modal = document.getElementById('robot-modal');
    if (!modal) return;
    modal.removeAttribute('hidden');
    // Init the modal canvas lazily on first open; redraw on subsequent opens
    if (!_state.canvases.has('map-canvas')) {
      init('map-canvas', { interactive: true });
    } else {
      drawAll();
    }
    // Wire close affordances (× button, backdrop click, ESC key)
    const close = () => closeModal();
    const closeBtn = document.getElementById('robot-modal-close');
    if (closeBtn) closeBtn.onclick = close;
    const backdrop = modal.querySelector('.robot-modal-backdrop');
    if (backdrop) backdrop.onclick = close;
    // Replace any previous handler so re-opening doesn't accumulate listeners
    if (modal._escHandler) {
      document.removeEventListener('keydown', modal._escHandler);
    }
    modal._escHandler = (e) => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', modal._escHandler);
  }

  function closeModal() {
    const modal = document.getElementById('robot-modal');
    if (!modal) return;
    modal.setAttribute('hidden', '');
    // Drop the modal canvas from the per-frame draw set; openModal re-inits it.
    destroy('map-canvas');
    if (modal._escHandler) {
      document.removeEventListener('keydown', modal._escHandler);
      modal._escHandler = null;
    }
  }

  function destroy(canvasId) {
    const entry = _state.canvases.get(canvasId);
    if (!entry) return;
    if (entry.resizeObserver) entry.resizeObserver.disconnect();
    for (const h of entry.handlers) {
      h.target.removeEventListener(h.type, h.fn, h.opts);
    }
    _state.canvases.delete(canvasId);
    if (_state.activeCanvasId === canvasId) {
      _state.activeCanvasId = _state.canvases.keys().next().value || null;
    }
  }

  // Robot sprite — load once
  (function () {
    const sprite = new Image();
    sprite.src = 'assets/icons/kachaka.png';
    _state._robotSprite = sprite;
  })();

  // Public API
  global.mapView = {
    init,
    refreshPose,
    reload,
    initMini,
    openModal,
    closeModal,
    getState: () => ({ img: _state.img, gMapDesc: { ..._state.gMapDesc }, tfROS2Canvas }),
    destroy,
  };
})(window);
