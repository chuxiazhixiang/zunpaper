// Live2D 站娘 (LSS) — 右下角，眼睛跟随鼠标，每隔几秒随机切换表情。
//
// 实现链：
//   live2dcubismcore.min.js  ← Live2D 官方运行时（自部署在 assets/live2d/core/）
//   pixi.js@6.5              ← WebGL 渲染框架
//   pixi-live2d-display@0.4  ← 把 .model3.json 喂给 PIXI（cubism4 build）
//
// 整个脚本是「单文件挂载」：每个 HTML 只要加一个
//   <script src="assets/js/mascot.js" defer></script>
// 就行。其它依赖会按顺序异步注入。
//
// 交互：右上角小箭头 = 隐藏（不卸载，只把整个挂件折成一个角落小气泡）；
// 点小气泡 = 把站娘叫回来。状态记在 localStorage(rp-mascot)：
//   'shown' (默认) | 'hidden'

(function () {
  const STORAGE_KEY = 'rp-mascot';
  const MIN_VIEWPORT_WIDTH = 768;       // 手机宽度不显示
  const CANVAS_WIDTH = 240;
  const CANVAS_HEIGHT = 320;

  // 6 个表情参数（从 cdi3.json 的「表情」ParamGroup 里提取）：
  //   Param  = 眼镜    Param2 = 啊这    Param3 = 好耶
  //   Param4 = 红     Param5 = 汉      Param6 = 手     Param7 = 眼睛
  // Param 是「眼镜」属于装饰用，不参与切换。
  const EXPR_PARAMS = ['Param2', 'Param3', 'Param4', 'Param5', 'Param6', 'Param7'];

  if (typeof window === 'undefined' || !window.document) return;
  if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return;
  if (window.innerWidth < MIN_VIEWPORT_WIDTH) return;
  if (document.getElementById('rp-mascot')) return;  // already mounted

  // 一旦写过 'off'（旧版本的"永久关闭"），自动升级为隐藏态，给用户一次复活机会。
  if (localStorage.getItem(STORAGE_KEY) === 'off') {
    localStorage.setItem(STORAGE_KEY, 'hidden');
  }

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = src;
      s.async = false;     // preserve order: cubism core → pixi → display
      s.crossOrigin = 'anonymous';
      s.onload = () => resolve(src);
      s.onerror = () => reject(new Error(`failed to load ${src}`));
      document.head.appendChild(s);
    });
  }

  let _modelLoaded = false;
  let _loadingModel = false;

  function applyVisibility(state) {
    const wrap = document.getElementById('rp-mascot');
    const bubble = document.getElementById('rp-mascot-bubble');
    const hidden = state === 'hidden';
    if (wrap) wrap.classList.toggle('is-hidden', hidden);
    if (bubble) bubble.classList.toggle('is-active', hidden);
    localStorage.setItem(STORAGE_KEY, hidden ? 'hidden' : 'shown');
    // 第一次从「隐藏」切到「显示」时再加载 PIXI + 模型，避免给关掉的用户白挂 ~400KB JS。
    if (!hidden && !_modelLoaded && !_loadingModel) {
      _loadingModel = true;
      init().finally(() => { _loadingModel = false; });
    }
  }

  function injectBubble() {
    if (document.getElementById('rp-mascot-bubble')) return;
    const b = document.createElement('button');
    b.id = 'rp-mascot-bubble';
    b.type = 'button';
    b.title = '叫站娘出来';
    b.setAttribute('aria-label', '叫站娘出来');
    b.textContent = '🫧';
    b.addEventListener('click', () => applyVisibility('shown'));
    document.body.appendChild(b);
  }

  function injectShell() {
    if (document.getElementById('rp-mascot')) return;
    const wrap = document.createElement('div');
    wrap.id = 'rp-mascot';
    wrap.innerHTML = `
      <button class="rp-mascot__close" aria-label="隐藏站娘" title="隐藏（右下角小气泡可叫回）">－</button>
      <canvas id="rp-mascot-canvas" width="${CANVAS_WIDTH}" height="${CANVAS_HEIGHT}"></canvas>
    `;
    document.body.appendChild(wrap);

    wrap.querySelector('.rp-mascot__close').addEventListener('click', (e) => {
      e.stopPropagation();
      applyVisibility('hidden');
    });
  }

  /** Center the model inside the canvas using its rendered bounds. */
  function centerModel(model, canvas) {
    // 让模型尺寸 ≈ canvas 的 92%，居中。
    const padFactor = 0.92;
    const desiredHeight = canvas.height * padFactor;
    // model 是 PIXI.Container；它有 width/height（已含 scale）。
    const baseScale = desiredHeight / (model.height / model.scale.y || 1);
    model.scale.set(baseScale);
    const b = model.getBounds();
    model.x += (canvas.width - b.width) / 2 - b.x;
    model.y += (canvas.height - b.height) / 2 - b.y;
    // 留出底部一点空间显示「身体」而不是只切到脖子。
    model.y -= 6;
  }

  async function init() {
    if (_modelLoaded) return;
    try {
      await loadScript('assets/live2d/core/live2dcubismcore.min.js');
      await loadScript('https://cdn.jsdelivr.net/npm/pixi.js@6.5.10/dist/browser/pixi.min.js');
      await loadScript('https://cdn.jsdelivr.net/npm/pixi-live2d-display@0.4.0/dist/cubism4.min.js');
    } catch (e) {
      console.warn('[mascot] dependency load failed', e);
      return;
    }

    if (!window.PIXI || !window.PIXI.live2d || !window.PIXI.live2d.Live2DModel) {
      console.warn('[mascot] pixi-live2d-display not available after load');
      return;
    }

    injectShell();
    const canvas = document.getElementById('rp-mascot-canvas');

    const app = new window.PIXI.Application({
      view: canvas,
      width: CANVAS_WIDTH,
      height: CANVAS_HEIGHT,
      autoStart: true,
      antialias: true,
      backgroundAlpha: 0,
      // High-DPI awareness — keeps the canvas sharp on retina.
      resolution: window.devicePixelRatio || 1,
      autoDensity: true,
    });

    const { Live2DModel } = window.PIXI.live2d;

    let model;
    try {
      model = await Live2DModel.from('assets/live2d/LSS/LSS.model3.json', {
        autoInteract: false,  // we manage tracking ourselves on document level
      });
    } catch (e) {
      console.warn('[mascot] model load failed', e);
      return;
    }

    app.stage.addChild(model);
    centerModel(model, app.screen);

    // ----- Eye + head tracking ----------------------------------------------
    // pixi-live2d-display 自带 focus()：传 canvas 坐标系下的 (x, y)，
    // 它会自动驱动 ParamAngleX/Y/Z、ParamBodyAngleX/Y 和 ParamEyeBallX/Y。
    function onMouseMove(e) {
      const rect = canvas.getBoundingClientRect();
      const localX = e.clientX - rect.left;
      const localY = e.clientY - rect.top;
      model.focus(localX, localY);
    }
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('touchmove', (e) => {
      if (!e.touches[0]) return;
      onMouseMove(e.touches[0]);
    }, { passive: true });

    // ----- Random expression switcher ---------------------------------------
    // 这套模型在 model3.json 里没声明 expressions 数组，但 cdi3.json 里
    // 「表情」ParamGroup 下有 Param2..Param7。我们直接把它们当离散开关用：
    // 每隔 9-15s 随机点亮一个，2-4s 后归零。
    let activeParam = null;
    function setExpr(id, value) {
      try {
        model.internalModel.coreModel.setParameterValueById(id, value);
      } catch (_) {}
    }
    function cycle() {
      if (activeParam) setExpr(activeParam, 0);
      const next = EXPR_PARAMS[Math.floor(Math.random() * EXPR_PARAMS.length)];
      setExpr(next, 1);
      activeParam = next;
      setTimeout(() => {
        setExpr(next, 0);
        if (activeParam === next) activeParam = null;
      }, 2500 + Math.random() * 2000);
    }
    setTimeout(cycle, 1800);                          // 第一次稍早
    const exprTimer = setInterval(cycle, 9000 + Math.random() * 6000);

    // ----- Tap on model = play one expression immediately -------------------
    canvas.addEventListener('click', () => {
      cycle();
    });

    // Clean up on page nav (mostly cosmetic — fresh load wipes everything).
    window.addEventListener('pagehide', () => {
      clearInterval(exprTimer);
      document.removeEventListener('mousemove', onMouseMove);
    });

    _modelLoaded = true;
    // 在 init 完成后再把可见性 class 同步上去（防止用户在加载过程中又点了隐藏）
    if (localStorage.getItem(STORAGE_KEY) === 'hidden') {
      applyVisibility('hidden');
    } else {
      applyVisibility('shown');
    }
  }

  // Boot: always inject the bubble so the user can call her back if hidden.
  // Then decide whether to eagerly load PIXI+model based on saved visibility.
  function boot() {
    injectBubble();
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === 'hidden') {
      // 站娘默认隐藏，让小气泡显出来等用户召唤；不下载 PIXI
      const b = document.getElementById('rp-mascot-bubble');
      if (b) b.classList.add('is-active');
      return;
    }
    // 默认 / 'shown'：异步预热 PIXI + 模型
    init();
  }

  // Defer to next macrotask so we don't compete with critical site JS for
  // network in the first 200ms (PIXI bundle is ~400KB gzip).
  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    setTimeout(boot, 600);
  } else {
    window.addEventListener('DOMContentLoaded', () => setTimeout(boot, 600));
  }
})();
