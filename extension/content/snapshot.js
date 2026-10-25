(() => {
  // After the extension reloads, the old scripts stay on the page but can no longer reach it.
  if (window.__reframe && !window.__reframe.alive?.()) {
    window.__reframe.teardown?.();
    window.__reframe = undefined;
  }
  const RF = (window.__reframe = window.__reframe || {});
  RF.alive = () => {
    try {
      return Boolean(chrome.runtime?.id);
    } catch {
      return false;
    }
  };
  if (RF.snapshotLoaded) return;
  RF.snapshotLoaded = true;

  const MAX_CONTROLS = 160;
  const MAX_BLOCKS = 40;

  const CONTROL_SELECTOR = [
    'a[href]', 'button', 'input:not([type="hidden"])', 'select', 'textarea', 'summary',
    '[role="button"]', '[role="link"]', '[role="menuitem"]', '[role="tab"]', '[role="checkbox"]',
    '[role="radio"]', '[role="switch"]', '[role="combobox"]', '[onclick]', '[contenteditable="true"]',
  ].join(',');

  const BLOCK_SELECTOR = [
    'header', 'nav', 'main', 'aside', 'footer', 'section', 'article', 'form', 'dialog', 'iframe',
    '[role="dialog"]', '[role="alertdialog"]', '[role="banner"]', '[role="navigation"]',
    '[role="complementary"]', '[role="alert"]', '[role="region"]', '[aria-modal="true"]',
    '[class*="banner" i]', '[id*="banner" i]', '[class*="cookie" i]', '[id*="cookie" i]',
    '[class*="promo" i]', '[class*="advert" i]', '[id*="advert" i]', '[class*="modal" i]',
    '[class*="popup" i]', '[class*="newsletter" i]',
  ].join(',');

  const SENSITIVE_AUTOCOMPLETE = /cc-|password|one-time-code/i;
  const SENSITIVE_NAME = /(card|cvv|cvc|ccv|security.?code|ssn|social.?security|\bpin\b|otp|passcode|password|routing|iban|expir)/i;
  const AD_HINT = /(advert|promo|banner|cookie|newsletter|sponsor|popup|modal)/i;
  const OURS = '[data-reframe-root],[data-reframe-widget]';

  RF.refs = RF.refs || new Map();
  RF.refCounter = RF.refCounter || 0;

  function refFor(el) {
    let ref = el.getAttribute('data-reframe-ref');
    if (!ref) {
      ref = `r${++RF.refCounter}`;
      el.setAttribute('data-reframe-ref', ref);
    }
    RF.refs.set(ref, el);
    return ref;
  }

  RF.byRef = (ref) => {
    const el = RF.refs.get(ref);
    if (el && el.isConnected) return el;
    return document.querySelector(`[data-reframe-ref="${CSS.escape(ref)}"]`);
  };

  const clean = (s, max) => (s || '').replace(/\s+/g, ' ').trim().slice(0, max);

  function classesOf(el) {
    const raw = typeof el.className === 'string' ? el.className : el.getAttribute('class') || '';
    const list = raw.split(/\s+/).filter((c) => c && !c.startsWith('reframe') && c.length < 40);
    return list.length ? list.slice(0, 4).join(' ').slice(0, 80) : null;
  }

  function isVisible(el) {
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return false;
    const style = getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity) > 0.05;
  }

  function labelFor(el) {
    const aria = el.getAttribute('aria-label');
    if (aria) return clean(aria, 100);
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const text = labelledBy
        .split(/\s+/)
        .map((id) => document.getElementById(id)?.innerText || '')
        .join(' ');
      if (clean(text, 100)) return clean(text, 100);
    }
    if (el.id) {
      const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (label) return clean(label.innerText, 100);
    }
    const wrapping = el.closest('label');
    if (wrapping) return clean(wrapping.innerText, 100);
    const ph = el.getAttribute('placeholder') || el.getAttribute('title');
    if (ph) return clean(ph, 100);
    const img = el.querySelector?.('img[alt]');
    if (img) return clean(img.getAttribute('alt'), 100);
    return null;
  }

  function isSensitive(el, label) {
    if (!/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return false;
    if (el.type === 'password') return true;
    if (SENSITIVE_AUTOCOMPLETE.test(el.getAttribute('autocomplete') || '')) return true;
    const hay = `${el.name || ''} ${el.id || ''} ${label || ''}`;
    return SENSITIVE_NAME.test(hay);
  }

  RF.isSensitiveField = (el) => isSensitive(el, labelFor(el));

  function hrefFor(el) {
    const raw = el.getAttribute('href');
    if (!raw || raw.startsWith('javascript:')) return null;
    if (raw.startsWith('#')) return '#';
    try {
      const url = new URL(raw, location.href);
      if (url.origin === location.origin) return url.pathname;
      return url.origin + url.pathname;
    } catch {
      return null;
    }
  }

  function regionFor(el) {
    const region = el.closest(
      'dialog,[role="dialog"],[role="alertdialog"],[aria-modal="true"],nav,[role="navigation"],header,footer,aside,form,main'
    );
    if (!region) return null;
    if (region.matches('dialog,[role="dialog"],[role="alertdialog"],[aria-modal="true"]')) return 'dialog';
    if (region.matches('nav,[role="navigation"]')) return 'nav';
    return region.tagName.toLowerCase();
  }

  const hiddenByUs = (el) => Boolean(el.closest('[data-reframe-hidden]'));

  function describeControl(el) {
    const tag = el.tagName.toLowerCase();
    const label = labelFor(el);
    const sensitive = isSensitive(el, label);
    let text = '';
    if (tag === 'input') {
      if (/^(submit|button|reset)$/.test(el.type)) text = clean(el.value, 100);
    } else if (tag === 'select') {
      text = clean([...el.options].slice(0, 8).map((o) => o.text).join(' / '), 160);
    } else if (tag !== 'textarea') {
      text = clean(el.innerText || el.textContent, 100);
    }
    if (!text && !label && !/^(input|select|textarea)$/.test(tag)) return null;
    let context = null;
    if ((text || label || '').length <= 24) {
      const container = el.closest('tr,li,[role="row"],[role="listitem"],dd,fieldset');
      const containerText = container ? clean(container.innerText, 140) : '';
      if (containerText && containerText !== text && containerText !== label) context = containerText;
    }
    return {
      ref: refFor(el),
      kind: 'control',
      tag,
      role: el.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'button' ? 'button' : null),
      text,
      label: label && label !== text ? label : null,
      context,
      href: tag === 'a' ? hrefFor(el) : null,
      input_type: tag === 'input' ? el.type || 'text' : null,
      cls: classesOf(el),
      sensitive,
      region: regionFor(el),
      hidden: hiddenByUs(el),
    };
  }

  function describeBlock(el, viewportArea) {
    const tag = el.tagName.toLowerCase();
    const rect = el.getBoundingClientRect();
    const hint = `${el.id} ${typeof el.className === 'string' ? el.className : ''}`;
    const isLandmark = /^(header|nav|main|aside|footer|dialog|form|iframe)$/.test(tag) || el.hasAttribute('role');
    const looksLikeClutter = AD_HINT.test(hint);
    const bigEnough = (rect.width * rect.height) / viewportArea > 0.03;
    if (!isLandmark && !looksLikeClutter && !bigEnough) return null;
    const heading = el.querySelector('h1,h2,h3,h4,legend');
    const text =
      tag === 'iframe'
        ? clean(el.getAttribute('title') || el.getAttribute('src') || 'embedded frame', 120)
        : clean(el.innerText, 140);
    return {
      ref: refFor(el),
      kind: 'block',
      tag,
      role: el.getAttribute('role'),
      text: looksLikeClutter ? clean(`[${hint.match(AD_HINT)[0]}] ${text}`, 140) : text,
      label: heading ? clean(heading.innerText, 100) : labelFor(el),
      cls: classesOf(el),
      region: regionFor(el.parentElement || el),
      hidden: hiddenByUs(el),
    };
  }

  // ---------- site theme, so generated UI can match it ----------

  const TRANSPARENT = /^(transparent|rgba\(0, 0, 0, 0\))$/;

  function backgroundOf(el) {
    for (let node = el; node; node = node.parentElement) {
      const bg = getComputedStyle(node).backgroundColor;
      if (!TRANSPARENT.test(bg)) return bg;
    }
    return 'rgb(255, 255, 255)';
  }

  function firstVisible(selector, test = () => true, limit = 300) {
    const nodes = document.querySelectorAll(selector);
    for (let i = 0; i < nodes.length && i < limit; i++) {
      const el = nodes[i];
      if (!el.closest(OURS) && isVisible(el) && test(el)) return el;
    }
    return null;
  }

  function extractTheme() {
    const body = getComputedStyle(document.body);
    const theme = {
      font_family: clean(body.fontFamily, 80),
      font_size: body.fontSize,
      text_color: body.color,
      background: backgroundOf(document.querySelector('main') || document.body),
    };

    const heading = firstVisible('h1,h2');
    if (heading) {
      const s = getComputedStyle(heading);
      theme.heading = { font_family: clean(s.fontFamily, 80), color: s.color, font_weight: s.fontWeight, cls: classesOf(heading) };
    }

    const link = firstVisible('main a[href], a[href]');
    if (link) theme.link_color = getComputedStyle(link).color;

    const button = firstVisible(
      'button, input[type="submit"], a[class*="btn" i], a[class*="button" i], [role="button"]',
      (el) => !TRANSPARENT.test(getComputedStyle(el).backgroundColor)
    );
    if (button) {
      const s = getComputedStyle(button);
      theme.button = {
        cls: classesOf(button), background: s.backgroundColor, color: s.color, border: s.border,
        border_radius: s.borderRadius, font_weight: s.fontWeight, text_transform: s.textTransform,
      };
    }

    const header = firstVisible('header, [role="banner"]');
    if (header) {
      const s = getComputedStyle(header);
      theme.header = { ref: refFor(header), cls: classesOf(header), background: backgroundOf(header), border_bottom: s.borderBottom };
    }

    const pageBg = theme.background;
    const card = firstVisible(
      'main section, main article, [class*="card" i], [class*="panel" i], [class*="tile" i], [class*="box" i]',
      (el) => {
        const s = getComputedStyle(el);
        return (!TRANSPARENT.test(s.backgroundColor) && s.backgroundColor !== pageBg) || s.boxShadow !== 'none' || parseFloat(s.borderTopWidth) > 0;
      }
    );
    if (card) {
      const s = getComputedStyle(card);
      theme.card = { cls: classesOf(card), background: s.backgroundColor, border: s.border, border_radius: s.borderRadius, box_shadow: s.boxShadow.slice(0, 60) };
    }

    const input = firstVisible('input[type="text"], input[type="email"], input:not([type]), select');
    if (input) {
      const s = getComputedStyle(input);
      theme.input = { cls: classesOf(input), border: s.border, border_radius: s.borderRadius };
    }
    return theme;
  }

  // ---------- snapshot ----------

  RF.takeSnapshot = () => {
    // Look at the page as it really is, including parts ReFrame has hidden.
    document.documentElement.setAttribute('data-reframe-peek', '');
    try {
      const viewportArea = Math.max(window.innerWidth * window.innerHeight, 1);
      const dialogs = [...document.querySelectorAll('dialog[open],[role="dialog"],[role="alertdialog"],[aria-modal="true"]')]
        .filter((d) => !d.closest(OURS) && isVisible(d));

      const controls = [];
      const seen = new Set();
      const pushControls = (root) => {
        for (const el of root.querySelectorAll(CONTROL_SELECTOR)) {
          if (controls.length >= MAX_CONTROLS) return;
          if (seen.has(el) || el.closest(OURS) || !isVisible(el)) continue;
          seen.add(el);
          const d = describeControl(el);
          if (d) controls.push(d);
        }
      };
      dialogs.forEach(pushControls);
      pushControls(document);

      const blocks = [];
      for (const el of document.querySelectorAll(BLOCK_SELECTOR)) {
        if (blocks.length >= MAX_BLOCKS) break;
        if (el === document.body || el.closest(OURS) || !isVisible(el)) continue;
        const d = describeBlock(el, viewportArea);
        if (d) blocks.push(d);
      }

      const headings = [...document.querySelectorAll('h1,h2,h3')]
        .filter((h) => !h.closest(OURS) && isVisible(h))
        .map((h) => clean(h.innerText, 100))
        .filter(Boolean)
        .slice(0, 15);

      return {
        url: location.origin + location.pathname,
        title: clean(document.title, 200),
        headings,
        has_dialog: dialogs.length > 0,
        theme: extractTheme(),
        elements: [...controls, ...blocks],
      };
    } finally {
      document.documentElement.removeAttribute('data-reframe-peek');
    }
  };
})();
