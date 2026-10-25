(() => {
  const RF = (window.__reframe = window.__reframe || {});
  if (RF.voiceLoaded) return;
  RF.voiceLoaded = true;

  const CALIBRATE_MS = 300;
  const SILENCE_MS = 1300;
  const NO_SPEECH_MS = 7000;
  const MAX_MS = 20000;
  const MIN_SPEECH_MS = 250;

  let rec = null;

  const toBase64 = (blob) =>
    new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });

  // Records until the speaker goes quiet (or stop() is called); onEnd gets the audio, or why there is none.
  async function start({ onLevel, onEnd }) {
    if (rec) return;
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (err) {
      onEnd({ error: err?.name === 'NotAllowedError' ? 'blocked' : 'unavailable' });
      return;
    }
    const ctx = new AudioContext();
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    ctx.createMediaStreamSource(stream).connect(analyser);
    const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus') ? 'audio/webm;codecs=opus' : '';
    const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : {});
    const chunks = [];
    recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);

    const samples = new Float32Array(analyser.fftSize);
    const t0 = performance.now();
    let last = t0;
    let floorSum = 0;
    let floorN = 0;
    let floor = 0.01;
    let heard = 0;
    let lastVoice = t0;
    let cancelled = false;
    let timer = 0;

    rec = {
      stop(cancel = false) {
        cancelled = cancelled || cancel;
        if (recorder.state !== 'inactive') recorder.stop();
      },
    };

    recorder.onstop = async () => {
      clearInterval(timer);
      stream.getTracks().forEach((t) => t.stop());
      ctx.close().catch(() => {});
      rec = null;
      onLevel(0);
      if (cancelled || heard < MIN_SPEECH_MS) {
        onEnd({ empty: true });
        return;
      }
      const blob = new Blob(chunks, { type: recorder.mimeType || 'audio/webm' });
      onEnd({ audio: await toBase64(blob), mime: blob.type });
    };

    const tick = () => {
      if (!rec) return;
      const now = performance.now();
      analyser.getFloatTimeDomainData(samples);
      let sum = 0;
      for (const v of samples) sum += v * v;
      const rms = Math.sqrt(sum / samples.length);
      const elapsed = now - t0;
      const dt = now - last;
      last = now;
      if (elapsed < CALIBRATE_MS) {
        floorSum += rms;
        floorN += 1;
        floor = Math.min(0.03, floorSum / floorN);
      }
      const threshold = Math.max(0.012, floor * 2.2);
      if (elapsed >= CALIBRATE_MS && rms > threshold) {
        heard += dt;
        lastVoice = now;
      }
      onLevel(Math.min(1, rms / (threshold * 4)));
      const doneTalking = heard >= MIN_SPEECH_MS && now - lastVoice > SILENCE_MS;
      const nothingSaid = heard < MIN_SPEECH_MS && elapsed > NO_SPEECH_MS;
      if (doneTalking || nothingSaid || elapsed > MAX_MS) rec.stop();
    };

    recorder.start(250);
    // An interval rather than requestAnimationFrame, so silence is still detected if the tab is in the background.
    timer = setInterval(tick, 50);
  }

  RF.voice = {
    start,
    stop: (cancel = false) => rec?.stop(cancel),
    get listening() {
      return Boolean(rec);
    },
  };
})();
