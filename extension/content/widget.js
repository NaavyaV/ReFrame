(() => {
  const RF = (window.__reframe = window.__reframe || {});
  if (RF.widgetLoaded) return;
  RF.widgetLoaded = true;

  const CSS = `
    :host{all:initial}
    *{box-sizing:border-box;font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
    [hidden]{display:none!important}
    .dock{position:fixed;top:72px;right:40px;display:flex;flex-direction:column;align-items:flex-end;gap:10px;
      color:#f4f6fb;font-size:15px;line-height:1.45}
    .orb{width:64px;height:64px;border-radius:50%;border:1.5px solid rgba(143,176,255,.55);cursor:pointer;color:#fff;
      background:radial-gradient(circle at 30% 25%,rgba(111,145,255,.75),rgba(18,22,32,.6) 70%);
      backdrop-filter:blur(14px) saturate(1.4);-webkit-backdrop-filter:blur(14px) saturate(1.4);
      box-shadow:0 10px 30px rgba(0,0,0,.3),0 0 0 6px rgba(111,145,255,.12);display:grid;place-items:center;opacity:.72;
      transition:opacity .2s,transform .2s,background .2s;position:relative;padding:0}
    .orb:hover,.orb[aria-expanded="true"]{opacity:1;transform:scale(1.06)}
    .orb svg{width:30px;height:30px}
    .orb.working::after{content:"";position:absolute;inset:-5px;border-radius:50%;border:2px solid transparent;
      border-top-color:#8fb0ff;border-right-color:#8fb0ff;animation:spin .8s linear infinite}
    @keyframes spin{to{transform:rotate(360deg)}}
    .panel{width:min(370px,calc(100vw - 56px));height:min(540px,calc(100vh - 170px));display:flex;flex-direction:column;
      background:rgba(16,19,28,.84);backdrop-filter:blur(22px) saturate(1.5);-webkit-backdrop-filter:blur(22px) saturate(1.5);
      border:1px solid rgba(255,255,255,.12);border-radius:20px;box-shadow:0 24px 60px rgba(0,0,0,.38);overflow:hidden;
      transform-origin:top right;animation:pop .16s ease-out}
    @keyframes pop{from{opacity:0;transform:scale(.96) translateY(-6px)}}
    .head{display:flex;align-items:center;gap:8px;padding:12px 12px 12px 16px;border-bottom:1px solid rgba(255,255,255,.08)}
    .title{font-weight:650;font-size:15px;letter-spacing:.01em;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .title small{font-weight:400;color:rgba(244,246,251,.55);margin-left:6px}
    .icon{width:34px;height:34px;border-radius:10px;border:0;background:transparent;color:inherit;cursor:pointer;
      display:grid;place-items:center;opacity:.75;padding:0}
    .icon:hover{background:rgba(255,255,255,.1);opacity:1}
    .icon svg{width:18px;height:18px}
    .menu{position:absolute;top:56px;right:12px;background:rgba(30,34,46,.98);border:1px solid rgba(255,255,255,.12);
      border-radius:12px;padding:6px;min-width:160px;box-shadow:0 12px 32px rgba(0,0,0,.35);z-index:2}
    .menu button{display:block;width:100%;text-align:left;padding:9px 12px;border:0;border-radius:8px;background:none;
      color:inherit;font-size:14px;cursor:pointer}
    .menu button:hover{background:rgba(255,255,255,.08)}
    .menu .who{padding:8px 12px 6px;font-size:13px;color:rgba(244,246,251,.6);overflow:hidden;text-overflow:ellipsis}
    .msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:8px;overscroll-behavior:contain}
    .msg{max-width:86%;padding:9px 13px;border-radius:16px;white-space:pre-wrap;word-wrap:break-word;flex-shrink:0;font-size:15px}
    .msg.user{align-self:flex-end;background:#4f74ff;color:#fff;border-bottom-right-radius:5px}
    .msg.assistant{align-self:flex-start;background:rgba(255,255,255,.09);border-bottom-left-radius:5px}
    .msg.error{align-self:flex-start;background:rgba(255,92,92,.16);color:#ffb4b4;border-bottom-left-radius:5px}
    .dots{align-self:flex-start;display:flex;gap:4px;padding:12px 14px;background:rgba(255,255,255,.09);border-radius:16px}
    .dots i{width:6px;height:6px;border-radius:50%;background:rgba(255,255,255,.7);animation:blink 1s infinite}
    .dots i:nth-child(2){animation-delay:.15s}.dots i:nth-child(3){animation-delay:.3s}
    @keyframes blink{0%,80%,100%{opacity:.25}40%{opacity:1}}
    .composer{display:flex;align-items:flex-end;gap:8px;padding:10px;border-top:1px solid rgba(255,255,255,.08)}
    textarea,input{width:100%;border:1px solid rgba(255,255,255,.16);background:rgba(255,255,255,.06);color:#f4f6fb;
      border-radius:14px;padding:11px 13px;font-size:15px;outline:none}
    textarea{resize:none;min-height:44px;max-height:120px}
    textarea:focus,input:focus{border-color:#8fb0ff;background:rgba(255,255,255,.09)}
    ::placeholder{color:rgba(244,246,251,.45)}
    .send{flex-shrink:0;width:44px;height:44px;border-radius:14px;border:0;background:#4f74ff;color:#fff;cursor:pointer;
      display:grid;place-items:center}
    .send:disabled{opacity:.4;cursor:default}
    .send svg{width:18px;height:18px}
    .auth{flex:1;display:flex;flex-direction:column;gap:12px;padding:22px 18px;overflow-y:auto}
    .auth h2{margin:0 0 4px;font-size:20px;font-weight:650}
    .auth label{display:flex;flex-direction:column;gap:6px;font-size:13px;color:rgba(244,246,251,.7)}
    .primary{height:46px;border:0;border-radius:14px;background:#4f74ff;color:#fff;font-size:15px;font-weight:600;cursor:pointer;margin-top:4px}
    .primary:disabled{opacity:.5}
    .link{background:none;border:0;color:#9db8ff;font-size:14px;cursor:pointer;padding:4px 0;align-self:center}
    .err{color:#ffb4b4;font-size:14px;margin:0}
    .confirm{position:fixed;inset:0;background:rgba(0,0,0,.45);display:grid;place-items:center;padding:24px}
    .confirm .box{width:min(440px,100%);background:rgba(22,26,36,.97);border:1px solid rgba(255,255,255,.14);border-radius:20px;
      padding:24px;box-shadow:0 24px 60px rgba(0,0,0,.45);animation:pop .16s ease-out}
    .confirm h3{margin:0 0 20px;font-size:20px;line-height:1.3;font-weight:650}
    .row{display:flex;gap:10px}.row button{flex:1}
    .ghost{height:46px;border-radius:14px;border:1px solid rgba(255,255,255,.2);background:transparent;color:inherit;font-size:15px;cursor:pointer}
    button:focus-visible,textarea:focus-visible,input:focus-visible{outline:2px solid #8fb0ff;outline-offset:2px}
  `;

  const ICONS = {
    logo: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="3.5" width="17" height="17" rx="4"/><path d="M8 15.5V8.5h4.2a2.3 2.3 0 0 1 0 4.6H8m4.2 0 3 2.4"/></svg>',
    restore: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7"/><path d="M3 4v5h5"/></svg>',
    more: '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>',
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M6 6l12 12M18 6 6 18"/></svg>',
    send: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
  };

  const host = document.createElement('reframe-widget');
  host.setAttribute('data-reframe-widget', '');
  for (const [prop, value] of Object.entries({
    all: 'initial', position: 'fixed', top: '0', right: '0', width: '0', height: '0', 'z-index': '2147483647',
  })) host.style.setProperty(prop, value, 'important');
  const root = host.attachShadow({ mode: 'closed' });

  // Static markup only; every dynamic string below goes through textContent.
  root.innerHTML = `
    <style>${CSS}</style>
    <div class="dock">
      <button class="orb" aria-label="ReFrame" aria-expanded="false">${ICONS.logo}</button>
      <section class="panel" role="dialog" aria-label="ReFrame" hidden>
        <div class="head">
          <div class="title">ReFrame<small class="site"></small></div>
          <button class="icon restore" title="Reset page and chat" aria-label="Reset page and chat" hidden>${ICONS.restore}</button>
          <button class="icon more" title="Account" aria-label="Account" hidden>${ICONS.more}</button>
          <button class="icon close" title="Close" aria-label="Close">${ICONS.close}</button>
        </div>
        <div class="menu" hidden>
          <div class="who"></div>
          <button class="hide-orb">Hide floating button</button>
          <button class="signout">Sign out</button>
        </div>
        <form class="auth" novalidate hidden>
          <h2 class="auth-title">Sign in</h2>
          <label class="name-field" hidden>Name<input name="fullname" autocomplete="name" maxlength="80"></label>
          <label>Email<input name="email" type="email" autocomplete="email"></label>
          <label>Password<input name="password" type="password" autocomplete="current-password"></label>
          <p class="err" role="alert" hidden></p>
          <button class="primary auth-submit" type="submit">Sign in</button>
          <button class="link auth-toggle" type="button">Create account</button>
        </form>
        <div class="msgs" aria-live="polite" hidden></div>
        <form class="composer" hidden>
          <textarea rows="1" maxlength="1000" placeholder="What do you want to do?" aria-label="Message"></textarea>
          <button class="send" type="submit" aria-label="Send">${ICONS.send}</button>
        </form>
      </section>
    </div>
    <div class="confirm" hidden>
      <div class="box" role="alertdialog" aria-modal="true">
        <h3></h3>
        <div class="row"><button class="primary yes">Yes, continue</button><button class="ghost no">Cancel</button></div>
      </div>
    </div>
  `;

  const $ = (sel) => root.querySelector(sel);
  const el = {
    orb: $('.orb'), panel: $('.panel'), site: $('.site'), restore: $('.restore'), more: $('.more'), close: $('.close'),
    menu: $('.menu'), who: $('.who'), signout: $('.signout'), hideOrb: $('.hide-orb'),
    auth: $('.auth'), authTitle: $('.auth-title'), nameField: $('.name-field'), err: $('.err'),
    authSubmit: $('.auth-submit'), authToggle: $('.auth-toggle'),
    msgs: $('.msgs'), composer: $('.composer'), input: $('textarea'), send: $('.send'),
    confirm: $('.confirm'), confirmTitle: $('.confirm h3'), yes: $('.yes'), no: $('.no'),
  };

  let view = { open: false, chat: [], busy: false, user: null };
  let orbHidden = false;
  let authMode = 'signin';
  let working = false;

  const send = (type, data = {}) => RF.notify(type, data);
  const setLocal = (data) => {
    if (RF.alive()) chrome.storage.local.set(data).catch(() => {});
  };

  function renderAuthMode() {
    const register = authMode === 'register';
    el.authTitle.textContent = register ? 'Create account' : 'Sign in';
    el.authSubmit.textContent = register ? 'Create account' : 'Sign in';
    el.authToggle.textContent = register ? 'Sign in' : 'Create account';
    el.nameField.hidden = !register;
    el.auth.password.autocomplete = register ? 'new-password' : 'current-password';
  }

  function renderMessages() {
    const nodes = view.chat.map((m) => {
      const div = document.createElement('div');
      div.className = `msg ${m.role}`;
      div.textContent = m.content;
      return div;
    });
    if (view.busy || working) {
      const dots = document.createElement('div');
      dots.className = 'dots';
      dots.innerHTML = '<i></i><i></i><i></i>';
      nodes.push(dots);
    }
    el.msgs.replaceChildren(...nodes);
    el.msgs.scrollTop = el.msgs.scrollHeight;
  }

  function render() {
    const signedIn = Boolean(view.user);
    el.panel.hidden = !view.open;
    el.orb.hidden = orbHidden && !view.open;
    el.hideOrb.textContent = orbHidden ? 'Show floating button' : 'Hide floating button';
    el.orb.setAttribute('aria-expanded', String(view.open));
    el.orb.classList.toggle('working', view.busy || working);
    // The typing dots always come with the page loader: a reply means the page is about to change.
    RF.showVeil?.(signedIn && (view.busy || working));
    el.site.textContent = location.hostname;
    el.auth.hidden = signedIn;
    el.msgs.hidden = !signedIn;
    el.composer.hidden = !signedIn;
    el.restore.hidden = !signedIn;
    el.more.hidden = !signedIn;
    if (!signedIn) el.menu.hidden = true;
    el.who.textContent = view.user ? view.user.display_name || view.user.email : '';
    el.send.disabled = view.busy;
    renderAuthMode();
    if (signedIn) renderMessages();
  }

  function setOpen(open) {
    view.open = open;
    render();
    send('rf:set-open', { open });
    if (open) (view.user ? el.input : el.auth.email).focus();
  }

  el.orb.addEventListener('click', () => setOpen(!view.open));
  el.close.addEventListener('click', () => setOpen(false));
  el.more.addEventListener('click', () => {
    el.menu.hidden = !el.menu.hidden;
  });
  el.signout.addEventListener('click', () => {
    el.menu.hidden = true;
    send('rf:signout');
  });
  el.hideOrb.addEventListener('click', () => {
    el.menu.hidden = true;
    setLocal({ orbHidden: !orbHidden });
  });
  el.restore.addEventListener('click', () => {
    RF.restoreAll();
    view.chat = [];
    render();
    el.input.focus();
    send('rf:restored');
  });
  if (RF.alive()) chrome.storage.local.get('orbHidden').then(({ orbHidden: hidden }) => {
    orbHidden = Boolean(hidden);
    render();
  });
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === 'local' && 'orbHidden' in changes) {
      orbHidden = Boolean(changes.orbHidden.newValue);
      render();
    }
  });
  el.authToggle.addEventListener('click', () => {
    authMode = authMode === 'signin' ? 'register' : 'signin';
    el.err.hidden = true;
    renderAuthMode();
  });

  el.auth.addEventListener('submit', async (e) => {
    e.preventDefault();
    const email = el.auth.email.value.trim();
    const password = el.auth.password.value;
    const name = el.auth.fullname.value.trim();
    const fail = (msg) => {
      el.err.textContent = msg;
      el.err.hidden = false;
    };
    if (!email || !password) return fail('Enter your email and password.');
    if (authMode === 'register' && !name) return fail('Enter your name.');
    if (authMode === 'register' && password.length < 8) return fail('Password needs at least 8 characters.');
    el.authSubmit.disabled = true;
    el.err.hidden = true;
    const res = await send(authMode === 'register' ? 'rf:register' : 'rf:login', { email, password, display_name: name });
    el.authSubmit.disabled = false;
    if (!res || res.error) return fail(res?.error || "Can't reach ReFrame.");
    el.auth.password.value = '';
  });

  el.composer.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = el.input.value.trim();
    if (!text || view.busy) return;
    el.input.value = '';
    el.input.style.height = '';
    view.chat = [...view.chat, { role: 'user', content: text }];
    view.busy = true;
    render();
    send('rf:chat', { text });
  });

  el.input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      el.composer.requestSubmit();
    }
  });
  el.input.addEventListener('input', () => {
    el.input.style.height = '';
    el.input.style.height = `${Math.min(el.input.scrollHeight, 120)}px`;
  });

  root.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && el.confirm.hidden && view.open) setOpen(false);
  });

  // Stop the page's own key handlers from reacting to typing in the widget.
  for (const type of ['keydown', 'keyup', 'keypress']) host.addEventListener(type, (e) => e.stopPropagation());

  RF.ui = {
    setState(next) {
      view = { ...view, ...next };
      render();
    },
    setWorking(on) {
      working = on;
      render();
    },
    toggle() {
      setOpen(!view.open);
    },
    confirm(label) {
      return new Promise((resolve) => {
        el.confirmTitle.textContent = label;
        el.confirm.hidden = false;
        el.yes.focus();
        const done = (answer) => {
          el.confirm.hidden = true;
          el.yes.onclick = el.no.onclick = el.confirm.onkeydown = null;
          resolve(answer);
        };
        el.yes.onclick = () => done(true);
        el.no.onclick = () => done(false);
        el.confirm.onkeydown = (e) => {
          if (e.key === 'Escape') done(false);
        };
      });
    },
  };

  const mount = () => document.documentElement.appendChild(host);
  mount();
  // Some single-page apps replace <html> children; put the widget back if it gets removed.
  const remount = new MutationObserver(() => {
    if (!host.isConnected) mount();
  });
  remount.observe(document.documentElement, { childList: true });
  RF.teardown = () => {
    remount.disconnect();
    host.remove();
  };
  render();
})();
