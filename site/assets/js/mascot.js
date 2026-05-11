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
// 用户随时可以点 × 收起，状态记在 localStorage(rp-mascot)；下次刷新还是收起态。

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
  if (localStorage.getItem(STORAGE_KEY) === 'off') return;
  if (document.getElementById('rp-mascot')) return;  // already mounted

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

  function injectDOM() {
    const wrap = document.createElement('div');
    wrap.id = 'rp-mascot';
    wrap.innerHTML = `
      <button class="rp-mascot__close" aria-label="收起站娘" title="收起">×</button>
      <canvas id="rp-mascot-canvas" width="${CANVAS_WIDTH}" height="${CANVAS_HEIGHT}"></canvas>
    `;
    document.body.appendChild(wrap);

    wrap.querySelector('.rp-mascot__close').addEventListener('click', () => {
      wrap.remove();
      localStorage.setItem(STORAGE_KEY, 'off');
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

    injectDOM();
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
  }

  // Defer to next macrotask so we don't compete with critical site JS for
  // network in the first 200ms (PIXI bundle is ~400KB gzip).
  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    setTimeout(init, 600);
  } else {
    window.addEventListener('DOMContentLoaded', () => setTimeout(init, 600));
  }
})();
