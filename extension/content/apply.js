(() => {
  const RF = (window.__reframe = window.__reframe || {});
  if (RF.applyLoaded) return;
  RF.applyLoaded = true;

  const STYLE_ID = 'reframe-page-style';
  const RENDER_STYLE_ID = 'reframe-render-style';
  const ROOT_ID = 'reframe-root';

  const ALLOWED_STYLE_PROPS = new Set([
    'color', 'background-color', 'font-size', 'font-weight', 'font-family', 'line-height',
    'letter-spacing', 'word-spacing', 'text-align', 'text-decoration', 'text-transform',
    'padding', 'margin', 'border', 'border-radius', 'outline', 'box-shadow',
    'width', 'max-width', 'min-width', 'height', 'min-height', 'max-height',
    'display', 'opacity', 'gap', 'cursor',
  ]);
  const BAD_STYLE_VALUE = /url\(|expression|javascript:|@|[;{}<>\\]|\/\*/i;
  const BAD_CSS = /url\s*\(|expression\s*\(|javascript:|@import|@font-face|@namespace|@charset|behavior\s*:|-moz-binding|<\/?\s*style/gi;
  const PAGE_STYLE_KEYS = ['font_scale', 'line_height', 'high_contrast', 'dark_mode', 'hide_images', 'reduce_motion', 'readable_font', 'underline_links'];

  const DROP_TAGS = new Set([
    'SCRIPT', 'STYLE', 'IFRAME', 'OBJECT', 'EMBED', 'LINK', 'META', 'BASE', 'TEMPLATE', 'NOSCRIPT',
    'SVG', 'MATH', 'FRAME', 'FRAMESET', 'APPLET', 'CANVAS', 'VIDEO', 'AUDIO', 'SOURCE', 'TRACK', 'PORTAL', 'DIALOG',
  ]);
  const ALLOWED_TAGS = new Set([
    'DIV', 'SECTION', 'ARTICLE', 'HEADER', 'FOOTER', 'NAV', 'MAIN', 'ASIDE', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6',
    'P', 'SPAN', 'STRONG', 'EM', 'B', 'I', 'U', 'SMALL', 'BR', 'HR', 'UL', 'OL', 'LI', 'A', 'BUTTON', 'LABEL',
    'INPUT', 'SELECT', 'OPTION', 'TEXTAREA', 'TABLE', 'THEAD', 'TBODY', 'TFOOT', 'TR', 'TH', 'TD', 'CAPTION',
    'FIGURE', 'FIGCAPTION', 'DETAILS', 'SUMMARY', 'FIELDSET', 'LEGEND', 'DL', 'DT', 'DD', 'BLOCKQUOTE', 'IMG',
    'MARK', 'TIME', 'ABBR',
  ]);
  const INPUT_TYPES = /^(text|email|tel|number|date|search|url|password|checkbox|radio|month|week|time)$/;

  const state = (RF.state = {
    inlineStyles: new Map(),
    inputValues: new Map(),
    ariaLabels: new Map(),
    pageStyle: {},
    rendered: false,
  });

  const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));
  const isOurs = (el) => Boolean(el?.closest?.('[data-reframe-root],[data-reframe-widget]'));

  function protectedNode(el) {
    return !el || el === document.documentElement || el === document.body || isOurs(el);
  }

  // ---------- page stylesheet ----------

  function buildPageCss() {
    const ps = state.pageStyle;
    let css = `
      html:not([data-reframe-peek]) [data-reframe-hidden]{display:none!important}
      html[data-reframe-pending] body{opacity:.35!important;transition:opacity .15s ease!important;pointer-events:none!important}
      [data-reframe-highlight]{outline:4px solid #f5a300!important;outline-offset:3px!important}
      [data-reframe-label]:not(input){font-size:0!important}
      [data-reframe-label]:not(input)>*{display:none!important}
      [data-reframe-label]:not(input)::after{content:attr(data-reframe-label);font-size:18px;line-height:1.3}
      #${ROOT_ID}{box-sizing:border-box;width:min(100% - 32px,880px);margin:24px auto;font-size:20px;line-height:1.5}
      #${ROOT_ID} *{box-sizing:border-box}
      #${ROOT_ID} [data-rf-target]{cursor:pointer}
      #${ROOT_ID} :focus-visible{outline:3px solid currentColor;outline-offset:3px}
    `;
    const filters = [];
    if (typeof ps.font_scale === 'number' && ps.font_scale !== 1) {
      css += `body{zoom:${clamp(ps.font_scale, 0.8, 2.5)}!important}`;
    }
    if (typeof ps.line_height === 'number') {
      css += `body,body *{line-height:${clamp(ps.line_height, 1, 2.5)}!important}`;
    }
    if (ps.high_contrast) filters.push('contrast(1.5)');
    if (ps.dark_mode) {
      filters.push('invert(1) hue-rotate(180deg)');
      css += 'html{background:#000!important}';
      css += 'img,video,picture,canvas,iframe,[style*="background-image"]{filter:invert(1) hue-rotate(180deg)!important}';
    }
    if (filters.length) css += `body{filter:${filters.join(' ')}!important}`;
    if (ps.hide_images) css += 'img,picture,video{display:none!important}';
    if (ps.reduce_motion) {
      css += '*,*::before,*::after{animation:none!important;transition:none!important;scroll-behavior:auto!important}';
    }
    if (ps.readable_font) {
      css += 'body,body *:not(i):not(code):not(pre):not([class*="icon"]):not([class*="fa-"]):not([class*="material"]){font-family:"Atkinson Hyperlegible",Verdana,Tahoma,Arial,sans-serif!important;letter-spacing:.02em!important}';
    }
    if (ps.underline_links) css += 'a{text-decoration:underline!important;text-underline-offset:3px!important}';
    return css;
  }

  function styleTag(id) {
    let tag = document.getElementById(id);
    if (!tag) {
      tag = document.createElement('style');
      tag.id = id;
      (document.head || document.documentElement).appendChild(tag);
    }
    return tag;
  }

  function updatePageStyle() {
    styleTag(STYLE_ID).textContent = buildPageCss();
  }

  RF.setPending = (on) => {
    updatePageStyle();
    if (on) document.documentElement.setAttribute('data-reframe-pending', '');
    else document.documentElement.removeAttribute('data-reframe-pending');
    RF.ui?.setWorking?.(on);
  };

  // ---------- generated HTML: sanitize, place, wire up ----------

  function cleanStyleAttr(value) {
    const v = String(value);
    return v.length <= 600 && !/url\s*\(|expression|javascript:|@import|behavior|-moz-binding/i.test(v) ? v : null;
  }

  function scopedCss(css) {
    let text = String(css || '').replace(/\/\*[\s\S]*?\*\//g, '').replace(BAD_CSS, 'x(');
    text = text.replace(/#reframe-root\b/g, '&');
    let depth = 0;
    for (const ch of text) {
      if (ch === '{') depth++;
      else if (ch === '}' && --depth < 0) return '';
    }
    if (depth !== 0) return '';
    return `#${ROOT_ID}{${text}}`;
  }

  function pageImageSources() {
    return new Set([...document.images].map((img) => img.currentSrc || img.src).filter(Boolean));
  }

  function sanitizeInto(source, target, images) {
    for (const node of source.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) {
        target.appendChild(document.createTextNode(node.textContent));
        continue;
      }
      if (node.nodeType !== Node.ELEMENT_NODE) continue;
      const tag = node.tagName.toUpperCase();
      if (DROP_TAGS.has(tag)) continue;
      if (!ALLOWED_TAGS.has(tag)) {
        sanitizeInto(node, target, images);
        continue;
      }
      if (tag === 'IMG') {
        const src = node.getAttribute('src');
        let abs = null;
        try {
          abs = src ? new URL(src, location.href).href : null;
        } catch {}
        if (!abs || !images.has(abs)) continue;
      }

      const el = document.createElement(tag.toLowerCase());
      for (const { name, value } of [...node.attributes]) {
        const n = name.toLowerCase();
        if (n === 'class') el.setAttribute('class', value.slice(0, 200));
        else if (n === 'style') {
          const v = cleanStyleAttr(value);
          if (v) el.setAttribute('style', v);
        } else if (n === 'role' || n.startsWith('aria-')) el.setAttribute(n, value.slice(0, 200));
        else if (n === 'data-rf-target' || n === 'data-rf-bind') {
          if (/^r\d{1,5}$/.test(value) && RF.byRef(value)) el.setAttribute(n, value);
        } else if (n === 'data-rf-confirm' || n === 'data-rf-primary') el.setAttribute(n, '1');
        else if (n === 'id' || n === 'for') el.setAttribute(n, `rf-${value.replace(/[^\w-]/g, '').slice(0, 40)}`);
        else if (n === 'type' && tag === 'INPUT') el.setAttribute('type', INPUT_TYPES.test(value) ? value : 'text');
        else if (['placeholder', 'title', 'alt'].includes(n)) el.setAttribute(n, value.slice(0, 200));
        else if (n === 'src' && tag === 'IMG') el.setAttribute('src', new URL(value, location.href).href);
        else if (['rows', 'cols', 'colspan', 'rowspan'].includes(n) && /^\d{1,3}$/.test(value)) el.setAttribute(n, value);
        else if (n === 'value' && tag === 'OPTION') el.setAttribute('value', value.slice(0, 200));
      }
      if (tag === 'BUTTON') el.setAttribute('type', 'button');
      if (el.hasAttribute('data-rf-target') && !/^(BUTTON|INPUT|SELECT|TEXTAREA)$/.test(tag)) {
        el.setAttribute('tabindex', '0');
        if (!el.hasAttribute('role')) el.setAttribute('role', 'button');
      }
      sanitizeInto(node, el, images);
      target.appendChild(el);
    }
  }

  function setNativeValue(input, value) {
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(input), 'value')?.set;
    if (setter) setter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function pressElement(target) {
    const opts = { bubbles: true, cancelable: true, view: window };
    try {
      target.focus({ preventScroll: true });
    } catch {}
    target.dispatchEvent(new PointerEvent('pointerdown', opts));
    target.dispatchEvent(new MouseEvent('mousedown', opts));
    target.dispatchEvent(new PointerEvent('pointerup', opts));
    target.dispatchEvent(new MouseEvent('mouseup', opts));
    target.click();
  }

  async function activate(node) {
    const target = RF.byRef(node.getAttribute('data-rf-target'));
    if (!target) {
      node.setAttribute('aria-disabled', 'true');
      RF.notifyPageChanged?.('missing');
      return;
    }
    if (node.hasAttribute('data-rf-confirm')) {
      const label = (node.innerText || node.textContent || '').trim().slice(0, 80) || 'Continue?';
      if (!(await RF.ui.confirm(label))) return;
    }
    RF.notify?.('rf:acted');
    pressElement(target);
    RF.afterAction?.();
  }

  function wireBinding(control) {
    const target = RF.byRef(control.getAttribute('data-rf-bind'));
    if (!target) {
      control.disabled = true;
      return;
    }
    const tag = control.tagName;
    if (tag === 'SELECT' && target.tagName === 'SELECT') {
      control.replaceChildren(
        ...[...target.options].map((opt, i) => {
          const o = document.createElement('option');
          o.value = String(i);
          o.textContent = opt.text;
          return o;
        })
      );
      control.selectedIndex = target.selectedIndex;
      control.addEventListener('change', () => {
        target.selectedIndex = control.selectedIndex;
        target.dispatchEvent(new Event('input', { bubbles: true }));
        target.dispatchEvent(new Event('change', { bubbles: true }));
      });
      return;
    }
    if (tag === 'INPUT' && /^(checkbox|radio)$/.test(target.type)) {
      control.type = target.type;
      control.checked = target.checked;
      control.addEventListener('click', (e) => {
        e.preventDefault();
        pressElement(target);
        control.checked = target.checked;
      });
      return;
    }
    if (tag === 'INPUT' || tag === 'TEXTAREA') {
      if (tag === 'INPUT' && target.type === 'password') control.type = 'password';
      const ac = target.getAttribute('autocomplete');
      if (ac) control.setAttribute('autocomplete', ac);
      control.value = target.value || '';
      control.addEventListener('input', () => setNativeValue(target, control.value));
      if (tag === 'INPUT' && target.form) {
        control.addEventListener('keydown', async (e) => {
          if (e.key !== 'Enter') return;
          e.preventDefault();
          if (await RF.ui.confirm('Submit this form?')) {
            target.form.requestSubmit ? target.form.requestSubmit() : target.form.submit();
            RF.afterAction?.();
          }
        });
      }
    }
  }

  function siteHeader() {
    for (const el of document.querySelectorAll('header, [role="banner"]')) {
      if (isOurs(el) || el.closest('main, article, aside, footer, dialog')) continue;
      const rect = el.getBoundingClientRect();
      if (rect.height > 0 && rect.height < 320 && rect.top < 240) return el;
    }
    return null;
  }

  function bodyLevel(el) {
    let node = el;
    while (node && node.parentElement && node.parentElement !== document.body) node = node.parentElement;
    return node && node.parentElement === document.body ? node : null;
  }

  function isolate(keepEls) {
    const chains = new Set();
    for (const el of keepEls) for (let n = el; n && n !== document.body; n = n.parentElement) chains.add(n);
    const prune = (parent) => {
      for (const child of parent.children) {
        if (keepEls.includes(child) || isOurs(child) || /^(SCRIPT|STYLE|LINK|NOSCRIPT|TEMPLATE)$/.test(child.tagName)) continue;
        if (chains.has(child)) prune(child);
        else if (!child.hasAttribute('data-reframe-hidden')) child.setAttribute('data-reframe-hidden', 'iso');
      }
    };
    prune(document.body);
  }

  function clearRender() {
    document.getElementById(ROOT_ID)?.remove();
    document.getElementById(RENDER_STYLE_ID)?.remove();
    for (const el of document.querySelectorAll('[data-reframe-hidden="iso"]')) el.removeAttribute('data-reframe-hidden');
    state.rendered = false;
  }

  function render(op) {
    clearRender();
    const parsed = new DOMParser().parseFromString(`<body>${op.html}</body>`, 'text/html');
    const root = document.createElement('div');
    root.id = ROOT_ID;
    root.setAttribute('data-reframe-root', '');
    sanitizeInto(parsed.body, root, pageImageSources());
    if (!root.childNodes.length) return;

    styleTag(RENDER_STYLE_ID).textContent = scopedCss(op.css);

    const isolating = op.isolate !== false;
    const keepEls = (op.keep_refs || []).map(RF.byRef).filter((el) => el && !protectedNode(el));
    if (isolating && !keepEls.length) {
      const header = siteHeader();
      if (header) keepEls.push(header);
    }
    // When rebuilding the whole view, sit under the site header as a body-level column so the
    // site's own grid/flex layout can't squeeze the panel into a sidebar slot.
    const anchor = !isolating && op.anchor_ref ? RF.byRef(op.anchor_ref) : null;
    if (anchor && !protectedNode(anchor)) {
      const pos = op.position || 'prepend';
      if (pos === 'before') anchor.before(root);
      else if (pos === 'after') anchor.after(root);
      else if (pos === 'append') anchor.append(root);
      else anchor.prepend(root);
    } else {
      const tops = keepEls.map(bodyLevel).filter(Boolean);
      const last = tops.sort((a, b) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1)).pop();
      if (last) last.after(root);
      else document.body.prepend(root);
    }

    root.addEventListener('click', (e) => {
      const node = e.target.closest('[data-rf-target]');
      if (!node || !root.contains(node)) return;
      e.preventDefault();
      activate(node);
    });
    root.addEventListener('keydown', (e) => {
      const node = e.target.closest('[data-rf-target]');
      if (!node || node.tagName === 'BUTTON' || (e.key !== 'Enter' && e.key !== ' ')) return;
      e.preventDefault();
      activate(node);
    });
    root.querySelectorAll('[data-rf-bind]').forEach(wireBinding);

    if (isolating) isolate([...keepEls, root]);
    state.rendered = true;
    root.scrollIntoView({ block: 'start' });
    window.scrollBy(0, -16);
    const first = root.querySelector('[data-rf-primary][data-rf-target]') || root.querySelector('[data-rf-target]');
    first?.focus({ preventScroll: true });
  }

  // ---------- operations ----------

  function saveInlineStyle(el) {
    if (!state.inlineStyles.has(el)) state.inlineStyles.set(el, el.getAttribute('style'));
  }

  const handlers = {
    render,
    hide(op) {
      for (const ref of op.refs) {
        const el = RF.byRef(ref);
        if (!protectedNode(el)) el.setAttribute('data-reframe-hidden', 'op');
      }
    },
    unhide(op) {
      for (const ref of op.refs) RF.byRef(ref)?.removeAttribute('data-reframe-hidden');
    },
    highlight(op) {
      op.refs.forEach((ref, i) => {
        const el = RF.byRef(ref);
        if (protectedNode(el)) return;
        el.setAttribute('data-reframe-highlight', '');
        if (i === 0 && !state.rendered) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      });
    },
    relabel(op) {
      const el = RF.byRef(op.ref);
      if (protectedNode(el)) return;
      if (!state.ariaLabels.has(el)) state.ariaLabels.set(el, el.getAttribute('aria-label'));
      el.setAttribute('aria-label', op.text);
      if (el.tagName === 'INPUT') {
        if (!state.inputValues.has(el)) state.inputValues.set(el, el.value);
        el.value = op.text;
      }
      el.setAttribute('data-reframe-label', op.text);
    },
    style(op) {
      const el = RF.byRef(op.ref);
      if (protectedNode(el)) return;
      saveInlineStyle(el);
      for (const [prop, value] of Object.entries(op.styles || {})) {
        const p = String(prop).toLowerCase().trim();
        const v = String(value).trim();
        if (ALLOWED_STYLE_PROPS.has(p) && v.length <= 80 && !BAD_STYLE_VALUE.test(v)) el.style.setProperty(p, v, 'important');
      }
    },
    page_style(op) {
      for (const key of PAGE_STYLE_KEYS) {
        const value = op[key];
        if (value === null || value === undefined) continue;
        if (value === false || (key === 'font_scale' && value === 1)) delete state.pageStyle[key];
        else state.pageStyle[key] = value;
      }
    },
    scroll_to(op) {
      const el = RF.byRef(op.ref);
      if (!el) return;
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      el.setAttribute('data-reframe-highlight', '');
    },
    restore() {
      RF.restoreAll();
    },
  };

  RF.applyOps = (ops) => {
    let applied = 0;
    let failed = 0;
    updatePageStyle();
    for (const op of ops) {
      const handler = handlers[op?.op];
      if (!handler) {
        failed++;
        continue;
      }
      try {
        handler(op);
        applied++;
      } catch (err) {
        console.warn('[ReFrame] operation failed', op.op, err);
        failed++;
      }
    }
    if (ops.some((op) => op?.op !== 'restore')) updatePageStyle();
    return { applied, failed, summary: RF.appliedSummary() };
  };

  RF.appliedSummary = () => {
    const out = [];
    if (state.rendered) out.push('rebuilt view shown');
    const hidden = [...document.querySelectorAll('[data-reframe-hidden="op"]')].map((e) => e.getAttribute('data-reframe-ref')).filter(Boolean);
    if (hidden.length) out.push(`hidden: ${hidden.slice(0, 20).join(',')}`);
    const hl = document.querySelectorAll('[data-reframe-highlight]').length;
    if (hl) out.push(`${hl} highlighted`);
    const relabeled = document.querySelectorAll('[data-reframe-label]').length;
    if (relabeled) out.push(`${relabeled} relabeled`);
    if (state.inlineStyles.size) out.push(`${state.inlineStyles.size} restyled`);
    const ps = Object.entries(state.pageStyle).map(([k, v]) => `${k}=${v}`);
    if (ps.length) out.push(`page style: ${ps.join(', ')}`);
    return out;
  };

  RF.restoreAll = () => {
    clearRender();
    state.pageStyle = {};
    document.documentElement.removeAttribute('data-reframe-pending');
    for (const el of document.querySelectorAll('[data-reframe-hidden],[data-reframe-highlight],[data-reframe-label]')) {
      el.removeAttribute('data-reframe-hidden');
      el.removeAttribute('data-reframe-highlight');
      el.removeAttribute('data-reframe-label');
    }
    for (const [el, original] of state.inlineStyles) {
      if (original === null) el.removeAttribute('style');
      else el.setAttribute('style', original);
    }
    for (const [el, original] of state.inputValues) el.value = original;
    for (const [el, original] of state.ariaLabels) {
      if (original === null) el.removeAttribute('aria-label');
      else el.setAttribute('aria-label', original);
    }
    state.inlineStyles.clear();
    state.inputValues.clear();
    state.ariaLabels.clear();
    for (const el of document.querySelectorAll('[data-reframe-ref]')) el.removeAttribute('data-reframe-ref');
    RF.refs?.clear();
    updatePageStyle();
    RF.ui?.setWorking?.(false);
  };

  RF.setPageStyle = (pageStyle) => {
    handlers.page_style(pageStyle || {});
    updatePageStyle();
  };
})();
