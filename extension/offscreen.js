// Plays spoken replies from the extension's own page, so playback survives page navigation
// and isn't subject to each site's autoplay rules.
let ctx = null;
let current = null;

function stop() {
  if (!current) return;
  try {
    current.stop();
  } catch {}
}

async function play({ id, audio }) {
  stop();
  ctx ||= new AudioContext();
  await ctx.resume();
  const bytes = Uint8Array.from(atob(audio), (c) => c.charCodeAt(0));
  const buffer = await ctx.decodeAudioData(bytes.buffer);
  const src = ctx.createBufferSource();
  src.buffer = buffer;
  src.connect(ctx.destination);
  src.onended = () => {
    if (current === src) current = null;
    chrome.runtime.sendMessage({ type: 'rf:speech-ended', id }).catch(() => {});
  };
  current = src;
  src.start();
  return { ok: true, duration: buffer.duration };
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.target !== 'offscreen') return false;
  if (msg.type === 'play') {
    play(msg).then(sendResponse, () => sendResponse({ ok: false }));
    return true;
  }
  if (msg.type === 'stop') {
    stop();
    sendResponse({ ok: true });
  }
  return false;
});
