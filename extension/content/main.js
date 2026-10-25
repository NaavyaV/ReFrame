(() => {
  const RF = (window.__reframe = window.__reframe || {});
  if (RF.mainLoaded) return;
  RF.mainLoaded = true;

  let navigating = false;
  addEventListener('beforeunload', () => {
    navigating = true;
  });
  addEventListener('pagehide', () => {
    navigating = true;
  });

  RF.notify = (type, data = {}) => {
    if (!RF.alive()) {
      RF.teardown?.();
      return Promise.resolve(null);
    }
    try {
      return chrome.runtime.sendMessage({ type, ...data }).catch(() => null);
    } catch {
      return Promise.resolve(null);
    }
  };

  RF.notifyPageChanged = (reason) => {
    if (!navigating) RF.notify('rf:page-changed', { reason });
  };

  // After a click that doesn't unload the page, rebuild only if the page actually changed.
  RF.afterAction = () => {
    const startUrl = location.href;
    let changes = 0;
    const observer = new MutationObserver((mutations) => {
      for (const m of mutations) {
        if (m.target.closest?.('[data-reframe-root],[data-reframe-widget]')) continue;
        changes += m.addedNodes.length + m.removedNodes.length;
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    setTimeout(() => {
      observer.disconnect();
      if (!navigating && (location.href !== startUrl || changes > 2)) RF.notifyPageChanged('action');
    }, 450);
  };

  let lastUrl = location.href;
  const urlTimer = setInterval(() => {
    if (!RF.alive()) {
      clearInterval(urlTimer);
      RF.teardown?.();
      return;
    }
    if (location.href !== lastUrl) {
      lastUrl = location.href;
      RF.notifyPageChanged('url');
    }
  }, 500);

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    switch (msg?.type) {
      case 'rf:ping':
        sendResponse({ ok: true });
        break;
      case 'rf:state':
        RF.ui.setState(msg.state);
        break;
      case 'rf:toggle':
        RF.ui.toggle();
        sendResponse({ ok: true });
        break;
      case 'rf:pending':
        RF.setPending(Boolean(msg.on));
        break;
      case 'rf:voice-state':
        RF.ui.setVoiceState(msg.state);
        break;
      case 'rf:speak':
        RF.ui.speak(msg);
        break;
      case 'rf:snapshot':
        sendResponse({ snapshot: RF.takeSnapshot(), applied: RF.appliedSummary() });
        break;
      case 'rf:apply':
        RF.ui.setState({ busy: false });
        RF.setPending(false);
        sendResponse(RF.applyOps(Array.isArray(msg.ops) ? msg.ops : []));
        break;
      case 'rf:page-style':
        RF.setPageStyle(msg.pageStyle);
        sendResponse({ ok: true });
        break;
      case 'rf:restore':
        RF.restoreAll();
        sendResponse({ ok: true });
        break;
      default:
        return false;
    }
    return false;
  });

  RF.notify('rf:hello').then((res) => {
    if (!res) return;
    if (res.pageStyle) RF.setPageStyle(res.pageStyle);
    if (res.pending) RF.setPending(true);
    RF.ui.setState(res.state);
  });
})();
