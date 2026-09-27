/* ==========================================================================
   Apex voice  ·  ui/apex_voice.js
   --------------------------------------------------------------------------
   The mouth, the talk key, the thinking cue, and the face (the Radial) for
   the Command Centre's voice assistant. Plain JavaScript, no build step,
   loaded before the React app. Original WijerCo code.

   What it adds to the voice loop the page already runs (mic -> /voice/ws ->
   transcript -> answer):

   MOUTH      Each reply fragment is sent to POST /voice/tts and played through
              one long-lived AudioContext, in order, while later fragments are
              still being synthesised. Kokoro runs locally; ElevenLabs is an
              option; the browser's own voice is the automatic fallback, so a
              dead engine degrades the sound instead of silencing Apex.
   INTERRUPT  Stop lands within one audio block. Anything that already calls
              speechSynthesis.cancel() (Esc, "stop", the bar button) now stops
              this audio too, and fragments from an interrupted turn are dropped.
   TALK KEY   Hold Space (when not typing) to talk; it also cuts Apex off. Two
              mic modes, switchable by voice: "push to talk mode" (mic closed
              unless the key is held) and "go hands free" (always listening).
   THINKING   A soft tone pad while the agent works, so a pause never reads as
              a dead line. Fades out the moment speech starts.
   RADIAL     Part of the dashboard, not an overlay: the Overview's orbital map
              draws it at its centre, an 80-bar mirrored starburst and a particle
              orb that bursts on every syllable of Apex's real voice. Sonar at
              idle, an intake ring while listening, a radar sweep while thinking
              or writing a document. When a conversation starts it grows in
              place, with captions and controls on the same surface. A small
              one sits in the top bar on every other page.

   Page contract (all optional; everything degrades if a hook is missing):
     ApexVoice.attach({startVoice, stopVoice, shutUp, send, isRecording,
                       onDoc, showOverview})
     ApexVoice.dock.mount(el)      voice controls and captions on a surface
     ApexVoice.core.draw(ctx,...)  the Radial at the orbital map's centre
     ApexVoice.mini.mount(canvas)  the small Radial in the top bar
     ApexVoice.docEvent(msg)       document draft events (apex_outputs.js)
     ApexVoice.onEvent(msg)        every /voice/ws message, before the page
     ApexVoice.speakText(text)     drop-in for speakText()
     ApexVoice.speechDone()        drop-in for speechDone()
     ApexVoice.micGate()           false => drop this mic frame
     ApexVoice.wakeWord(pref)      value for the handshake's wake_word
     ApexVoice.autoQuery(pref)     value for the handshake's auto_query
     ApexVoice.tapMic(stream)      lets the Radial see the microphone
   ========================================================================== */
(function () {
  "use strict";
  if (window.ApexVoice) return;

  const TAU = Math.PI * 2;
  const now = () => performance.now();
  const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
  const lerp = (a, b, t) => a + (b - a) * t;
  const hash = (i) => { const s = Math.sin(i * 127.1 + 311.7) * 43758.5453; return s - Math.floor(s); };

  /* ---------------------------------------------------------------- storage */
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, String(v)); } catch (e) { /* private mode */ } },
  };

  const settings = {
    micMode: store.get("cc_mic_mode", "open") === "ptt" ? "ptt" : "open",
    pttKey: store.get("cc_ptt_key", "Space"),
    ttsEngine: store.get("cc_tts_engine", "auto"),          // auto | kokoro | elevenlabs | browser
    ttsVoice: store.get("cc_apex_voice", ""),                // "" = the server default
    ttsSpeed: clamp(parseFloat(store.get("cc_tts_speed", "1.05")) || 1.05, 0.7, 1.5),
    thinkingCue: store.get("cc_thinking_cue", "true") !== "false",
    stageAuto: store.get("cc_stage_auto", "true") !== "false",
  };
  const LS_KEYS = {
    micMode: "cc_mic_mode", pttKey: "cc_ptt_key", ttsEngine: "cc_tts_engine", ttsVoice: "cc_apex_voice",
    ttsSpeed: "cc_tts_speed", thinkingCue: "cc_thinking_cue", stageAuto: "cc_stage_auto",
  };
  function setSetting(key, value) {
    settings[key] = value;
    store.set(LS_KEYS[key], value);
    bus.emit();
  }

  function apiBase() {
    const saved = store.get("cc_api_base", "");
    if (saved) return saved.replace(/\/$/, "");
    const preview = new Set(["5179", "5500", "5501"]);
    if (location.protocol === "file:" || preview.has(location.port)) return "http://localhost:8000";
    return location.origin;
  }

  /* -------------------------------------------------------------------- bus
     One small shared state object. The page's events and the mouth write it;
     the stage and the Radial read it. set() notifies; per-frame values
     (level, spectrum) are read directly and never notify. */
  const bus = {
    state: "off",          // off | idle | listening | hearing | thinking | speaking | error
    heard: "", partial: "", saying: "", said: "", err: "",
    engine: "", voice: "", note: "",
    firstAudioMs: null, turns: 0,
    writing: null,         // {id, title, kind, source} while a document is being drafted
    level: 0, spectrum: new Float32Array(64), source: "none",
    _subs: new Set(),
    set(patch) { Object.assign(this, patch); this.emit(); },
    emit() { for (const f of this._subs) { try { f(this); } catch (e) { /* a view must not break the bus */ } } kick(); },
    on(f) { this._subs.add(f); return () => this._subs.delete(f); },
  };

  /* ------------------------------------------------------------ page hooks */
  const page = { startVoice: null, stopVoice: null, shutUp: null, send: null, isRecording: null,
    onDoc: null, showOverview: null };
  const recording = () => { try { return !!(page.isRecording && page.isRecording()); } catch (e) { return false; } };
  const send = (obj) => { try { page.send && page.send(obj); } catch (e) { /* socket gone */ } };

  /* ----------------------------------------------------------------- audio */
  const audio = {
    ctx: null, out: null, analyser: null, fbuf: null, tbuf: null,
    micSrc: null, micAnalyser: null, micBuf: null,
    ensure() {
      if (this.ctx) return this.ctx;
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      try { this.ctx = new AC({ latencyHint: "interactive" }); } catch (e) { return null; }
      this.out = this.ctx.createGain();
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 1024;
      this.analyser.smoothingTimeConstant = 0.5;
      this.analyser.minDecibels = -88;
      this.analyser.maxDecibels = -22;
      this.out.connect(this.analyser);
      this.analyser.connect(this.ctx.destination);
      this.fbuf = new Uint8Array(this.analyser.frequencyBinCount);
      this.tbuf = new Float32Array(this.analyser.fftSize);
      return this.ctx;
    },
    running() { return !!this.ctx && this.ctx.state === "running"; },
    async resume() {
      const c = this.ensure();
      if (c && c.state === "suspended") { try { await c.resume(); } catch (e) { /* needs a gesture */ } }
      return !!c && c.state === "running";
    },
  };
  // Autoplay rules keep a context created without a gesture suspended, and the
  // first reply would then be silent. Any click or key anywhere unlocks it.
  ["pointerdown", "keydown", "touchstart"].forEach((t) =>
    window.addEventListener(t, () => { audio.resume(); }, { capture: true, passive: true }));

  function tapMic(stream) {
    const ctx = audio.ensure();
    if (!ctx || !stream) return;
    untapMic();
    try {
      audio.micSrc = ctx.createMediaStreamSource(stream);
      audio.micAnalyser = ctx.createAnalyser();
      audio.micAnalyser.fftSize = 1024;
      audio.micAnalyser.smoothingTimeConstant = 0.6;
      audio.micAnalyser.minDecibels = -90;
      audio.micAnalyser.maxDecibels = -25;
      audio.micSrc.connect(audio.micAnalyser);        // never to the speakers
      audio.micBuf = new Uint8Array(audio.micAnalyser.frequencyBinCount);
    } catch (e) { untapMic(); }
  }
  function untapMic() {
    try { audio.micSrc && audio.micSrc.disconnect(); } catch (e) { /* already gone */ }
    audio.micSrc = null; audio.micAnalyser = null; audio.micBuf = null;
  }

  /* --------------------------------------------------------- text hygiene */
  function speakable(md) {
    return String(md || "")
      .replace(/```[\s\S]*?```/g, " ")
      .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
      .replace(/https?:\/\/\S+|www\.\S+/g, "the link on screen")
      .replace(/<<[^<>]{0,80}>>/g, " ")
      .replace(/`/g, "")
      .replace(/^\s{0,3}#{1,6}\s+/gm, "")
      .replace(/^\s*(?:[-*+•]|\d+[.)])\s+/gm, "")
      .replace(/(\*\*|__)(.+?)\1/g, "$2")
      .replace(/(^|[^\w*])[*_](\S(?:.*?\S)?)[*_](?![\w*])/g, "$1$2")
      .replace(/^⚠️\s*/, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  /**
   * Cut text into pieces for speech: the first about `first` characters, each
   * next one up to `growth` times the last. Prefers a sentence end, then a
   * clause mark, then a space. Each piece carries the pause to leave before it.
   */
  function speechPieces(text, first, growth) {
    const out = [];
    let rest = text.trim(), target = first;
    while (rest.length > target * 1.35 && out.length < 6) {
      const win = rest.slice(0, Math.round(target * 1.1));
      let cut = -1, gap = 0;
      let m;
      const reSent = /[.!?](?=\s)/g, reClause = /[,;:](?=\s)/g;
      while ((m = reSent.exec(win))) if (m.index >= target * 0.5) { cut = m.index + 1; gap = 0.08; }
      if (cut < 0) while ((m = reClause.exec(win))) if (m.index >= target * 0.5) { cut = m.index + 1; gap = 0.04; }
      if (cut < 0) { const sp = win.lastIndexOf(" "); if (sp >= target * 0.6) { cut = sp; gap = 0; } }
      if (cut < 0 || rest.length - cut < 12) break;
      out.push({ text: rest.slice(0, cut).trim(), gap: out.length ? out[out.length - 1].nextGap : 0.08, nextGap: gap });
      rest = rest.slice(cut).trim();
      target = Math.round(target * growth);
    }
    out.push({ text: rest, gap: out.length ? out[out.length - 1].nextGap : 0.08 });
    return out.map((p) => ({ text: p.text, gap: p.gap }));
  }

  /* ------------------------------------------------------------------ tts */
  const SLOW_RTF = 0.85;    // slower than this and local speech falls behind playback
  const tts = {
    status: null, fetchedAt: 0, downUntil: 0, rtf: null,
    async refresh(force) {
      if (!force && this.status && now() - this.fetchedAt < 300000) return this.status;
      try {
        const r = await window.fetch(apiBase() + "/voice/tts/status", { cache: "no-store" });
        this.status = r.ok ? await r.json() : { engines: {} };
      } catch (e) { this.status = { engines: {} }; }
      this.fetchedAt = now();
      const k = this.status.engines && this.status.engines.kokoro;
      if (k && typeof k.rtf === "number") this.rtf = k.rtf;
      bus.emit();
      return this.status;
    },
    engines() { return (this.status && this.status.engines) || {}; },
    /** Which engine the next fragment should use. */
    pick() {
      const want = settings.ttsEngine;
      if (want === "browser" || now() < this.downUntil) return "browser";
      const eng = this.engines();
      if (want === "elevenlabs" && eng.elevenlabs && eng.elevenlabs.available) return "elevenlabs";
      const k = eng.kokoro;
      if (!this.status) return "kokoro";                       // unknown yet: try, a 503 corrects it
      if (k && k.available) {
        if (want === "auto" && this.rtf != null && this.rtf > SLOW_RTF) return "browser";
        return "kokoro";
      }
      return "browser";
    },
    /** One plain sentence for the settings panel. */
    describe() {
      const eng = this.engines();
      const k = eng.kokoro || {};
      const e = eng.elevenlabs || {};
      const bits = [];
      if (!this.status) bits.push("Checking the voice engines…");
      else if (!k.available) bits.push(k.installed === false ? "Local voice not installed." : (k.error || "Local voice unavailable."));
      else if (this.rtf != null) {
        const x = this.rtf.toFixed(2);
        bits.push(this.rtf > SLOW_RTF
          ? `Local voice is slow on this machine (${x}× real time), so Auto uses the browser voice.`
          : `Local voice ready, ${x}× real time.`);
      } else bits.push("Local voice ready.");
      bits.push(e.available ? "Natural voice ready." : "Natural voice off" + (e.detail ? `: ${e.detail}` : "."));
      return bits.join(" ");
    },
  };

  async function fetchSpeech(text, engine, signal) {
    const t0 = now();
    const r = await window.fetch(apiBase() + "/voice/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, engine, voice: settings.ttsVoice, speed: settings.ttsSpeed }),
      signal,
    });
    if (!r.ok) { const err = new Error("tts " + r.status); err.status = r.status; throw err; }
    const rtf = parseFloat(r.headers.get("X-TTS-RTF"));
    if (!isNaN(rtf)) tts.rtf = rtf;
    const meta = {
      engine: r.headers.get("X-TTS-Engine") || engine,
      voice: r.headers.get("X-TTS-Voice") || "",
      fallbackFrom: r.headers.get("X-TTS-Fallback-From") || "",
      fetchMs: 0,
    };
    const bytes = await r.arrayBuffer();
    meta.fetchMs = now() - t0;
    const ctx = audio.ensure();
    const buffer = await new Promise((res, rej) => {
      const p = ctx.decodeAudioData(bytes, res, rej);
      if (p && p.then) p.then(res, rej);
    });
    return { buffer, meta };
  }

  function pickBrowserVoice() {
    const ss = window.speechSynthesis;
    const voices = ss ? ss.getVoices() : [];
    if (!voices.length) return null;
    const want = store.get("cc_tts_voice", "");
    return voices.find((v) => v.name === want)
      || voices.find((v) => /en-AU/i.test(v.lang))
      || voices.find((v) => /en-GB/i.test(v.lang))
      || voices.find((v) => /^en/i.test(v.lang))
      || voices[0];
  }

  /* The browser voice cannot be heard by Web Audio, so the Radial gets a
     stand-in: word-boundary events drive an envelope that looks like speech. */
  const synth = {
    active: false, env: 0, t: 0,
    pulse(a) { this.env = Math.max(this.env, a); },
    step(dt, out) {
      this.t += dt;
      this.env *= Math.exp(-dt / 0.16);
      const base = this.active ? 0.18 + 0.1 * Math.sin(this.t * 23) : 0;
      const e = Math.max(base, this.env);
      for (let b = 0; b < out.length; b++) {
        const f = b / out.length;
        const shape = 0.55 * Math.exp(-((f - 0.12) ** 2) / 0.006) + 0.45 * Math.exp(-((f - 0.32) ** 2) / 0.012)
          + 0.25 * Math.exp(-((f - 0.55) ** 2) / 0.02);
        out[b] = clamp(e * shape * (0.75 + 0.5 * hash(b * 13 + Math.floor(this.t * 30))), 0, 1);
      }
      return e;
    },
  };

  let origCancel = null;

  /* ---------------------------------------------------------------- mouth */
  const mouth = {
    queue: [], current: null, turn: 1, waiters: [], lastEnd: 0,
    get speaking() { return !!this.current || this.queue.length > 0; },

    say(text, opts) {
      const clean = speakable(text);
      if (!clean) return Promise.resolve("empty");
      const engine = (opts && opts.engine) || tts.pick();
      // The first words decide how long the silence lasts. Local speech is
      // slower than real time only by a margin (0.55x on wijwork, 0.65 to 0.76x
      // on wijerco), so a whole opening sentence can mean 3 to 4 s of silence.
      // The opening fragment is cut into a short first piece and pieces that
      // grow by about 1/rtf, so each one is synthesised before the one ahead
      // of it finishes playing.
      if (!this.current && !this.queue.length && engine !== "browser" && clean.length > 45) {
        const growth = clamp(0.9 / (tts.rtf || 0.7), 1.2, 2.5);
        const pieces = speechPieces(clean, 38, growth);
        if (pieces.length > 1) {
          let last = null;
          pieces.forEach((pc, i) => { last = this.say(pc.text, { engine, gap: i ? pc.gap : 0.08, _piece: true }); });
          return last;
        }
      }
      const item = { text: clean, turn: this.turn, engine, ctrl: null, speechP: null, cancelled: false, stopFn: null,
        gap: opts && typeof opts.gap === "number" ? opts.gap : 0.08 };
      item.done = new Promise((res) => { item.resolve = res; });
      if (engine !== "browser" && audio.ensure()) {
        // Fetch now, play later: the next fragment synthesises while this one plays.
        item.ctrl = typeof AbortController !== "undefined" ? new AbortController() : null;
        item.speechP = fetchSpeech(clean, engine, item.ctrl && item.ctrl.signal).catch((err) => {
          if (err && err.name === "AbortError") return null;
          tts.downUntil = now() + (err && err.status === 503 ? 60000 : 15000);
          bus.set({ note: "Voice engine unavailable, using the browser voice for a minute." });
          return null;
        });
      }
      this.queue.push(item);
      this.pump();
      return item.done;
    },

    async pump() {
      if (this.current) return;
      if (!this.queue.length) { this.drained(); return; }
      const item = this.queue[0];
      this.current = item;
      let result = "done";
      try {
        const got = item.speechP ? await item.speechP : null;
        if (item.cancelled) result = "cancelled";
        else if (got && got.buffer && (await audio.resume())) result = await this.playBuffer(item, got);
        else result = await this.playBrowser(item);
      } catch (e) { result = "error"; }
      if (this.queue[0] === item) this.queue.shift();
      if (this.current === item) this.current = null;
      item.resolve(item.cancelled ? "cancelled" : result);
      this.pump();
    },

    playBuffer(item, got) {
      return new Promise((resolve) => {
        const ctx = audio.ctx;
        const src = ctx.createBufferSource();
        src.buffer = got.buffer;
        src.connect(audio.out);
        let settled = false;
        const fin = (r) => { if (!settled) { settled = true; this.lastEnd = ctx.currentTime; resolve(r); } };
        src.onended = () => fin("done");
        // A short breath between sentences; none when a sentence was cut at a
        // word to start speaking sooner, so the join is not heard.
        const gap = ctx.currentTime - this.lastEnd < 0.35 ? (item.gap || 0) : 0;
        src.start(ctx.currentTime + gap);
        item.stopFn = () => { src.onended = null; try { src.stop(); } catch (e) { /* not started */ } fin("cancelled"); };
        this.started(item, got.meta, "tts");
        // If the device stalls, onended can go missing. Never let that wedge the queue.
        setTimeout(() => fin("timeout"), (got.buffer.duration + gap + 2) * 1000);
      });
    },

    playBrowser(item) {
      return new Promise((resolve) => {
        const ss = window.speechSynthesis;
        if (!ss || !window.SpeechSynthesisUtterance) { resolve("unsupported"); return; }
        const u = new SpeechSynthesisUtterance(item.text);
        const v = pickBrowserVoice();
        if (v) { u.voice = v; u.lang = v.lang; }
        u.rate = settings.ttsSpeed;
        let settled = false;
        const fin = (r) => { if (!settled) { settled = true; synth.active = false; resolve(r); } };
        u.onend = () => fin("done");
        u.onerror = () => fin("error");
        u.onboundary = (e) => synth.pulse(e.name === "sentence" ? 0.5 : 0.95);
        u.onstart = () => synth.pulse(0.8);
        item.stopFn = () => { try { origCancel ? origCancel() : ss.cancel(); } catch (e) { /* ignore */ } fin("cancelled"); };
        synth.active = true;
        this.started(item, { engine: "browser", voice: v ? v.name : "" }, "synthetic");
        try { ss.speak(u); } catch (e) { fin("error"); }
        // Chrome sometimes never fires onend. A generous watchdog keeps the queue moving.
        setTimeout(() => fin("timeout"), 4000 + item.text.length * 120 / settings.ttsSpeed);
      });
    },

    started(item, meta, source) {
      cue.stop();
      const patch = {
        state: "speaking", saying: item.text, said: (bus.said ? bus.said + " " : "") + item.text,
        engine: meta.engine || "", voice: meta.voice || "", source,
      };
      if (turnClock.t0 && bus.firstAudioMs == null) patch.firstAudioMs = Math.round(now() - turnClock.t0);
      if (meta.fallbackFrom) patch.note = `The ${meta.fallbackFrom} voice failed, so this is the local voice.`;
      bus.set(patch);
      stage.autoOpen();
    },

    drained() {
      const waiters = this.waiters.splice(0);
      waiters.forEach((w) => w());
      if (bus.state === "speaking") {
        bus.set({ state: recording() && settings.micMode === "open" ? "listening" : "idle", saying: "", source: "none" });
        stage.armAutoClose();
      }
    },

    /** Stop now: current audio, everything queued, and any in-flight synthesis. */
    stop() {
      this.turn += 1;
      const items = this.queue.splice(0);
      const cur = this.current;
      this.current = null;
      for (const it of items.concat(cur ? [cur] : [])) {
        it.cancelled = true;
        try { it.ctrl && it.ctrl.abort(); } catch (e) { /* ignore */ }
        try { it.stopFn && it.stopFn(); } catch (e) { /* ignore */ }
        it.resolve("cancelled");
      }
      try { origCancel && origCancel(); } catch (e) { /* ignore */ }
      synth.active = false;
      this.drained();
    },

    done() {
      if (!this.speaking) return Promise.resolve();
      return new Promise((res) => this.waiters.push(res));
    },
  };

  // Every existing "stop talking" path in the page (Esc, barge-in, the bar
  // button, closing the view) calls speechSynthesis.cancel(). Routing that
  // through the mouth means all of them stop the new audio too, with no edits.
  if (window.speechSynthesis && typeof window.speechSynthesis.cancel === "function") {
    origCancel = window.speechSynthesis.cancel.bind(window.speechSynthesis);
    try { window.speechSynthesis.cancel = function () { mouth.stop(); }; } catch (e) { origCancel = null; }
  }

  /* ------------------------------------------------------ thinking cue */
  const cue = {
    nodes: null, timer: 0,
    arm() {
      clearTimeout(this.timer);
      if (!settings.thinkingCue) return;
      this.timer = setTimeout(() => this.start(), 450);   // fast answers never hear it
    },
    start() {
      const ctx = audio.ensure();
      if (!ctx || ctx.state !== "running" || this.nodes || mouth.speaking) return;
      const t = ctx.currentTime;
      const master = ctx.createGain();
      master.gain.setValueAtTime(0, t);
      master.gain.linearRampToValueAtTime(0.02, t + 0.9);
      master.connect(ctx.destination);                      // not the analyser: the face ignores it
      const lp = ctx.createBiquadFilter();
      lp.type = "lowpass"; lp.frequency.value = 850; lp.Q.value = 0.4;
      lp.connect(master);
      const trem = ctx.createGain();
      trem.gain.value = 0.7;
      trem.connect(lp);
      const lfo = ctx.createOscillator();
      lfo.frequency.value = 0.75;
      const depth = ctx.createGain();
      depth.gain.value = 0.3;
      lfo.connect(depth); depth.connect(trem.gain);
      const oscs = [lfo];
      [[196.0, 0], [293.66, 3], [392.0, -4]].forEach(([f, cents]) => {
        const o = ctx.createOscillator();
        o.type = "sine"; o.frequency.value = f; o.detune.value = cents;
        const g = ctx.createGain(); g.gain.value = f > 300 ? 0.25 : 0.5;
        o.connect(g); g.connect(trem);
        oscs.push(o);
      });
      oscs.forEach((o) => o.start(t));
      this.nodes = { master, oscs };
    },
    stop() {
      clearTimeout(this.timer);
      const n = this.nodes;
      if (!n) return;
      this.nodes = null;
      const t = audio.ctx.currentTime;
      try {
        n.master.gain.cancelScheduledValues(t);
        n.master.gain.setValueAtTime(n.master.gain.value, t);
        n.master.gain.linearRampToValueAtTime(0, t + 0.22);
        n.oscs.forEach((o) => o.stop(t + 0.3));
      } catch (e) { /* context closed */ }
    },
  };

  /* ------------------------------------------------------- turn tracking */
  const turnClock = { t0: 0 };
  let dropSpeech = false;       // after an interrupt, until the next turn starts
  let latestTurn = -1;          // server turn ids (voice.py TurnRunner)
  const cancelledTurns = new Set();

  function interrupt(reason) {
    const wasBusy = mouth.speaking || bus.state === "thinking";
    mouth.stop();
    cue.stop();
    if (wasBusy) dropSpeech = true;
    try { page.shutUp && page.shutUp(); } catch (e) { /* ignore */ }
    send({ type: "interrupt", reason: reason || "user" });
    if (wasBusy) bus.set({ state: "listening", saying: "" });
  }

  function applyConsole(v) {
    if (!v) return;
    if (v.mic_mode && v.mic_mode !== settings.micMode) {
      setSetting("micMode", v.mic_mode === "ptt" ? "ptt" : "open");
      restartVoice();
    }
    if (v.tts_engine) {
      setSetting("ttsEngine", v.tts_engine);
      tts.downUntil = 0;
      if (v.tts_engine === "elevenlabs") {
        const e = tts.engines().elevenlabs;
        if (!e || !e.available) setTimeout(() => mouth.say("The natural voice is not set up yet, so I will stay on the local voice.", { engine: "browser" }), 0);
      }
      tts.refresh(true);
    }
    if (v.tts_speed === "faster") setSetting("ttsSpeed", clamp(Math.round((settings.ttsSpeed + 0.1) * 100) / 100, 0.7, 1.5));
    if (v.tts_speed === "slower") setSetting("ttsSpeed", clamp(Math.round((settings.ttsSpeed - 0.1) * 100) / 100, 0.7, 1.5));
    if (v.stage === "open") stage.open(true);
    if (v.stage === "close") setTimeout(() => stage.close(), 1200);
  }

  function restartVoice() {
    if (!recording()) return;
    try { page.stopVoice && page.stopVoice(true); } catch (e) { /* ignore */ }
    setTimeout(() => { try { page.startVoice && page.startVoice(); } catch (e) { /* ignore */ } }, 450);
  }

  /**
   * Every /voice/ws message, called by the page before it handles it.
   * Returns false for a straggler from a cancelled turn, which the page should
   * ignore entirely.
   */
  function onEvent(m) {
    if (!m || !m.type) return true;
    if (m.type.startsWith("doc_")) { docEvent(m); return true; }
    if (typeof m.turn === "number") {
      if (m.type === "turn_cancelled") cancelledTurns.add(m.turn);
      else if (cancelledTurns.has(m.turn) || m.turn < latestTurn) return false;
      else latestTurn = Math.max(latestTurn, m.turn);
    }
    switch (m.type) {
      case "ready":
        bus.set({ state: m.asleep ? "idle" : (settings.micMode === "open" ? "listening" : "idle"), err: "" });
        break;
      case "wake": bus.set({ state: "listening" }); stage.autoOpen(); break;
      case "sleep": if (!mouth.speaking) bus.set({ state: "idle", partial: "" }); stage.armAutoClose(); break;
      case "speech_start": if (!mouth.speaking) bus.set({ state: "hearing" }); stage.autoOpen(); break;
      case "speech_end": if (bus.state === "hearing") bus.set({ state: "listening" }); break;
      case "partial": bus.set({ partial: m.text || "", state: mouth.speaking ? bus.state : "hearing" }); break;
      case "final":
        turnClock.t0 = now();
        bus.set({ heard: m.text || "", partial: "", firstAudioMs: null });
        break;
      case "thinking":
        dropSpeech = false;
        if (mouth.speaking) mouth.stop();       // a new question supersedes the old reply
        if (!turnClock.t0) turnClock.t0 = now();
        bus.set({ state: "thinking", said: "", saying: "", firstAudioMs: null, turns: bus.turns + 1, err: "", note: "" });
        cue.arm();
        stage.autoOpen();
        break;
      case "speak":
        break;                                   // the page calls speakText() for these
      case "answer":
        cue.stop();
        if (m.ui && m.ui.voice) applyConsole(m.ui.voice);
        // The page speaks a non-streamed answer inside this same handler, so let
        // that through before the drop flag (if any) is cleared.
        setTimeout(() => { dropSpeech = false; turnClock.t0 = 0; }, 0);
        if (!mouth.speaking && bus.state === "thinking") {
          setTimeout(() => { if (!mouth.speaking && bus.state === "thinking") { bus.set({ state: recording() ? "listening" : "idle" }); stage.armAutoClose(); } }, 400);
        }
        break;
      case "turn_cancelled":
        mouth.stop(); cue.stop(); break;
      case "barge_in":
        mouth.stop(); cue.stop(); dropSpeech = true; bus.set({ state: "listening", saying: "" }); break;
      case "skipped":
        if (m.reason === "busy") bus.set({ note: "Still on the last question. Hold the talk key to cut in." });
        break;
      case "error":
        cue.stop();
        bus.set({ state: "error", err: m.message || "Voice error" });
        setTimeout(() => { if (bus.state === "error") bus.set({ state: recording() ? "listening" : "idle" }); }, 4000);
        break;
      default: break;
    }
    return true;
  }

  function speakText(text) {
    if (dropSpeech) return Promise.resolve("dropped");
    return mouth.say(text);
  }

  /* ------------------------------------------------------------- talk key */
  const ptt = { held: false, source: "", downAt: 0, tailUntil: 0, flushTimer: 0 };
  const isTyping = (el) => !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));

  function pttDown(source) {
    if (ptt.held) return;
    ptt.held = true; ptt.source = source; ptt.downAt = now();
    clearTimeout(ptt.flushTimer);
    audio.resume();
    interrupt("talk-key");
    if (!recording()) { try { page.startVoice && page.startVoice(); } catch (e) { /* ignore */ } }
    else send({ type: "wake" });              // skip the wake word: holding the key is addressing Apex
    bus.set({ state: "hearing", partial: "", note: "" });
    stage.autoOpen(true);
  }
  function pttUp() {
    if (!ptt.held) return;
    ptt.held = false;
    ptt.tailUntil = now() + 320;               // keep the last syllable
    clearTimeout(ptt.flushTimer);
    ptt.flushTimer = setTimeout(() => {
      // Close the utterance now instead of waiting for the silence timer.
      send({ type: "flush" });
      if (bus.state === "hearing") bus.set({ state: settings.micMode === "open" ? "listening" : "idle" });
    }, 340);
  }
  window.addEventListener("keydown", (e) => {
    if (e.code !== settings.pttKey || e.ctrlKey || e.metaKey || e.altKey) return;
    if (isTyping(e.target) || stage.capturingKey) return;
    e.preventDefault(); e.stopPropagation();
    if (!e.repeat) pttDown("key");
  }, true);
  window.addEventListener("keyup", (e) => {
    if (e.code !== settings.pttKey || !ptt.held) return;
    e.preventDefault(); e.stopPropagation();
    pttUp();
  }, true);
  window.addEventListener("blur", () => pttUp());

  function micGate() {
    if (settings.micMode === "open") return true;
    return ptt.held || now() < ptt.tailUntil;
  }

  /* ----------------------------------------------------- per-frame audio
     Fills bus.spectrum (64 log-spaced bands, 90 Hz to 5.5 kHz, where speech
     lives) and bus.level from whichever source is live: Apex's own voice,
     the stand-in envelope for the browser voice, or the microphone. */
  const BANDS = 64, F_LO = 90, F_HI = 5500;
  const raw = new Float32Array(BANDS);
  const agc = { tts: 0.55, mic: 0.45 };

  function readBands(analyser, buf, out) {
    analyser.getByteFrequencyData(buf);
    const binHz = audio.ctx.sampleRate / analyser.fftSize;
    for (let b = 0; b < BANDS; b++) {
      const lo = F_LO * Math.pow(F_HI / F_LO, b / BANDS);
      const hi = F_LO * Math.pow(F_HI / F_LO, (b + 1) / BANDS);
      const i0 = Math.max(1, Math.floor(lo / binHz));
      const i1 = Math.max(i0 + 1, Math.ceil(hi / binHz));
      let m = 0;
      for (let i = i0; i < i1 && i < buf.length; i++) if (buf[i] > m) m = buf[i];
      out[b] = m / 255;
    }
  }

  function sampleAudio(dt) {
    let src = "none";
    const cur = mouth.current;
    if (cur && bus.source === "tts" && audio.analyser && audio.running()) {
      readBands(audio.analyser, audio.fbuf, raw);
      src = "tts";
    } else if (synth.active || synth.env > 0.02) {
      synth.step(dt, raw);
      src = "synthetic";
    } else if (demo.state) {
      demo.step(dt, raw);
      src = "demo";
    } else if (audio.micAnalyser && audio.running() && micGate() && (bus.state === "hearing" || bus.state === "listening")) {
      readBands(audio.micAnalyser, audio.micBuf, raw);
      src = "mic";
    } else {
      raw.fill(0);
    }
    // Byte spectra sit on a noise floor; lift it off and normalise with a slow
    // automatic gain so quiet and loud voices both fill the starburst.
    let peak = 0, sum = 0;
    const floor = src === "mic" ? 0.32 : src === "tts" ? 0.2 : 0;
    for (let b = 0; b < BANDS; b++) {
      const v = clamp((raw[b] - floor) / (1 - floor), 0, 1);
      raw[b] = v; if (v > peak) peak = v; sum += v;
    }
    const key = src === "mic" ? "mic" : "tts";
    agc[key] = peak > agc[key] ? lerp(agc[key], peak, 0.3) : Math.max(0.25, agc[key] * Math.pow(0.6, dt));
    const gain = src === "synthetic" || src === "demo" ? 1 : 0.9 / agc[key];
    const s = bus.spectrum;
    const rel = Math.pow(0.02, dt);      // release: fall to 2% in one second
    for (let b = 0; b < BANDS; b++) {
      const v = clamp(raw[b] * gain, 0, 1);
      s[b] = v > s[b] ? lerp(s[b], v, 0.6) : s[b] * rel + v * (1 - rel);
    }
    const lvl = clamp((sum / BANDS) * gain * 2.2, 0, 1);
    bus.level = lvl > bus.level ? lerp(bus.level, lvl, 0.55) : lerp(bus.level, lvl, 1 - Math.pow(0.05, dt));
    bus.live = src;
  }

  // The orbital core on the overview listens to a shared APX_VOICE object.
  // While Apex talks, feed it Apex's real voice so the core pulses with it.
  let fedOrbital = false;
  function feedOrbital() {
    let v = null;
    try { v = (typeof APX_VOICE !== "undefined") ? APX_VOICE : null; } catch (e) { v = null; }
    if (!v || !v.wave) return;
    const talking = bus.live === "tts" || bus.live === "synthetic";
    if (talking) {
      const n = v.wave.length;
      for (let i = 0; i < n; i++) v.wave[i] = bus.spectrum[Math.floor((i / n) * 48)];
      v.level = bus.level; v.active = true; fedOrbital = true;
    } else if (fedOrbital) {
      v.active = false; v.level = 0; v.wave.fill(0); fedOrbital = false;
    }
  }

  /* ---------------------------------------------------------------- demo
     Scripted states for previews and screenshots: ?apexdemo=speaking */
  const demo = {
    state: "", t: 0,
    step(dt, out) {
      this.t += dt;
      const syll = Math.max(0, Math.sin(this.t * TAU * 2.3)) ** 2 * (0.6 + 0.4 * Math.sin(this.t * 1.7));
      const e = this.state === "speaking" ? 0.25 + 0.75 * syll : this.state === "hearing" ? 0.25 + 0.35 * syll : 0;
      synth.t = this.t;
      const f1 = 0.1 + 0.05 * Math.sin(this.t * 3.1), f2 = 0.32 + 0.1 * Math.sin(this.t * 2.3 + 1), f3 = 0.58 + 0.06 * Math.sin(this.t * 1.7);
      for (let b = 0; b < out.length; b++) {
        const f = b / out.length;
        const shape = 0.95 * Math.exp(-((f - f1) ** 2) / 0.004) + 0.7 * Math.exp(-((f - f2) ** 2) / 0.006)
          + 0.45 * Math.exp(-((f - f3) ** 2) / 0.01) + 0.05;
        out[b] = clamp(e * shape * (0.65 + 0.7 * hash(b * 7 + Math.floor(this.t * 24))), 0, 1);
      }
    },
  };

  /* ============================================================== RADIAL
     An original design in the Apex palette.
       orb        2,600 grains on a Fibonacci sphere; a spring per grain, so
                  every syllable onset kicks the interior outward and it
                  rebounds (interior grains fly furthest, the crust holds)
       starburst  80 tapered bars, mirrored on both axes, lows at the sides
       voiceprint a smooth closed curve through the bar tips
       galaxy     a banded nebula and a starfield turning slowly on the centre
       idle       sonar rings and three orbiting satellites (the units)
       listening  an intake ring that breathes with the microphone
       thinking   a radar sweep, a tick ring and counter-rotating arcs
     ===================================================================== */
  const PAL = {
    void: [3, 7, 17], deep: [10, 22, 48],
    gold: [244, 178, 63], goldSoft: [242, 207, 130], amber: [245, 160, 94], coral: [255, 107, 114],
    teal: [52, 224, 200], blue: [91, 157, 249], ice: [210, 240, 255], violet: [128, 102, 230],
  };
  const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;
  const mix = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t].map((x) => x | 0);

  class Radial {
    constructor(opts) {
      // Drawn into a host canvas in CSS pixels: the overview's orbital map, or
      // the small one in the top bar. `grains` sets the orb's density.
      this.embedded = true;
      this.grains = (opts && opts.grains) || 1300;
      this.dpr = 1;
      this.N = 80;
      this.bar = new Float32Array(this.N); this.peak = new Float32Array(this.N); this.prevBar = new Float32Array(this.N);
      this.sparks = []; this.rings = []; this.ringClock = 0.6; this.intake = 0;
      this.w8 = { idle: 1, listen: 0, think: 0, speak: 0, error: 0 };
      this.spin = 0; this.sweep = -Math.PI / 2; this.t = 0;
      this.onset = { fast: 0, slow: 0, last: -1 };
      this.reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      this.buildOrb();
    }

    buildOrb() {
      const n = this.reduced ? Math.min(this.grains, 900) : this.grains;
      this.n = n;
      this.px = new Float32Array(n); this.py = new Float32Array(n); this.pz = new Float32Array(n);
      this.r0 = new Float32Array(n); this.d = new Float32Array(n); this.v = new Float32Array(n);
      this.tw = new Float32Array(n); this.kw = new Float32Array(n); this.core = new Uint8Array(n);
      const ga = Math.PI * (3 - Math.sqrt(5));
      for (let i = 0; i < n; i++) {
        const y = 1 - ((i + 0.5) * 2) / n, rr = Math.sqrt(1 - y * y), th = i * ga;
        this.px[i] = Math.cos(th) * rr; this.py[i] = y; this.pz[i] = Math.sin(th) * rr;
        const h = hash(i + 1);
        const interior = i % 3 === 0;
        this.core[i] = interior ? 1 : 0;
        this.r0[i] = interior ? 0.18 + 0.64 * Math.cbrt(h) : 0.9 + 0.1 * h;
        this.tw[i] = hash(i * 7.13) * TAU;
        this.kw[i] = (1.3 - this.r0[i]) * (0.7 + 0.6 * hash(i * 3.7));
      }
      this.paths = [];
    }

    step(dt) {
      const st = bus.state;
      const writing = !!bus.writing && st !== "speaking" && st !== "hearing";
      const target = {
        idle: (st === "idle" || st === "off") && !writing ? 1 : 0,
        listen: (st === "listening" || st === "hearing") && !writing ? 1 : 0,
        think: st === "thinking" || writing ? 1 : 0,
        speak: st === "speaking" ? 1 : 0,
        error: st === "error" ? 1 : 0,
      };
      const k = 1 - Math.pow(0.02, dt);
      for (const key in this.w8) this.w8[key] = lerp(this.w8[key], target[key], k);
      this.t += dt;
      const calm = this.reduced ? 0.35 : 1;
      this.spin += dt * (0.18 + 0.9 * bus.level) * calm;
      this.sweep += dt * TAU / 2.4 * calm;

      // Syllable onsets: a fast follower pulling ahead of a slow one.
      const L = bus.level;
      this.onset.fast = lerp(this.onset.fast, L, 1 - Math.pow(0.001, dt));
      this.onset.slow = lerp(this.onset.slow, L, 1 - Math.pow(0.35, dt));
      let kick = 0;
      const rise = this.onset.fast - this.onset.slow;
      if (rise > 0.07 && this.t - this.onset.last > 0.085) { kick = Math.min(1.5, rise * 7); this.onset.last = this.t; }

      // Orb springs.
      const K = 42, C = 7.2, pressure = 0.22 * L;
      const sub = dt > 1 / 45 ? 2 : 1, h = dt / sub;
      for (let s = 0; s < sub; s++) {
        for (let i = 0; i < this.n; i++) {
          const w = this.kw[i];
          if (kick && s === 0) this.v[i] += kick * w * 3.1;
          const a = K * (pressure * w - this.d[i]) - C * this.v[i];
          this.v[i] += a * h;
          this.d[i] += this.v[i] * h;
        }
      }

      // Bars: mirrored across both axes, lows at 3 and 9 o'clock, highs at the
      // crown and the base. Speech carries most energy low, so the burst flares
      // sideways, which suits a wide screen and leaves room for the captions.
      const half = this.N / 2;
      const att = 1 - Math.pow(0.0005, dt), rel = Math.pow(0.12, dt);
      for (let i = 0; i < this.N; i++) {
        const k = i < half ? i : this.N - 1 - i;             // 0 top .. 39 bottom
        const q = half / 2;
        const pos = Math.abs(k + 0.5 - q) / q;                 // 0 at the sides, 1 top and bottom
        const bf = pos * 46, b0 = Math.floor(bf), fr = bf - b0;
        let v = bus.spectrum[b0] * (1 - fr) + bus.spectrum[Math.min(63, b0 + 1)] * fr;
        v *= 0.78 + pos * 0.55;                 // lift the thin top end
        v = clamp(Math.pow(clamp(v, 0, 1), 1.35) * 1.08, 0, 1);   // contrast: formants stand out
        this.prevBar[i] = this.bar[i];
        this.bar[i] = v > this.bar[i] ? lerp(this.bar[i], v, att) : this.bar[i] * rel + v * (1 - rel);
        this.peak[i] = this.bar[i] > this.peak[i] ? this.bar[i] : Math.max(0, this.peak[i] - dt * 0.55);
      }

      // Sonar rings at idle.
      this.ringClock -= dt;
      if (this.ringClock <= 0) {
        this.ringClock = 2.8;
        if (this.w8.idle > 0.4 || this.w8.error > 0.4) this.rings.push({ r: 0, a: 1, err: this.w8.error > 0.4 });
      }
      for (const r of this.rings) { r.r += dt * 0.34; r.a -= dt * 0.36; }
      this.rings = this.rings.filter((r) => r.a > 0);

      // Sparks.
      for (const p of this.sparks) {
        p.x += p.vx * dt; p.y += p.vy * dt;
        p.vx *= Math.pow(0.18, dt); p.vy *= Math.pow(0.18, dt);
        p.life -= dt;
      }
      this.sparks = this.sparks.filter((p) => p.life > 0);
      this.intake = (this.intake + dt * 0.55) % 1;
    }

    /** The Radial inside another canvas: the overview's orbital map calls this
     *  for its centre. Units are CSS pixels (that canvas is already scaled), and
     *  the starburst is shorter so it clears the department nodes. */
    drawEmbedded(g, cx, cy, R, dt) {
      this.dpr = 1;
      this.step(dt);
      g.save();
      g.globalAlpha = 1;
      this.scene(g, cx, cy, R, R * 1.3, R * 1.2);
      g.restore();
    }

    /** Everything in front of the galaxy: halo, state layers, bars, orb. */
    scene(g, cx, cy, R, base, len) {
      const W = this.w8, L = bus.level, dpr = this.dpr;

      // Halo behind the orb, tinted by state.
      const haloCol = W.think > 0.5 ? PAL.gold : W.speak > 0.3 ? PAL.gold : W.listen > 0.3 ? PAL.teal : W.error > 0.3 ? PAL.coral : PAL.blue;
      const breath = 0.5 + 0.5 * Math.sin(this.t * TAU / 6);
      const haloA = 0.12 + 0.08 * breath + 0.35 * L;
      const hg = g.createRadialGradient(cx, cy, R * 0.4, cx, cy, R * (2.3 + 0.6 * L));
      hg.addColorStop(0, rgba(haloCol, haloA)); hg.addColorStop(1, rgba(haloCol, 0));
      g.fillStyle = hg; g.beginPath(); g.arc(cx, cy, R * (2.3 + 0.6 * L), 0, TAU); g.fill();

      g.globalCompositeOperation = "lighter";
      if (W.idle > 0.02 || W.error > 0.02) this.drawIdle(g, cx, cy, R, base, len, W);
      if (W.listen > 0.02) this.drawListen(g, cx, cy, R, base, len, W.listen);
      if (W.think > 0.02) this.drawThink(g, cx, cy, R, base, len, W.think);
      this.drawBars(g, cx, cy, base, len);
      this.drawSparks(g);
      this.drawOrb(g, cx, cy, R, L);
      g.globalCompositeOperation = "source-over";
      this.drawFrame(g, cx, cy, R, dpr);
    }

    drawIdle(g, cx, cy, R, base, len, W) {
      const a = Math.max(W.idle, W.error);
      for (const r of this.rings) {
        const rad = R * 1.1 + r.r * (base + len - R);
        g.strokeStyle = rgba(r.err ? PAL.coral : PAL.blue, 0.55 * r.a * a);
        g.lineWidth = 1.4 * this.dpr;
        g.beginPath(); g.arc(cx, cy, rad, 0, TAU); g.stroke();
      }
      // Three satellites on tilted orbits: the specialist units, waiting.
      // Embedded on the overview, the real department nodes play that part.
      for (let k = 0; k < (this.embedded ? 0 : 3); k++) {
        const ang = this.t * (0.32 + k * 0.11) + k * 2.1;
        const tilt = 0.35 + k * 0.22, rx = base + len * (0.35 + k * 0.22), ry = rx * tilt;
        const rot = k * 1.05 - 0.4;
        g.strokeStyle = rgba(PAL.blue, 0.07 * W.idle);
        g.lineWidth = this.dpr;
        g.beginPath(); g.ellipse(cx, cy, rx, ry, rot, 0, TAU); g.stroke();
        for (let s = 0; s < 14; s++) {
          const aa = ang - s * 0.045;
          const ex = Math.cos(aa) * rx, ey = Math.sin(aa) * ry;
          const x = cx + ex * Math.cos(rot) - ey * Math.sin(rot), y = cy + ex * Math.sin(rot) + ey * Math.cos(rot);
          g.fillStyle = rgba(k === 1 ? PAL.teal : PAL.blue, (s ? 0.35 * (1 - s / 14) : 0.95) * W.idle);
          const size = (s ? 1.6 : 3.2) * this.dpr;
          g.beginPath(); g.arc(x, y, size, 0, TAU); g.fill();
        }
      }
      if (W.error > 0.02) {
        const p = 0.6 + 0.4 * Math.sin(this.t * 5);
        g.strokeStyle = rgba(PAL.coral, 0.7 * p * W.error);
        g.lineWidth = 3 * this.dpr;
        g.beginPath(); g.arc(cx, cy, R * 1.25, 0, TAU); g.stroke();
      }
    }

    drawListen(g, cx, cy, R, base, len, a) {
      // An intake: dots drift inward toward the orb, faster as the mic hears more.
      const L = bus.live === "mic" ? bus.level : 0;
      const n = 48;
      for (let i = 0; i < n; i++) {
        const ph = (this.intake + hash(i * 3.1)) % 1;
        const rad = base + len * 0.9 - ph * (base + len * 0.9 - R * 1.15);
        const ang = (i / n) * TAU + this.t * 0.05;
        g.fillStyle = rgba(PAL.teal, 0.55 * a * Math.sin(ph * Math.PI));
        const s = (1.3 + 1.2 * ph) * this.dpr;
        g.fillRect(cx + Math.cos(ang) * rad - s / 2, cy + Math.sin(ang) * rad - s / 2, s, s);
      }
      const rr = R * (1.16 + 0.18 * L) + Math.sin(this.t * 3) * R * 0.02;
      g.strokeStyle = rgba(PAL.teal, (0.35 + 0.5 * L) * a);
      g.lineWidth = (1.5 + 3 * L) * this.dpr;
      g.beginPath(); g.arc(cx, cy, rr, 0, TAU); g.stroke();
      g.strokeStyle = rgba(PAL.teal, 0.12 * a);
      g.lineWidth = 10 * this.dpr;
      g.beginPath(); g.arc(cx, cy, rr, 0, TAU); g.stroke();
    }

    drawThink(g, cx, cy, R, base, len, a) {
      const inner = R * 1.2, outer = base + len * 0.75;
      // Radar: a wedge of fading spokes trailing the sweep head.
      const spokes = 30;
      for (let s = 0; s < spokes; s++) {
        const ang = this.sweep - s * 0.035;
        const al = Math.pow(1 - s / spokes, 2.2) * 0.42 * a;
        g.strokeStyle = rgba(PAL.gold, al);
        g.lineWidth = (s ? 2 : 2.6) * this.dpr;
        g.beginPath();
        g.moveTo(cx + Math.cos(ang) * inner, cy + Math.sin(ang) * inner);
        g.lineTo(cx + Math.cos(ang) * outer, cy + Math.sin(ang) * outer);
        g.stroke();
      }
      // Tick ring that lights up as the sweep passes.
      const ticks = 72, tr = R * 1.42;
      for (let i = 0; i < ticks; i++) {
        const ang = (i / ticks) * TAU;
        let diff = ((this.sweep - ang) % TAU + TAU) % TAU;
        const lit = Math.max(0, 1 - diff / 1.4);
        const l = (i % 6 === 0 ? 0.16 : 0.08) * R;
        g.strokeStyle = rgba(lit > 0.02 ? PAL.goldSoft : PAL.gold, (0.12 + 0.75 * lit) * a);
        g.lineWidth = 1.3 * this.dpr;
        g.beginPath();
        g.moveTo(cx + Math.cos(ang) * tr, cy + Math.sin(ang) * tr);
        g.lineTo(cx + Math.cos(ang) * (tr + l), cy + Math.sin(ang) * (tr + l));
        g.stroke();
      }
      // Counter-rotating dashed arcs.
      const arcs = [[1.62, 0.9, 1.9, PAL.gold], [1.78, -0.6, 1.2, PAL.amber], [1.95, 0.35, 2.4, PAL.goldSoft]];
      g.lineWidth = 1.6 * this.dpr;
      arcs.forEach(([f, spd, span, col], i) => {
        const start = this.t * spd + i * 2.2;
        g.setLineDash([6 * this.dpr, 7 * this.dpr]);
        g.strokeStyle = rgba(col, 0.55 * a);
        g.beginPath(); g.arc(cx, cy, R * f, start, start + span); g.stroke();
      });
      g.setLineDash([]);
    }

    drawBars(g, cx, cy, base, len) {
      const N = this.N, dpr = this.dpr;
      const step = TAU / N;
      const tips = [];
      const any = bus.level > 0.01 || this.bar.some((v) => v > 0.02);
      const youTalking = bus.live === "mic" || (bus.live === "demo" && bus.state === "hearing");
      for (let i = 0; i < N; i++) {
        const ang = -Math.PI / 2 + (i + 0.5) * step;
        const v = this.bar[i];
        const l = 4 * dpr + v * len;
        const ca = Math.cos(ang), sa = Math.sin(ang);
        tips.push([cx + ca * (base + l * 0.93), cy + sa * (base + l * 0.93)]);
        if (v < 0.015) continue;
        // Apex's voice burns gold to coral; yours (the mic) runs teal to ice.
        const col = youTalking
          ? mix(PAL.teal, PAL.ice, v)
          : v < 0.5 ? mix(PAL.gold, PAL.amber, v * 2) : mix(PAL.amber, PAL.coral, (v - 0.5) * 2);
        const w0 = (TAU * base / N) * 0.46, w1 = w0 * 0.28;
        const px = -sa, py = ca;
        const x0 = cx + ca * base, y0 = cy + sa * base, x1 = cx + ca * (base + l), y1 = cy + sa * (base + l);
        for (const [wide, al] of [[3.2, 0.1], [1, 0.9]]) {
          g.fillStyle = rgba(col, al);
          g.beginPath();
          g.moveTo(x0 + px * w0 * wide / 2, y0 + py * w0 * wide / 2);
          g.lineTo(x1 + px * w1 * wide / 2, y1 + py * w1 * wide / 2);
          g.lineTo(x1 - px * w1 * wide / 2, y1 - py * w1 * wide / 2);
          g.lineTo(x0 - px * w0 * wide / 2, y0 - py * w0 * wide / 2);
          g.closePath(); g.fill();
        }
        const pk = base + 4 * dpr + this.peak[i] * len;
        if (this.peak[i] > v + 0.04) {
          g.fillStyle = rgba(youTalking ? PAL.ice : PAL.goldSoft, 0.85);
          g.beginPath(); g.arc(cx + ca * pk, cy + sa * pk, 1.6 * dpr, 0, TAU); g.fill();
        }
        if (v > 0.5 && v - this.prevBar[i] > 0.06 && this.sparks.length < 240) {
          for (let k = 0; k < 2; k++) {
            const spd = (120 + 260 * Math.random()) * dpr, jit = (Math.random() - 0.5) * 0.5;
            this.sparks.push({ x: x1, y: y1, vx: Math.cos(ang + jit) * spd, vy: Math.sin(ang + jit) * spd, life: 0.5 + 0.5 * Math.random(), max: 1, col });
          }
        }
      }
      if (!any) return;
      // Voiceprint: a smooth closed curve through the tips.
      g.beginPath();
      for (let i = 0; i <= N; i++) {
        const a = tips[i % N], b = tips[(i + 1) % N];
        const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
        if (i === 0) g.moveTo(mx, my); else g.quadraticCurveTo(a[0], a[1], mx, my);
      }
      g.closePath();
      g.fillStyle = rgba(youTalking ? PAL.teal : PAL.gold, 0.035 + 0.05 * bus.level);
      g.fill();
      g.strokeStyle = rgba(youTalking ? PAL.ice : PAL.goldSoft, 0.18 + 0.35 * bus.level);
      g.lineWidth = 1.2 * dpr;
      g.stroke();
    }

    drawSparks(g) {
      for (const p of this.sparks) {
        const k = clamp(p.life / p.max, 0, 1);
        g.fillStyle = rgba(p.col, 0.9 * k);
        const s = (1 + 2.2 * k) * this.dpr;
        g.fillRect(p.x - s / 2, p.y - s / 2, s, s);
      }
    }

    drawOrb(g, cx, cy, R, L) {
      const n = this.n, dpr = this.dpr;
      const yaw = this.spin, pitch = 0.32 + 0.12 * Math.sin(this.t * 0.21);
      const cyw = Math.cos(yaw), syw = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
      const B = 5;
      if (!this.paths.length) for (let k = 0; k < 2 * B; k++) this.paths.push(null);
      for (let k = 0; k < 2 * B; k++) this.paths[k] = new Path2D();
      let heat = 0;
      for (let i = 0; i < n; i++) heat = Math.max(heat, this.d[i]);
      heat = clamp(heat * 2.2, 0, 1);
      // A dark disc so the grains read against the starburst behind them.
      g.globalCompositeOperation = "source-over";
      const disc = g.createRadialGradient(cx, cy, 0, cx, cy, R * 1.05);
      disc.addColorStop(0, "rgba(4,10,24,0.92)"); disc.addColorStop(1, "rgba(4,10,24,0)");
      g.fillStyle = disc; g.beginPath(); g.arc(cx, cy, R * 1.05, 0, TAU); g.fill();
      g.globalCompositeOperation = "lighter";
      for (let i = 0; i < n; i++) {
        const x = this.px[i], y = this.py[i], z = this.pz[i];
        const x1 = x * cyw + z * syw, z1 = -x * syw + z * cyw;
        const y2 = y * cp - z1 * sp, z2 = y * sp + z1 * cp;
        const r = Math.min(1.7, this.r0[i] + this.d[i]);
        const depth = (z2 + 1) / 2;
        const persp = 0.86 + 0.28 * depth;
        const sx = cx + x1 * R * r * persp, sy = cy + y2 * R * r * persp;
        const tw = 0.72 + 0.28 * Math.sin(this.t * 1.3 + this.tw[i]);
        const bright = clamp(depth * tw * (0.55 + 0.6 * L) + this.d[i] * 1.4, 0, 0.999);
        const bucket = (this.core[i] ? B : 0) + Math.floor(bright * B);
        const s = (0.8 + 1.5 * depth) * dpr;
        this.paths[bucket].rect(sx - s / 2, sy - s / 2, s, s);
      }
      for (let k = 0; k < 2 * B; k++) {
        const isCore = k >= B, lvl = (k % B + 0.5) / B;
        const col = isCore ? mix(mix(PAL.blue, PAL.gold, clamp(L * 1.4, 0, 1)), [255, 246, 226], heat * lvl)
          : mix(PAL.teal, PAL.ice, lvl * 0.7);
        g.fillStyle = rgba(col, 0.18 + 0.8 * lvl);
        g.fill(this.paths[k]);
      }
    }

    drawFrame(g, cx, cy, R, dpr) {
      // Fine instrument rings around the orb, and four live readouts.
      g.strokeStyle = rgba(PAL.blue, 0.28);
      g.lineWidth = dpr;
      g.beginPath(); g.arc(cx, cy, R * 1.1, 0, TAU); g.stroke();
      g.strokeStyle = rgba(PAL.gold, 0.22);
      for (let i = 0; i < 48; i++) {
        const a = (i / 48) * TAU + this.spin * 0.08;
        const r1 = R * 1.1, r2 = R * (i % 4 === 0 ? 1.16 : 1.13);
        g.beginPath();
        g.moveTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1);
        g.lineTo(cx + Math.cos(a) * r2, cy + Math.sin(a) * r2);
        g.stroke();
      }
      // Live readouts (state, engine, first-word latency) live in the DOM header,
      // where they stay legible at any size and never collide with the bars.
    }
  }

  function keyName(code) {
    if (!code) return "KEY";
    return String(code).replace(/^Key/, "").replace(/^Digit/, "").replace(/Left$|Right$/, "").toUpperCase();
  }

  /* ================================================================ DOCK
     The Radial is part of the dashboard, not an overlay. The Overview's
     orbital map draws it at its centre (see `core`), and this dock puts the
     voice controls and captions on the same surface: state and engine at the
     top right, what you said and what Apex is saying under the orb, and a
     hold-to-talk button while a conversation is on.

     "Engaged" is the old stage's open state: a conversation starting makes
     the centre grow in place and the KPI strip fold away; seven quiet seconds
     after the last reply it settles back. Clicking the orb pins it open until
     Done or Esc. On other pages the small Radial in the top bar carries the
     same state (see `mini`). */
  const CSS = `
  .apxv-dock{position:absolute;inset:0;pointer-events:none;z-index:6;color:#e4ecf7;font-family:'IBM Plex Sans',system-ui,sans-serif;container-type:inline-size}
  .apxv-dock > *{pointer-events:auto}
  .apxv-top{position:absolute;top:14px;right:14px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end;
    max-width:calc(100% - 28px);font:500 10px 'IBM Plex Mono',ui-monospace,monospace;letter-spacing:.14em;text-transform:uppercase;color:#8fa0bd}
  .apxv-chip{padding:4px 9px;border:1px solid rgba(91,157,249,.3);border-radius:999px;color:#e4ecf7;white-space:nowrap}
  .apxv-chip[data-s=speaking],.apxv-chip[data-s=thinking],.apxv-chip[data-s=writing]{border-color:rgba(244,178,63,.6);color:#f4b23f}
  .apxv-chip[data-s=listening],.apxv-chip[data-s=hearing]{border-color:rgba(52,224,200,.55);color:#34e0c8}
  .apxv-chip[data-s=error]{border-color:rgba(255,107,114,.6);color:#ff6b72}
  .apxv-meta{color:#5d6f92;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:220px}
  .apxv-btn{background:rgba(11,20,36,.72);border:1px solid rgba(91,157,249,.24);color:#e4ecf7;border-radius:8px;
    padding:5px 9px;font:500 10px 'IBM Plex Mono',ui-monospace,monospace;letter-spacing:.1em;text-transform:uppercase;cursor:pointer}
  .apxv-btn:hover{border-color:rgba(244,178,63,.55)}
  .apxv-btn[aria-pressed=true]{border-color:rgba(52,224,200,.6);color:#34e0c8}
  .apxv-btn.done{display:none}
  .apxv-dock.engaged .apxv-btn.done{display:inline-block}
  .apxv-cap{position:absolute;left:50%;bottom:62px;transform:translateX(-50%);width:min(720px,calc(100% - 32px));
    text-align:center;pointer-events:none;opacity:0;transition:opacity .3s ease}
  .apxv-dock.engaged .apxv-cap,.apxv-dock.speaking .apxv-cap{opacity:1}
  .apxv-you{color:#8fa0bd;font-size:13px;line-height:1.45;margin-bottom:8px;min-height:1.45em}
  .apxv-you.partial{font-style:italic;opacity:.8}
  .apxv-you b,.apxv-say b{font:500 9.5px 'IBM Plex Mono',monospace;letter-spacing:.16em;text-transform:uppercase;margin-right:8px}
  .apxv-you b{color:#34e0c8}.apxv-say b{color:#f4b23f}
  .apxv-say{font-size:16.5px;line-height:1.5;min-height:1.5em;text-shadow:0 1px 14px rgba(3,7,17,.95)}
  .apxv-note{margin-top:6px;font:400 12px 'IBM Plex Sans',sans-serif;color:#f5a05e;min-height:1.2em}
  .apxv-hold{position:absolute;left:50%;bottom:14px;transform:translateX(-50%);min-width:170px;padding:9px 20px;
    border-radius:999px;border:1px solid rgba(52,224,200,.45);background:rgba(11,20,36,.82);color:#e4ecf7;cursor:pointer;
    font:500 10.5px 'IBM Plex Mono',monospace;letter-spacing:.16em;text-transform:uppercase;touch-action:none;user-select:none;
    opacity:0;visibility:hidden;transition:opacity .3s ease}
  .apxv-dock.engaged .apxv-hold{opacity:1;visibility:visible}
  .apxv-hold.on{background:rgba(52,224,200,.2);border-color:#34e0c8;box-shadow:0 0 0 6px rgba(52,224,200,.12)}
  .apxv-panel{position:absolute;top:48px;right:14px;width:min(320px,calc(100% - 28px));display:none;
    background:rgba(7,14,28,.97);border:1px solid rgba(91,157,249,.26);border-radius:12px;padding:14px 14px 10px;
    font:400 12.5px 'IBM Plex Sans',sans-serif;color:#e4ecf7;box-shadow:0 18px 50px rgba(0,0,0,.5)}
  .apxv-panel.open{display:block}
  .apxv-row{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0 0 10px}
  .apxv-row label{color:#8fa0bd}
  .apxv-panel select,.apxv-panel input[type=range]{background:#0b1424;color:#e4ecf7;border:1px solid rgba(91,157,249,.26);border-radius:6px;padding:4px 6px;max-width:170px}
  .apxv-status{color:#8fa0bd;font-size:11.5px;line-height:1.5;border-top:1px solid rgba(91,157,249,.14);padding-top:9px;margin-top:4px}
  .apxv-hint{position:absolute;left:14px;bottom:16px;font:400 10px 'IBM Plex Mono',monospace;color:#46587a;letter-spacing:.08em;pointer-events:none}
  @container (max-width:880px){.apxv-meta,.apxv-hint{display:none}.apxv-top{max-width:calc(50% - 56px)}.apxv-say{font-size:15px}}
  @media (max-width:760px){.apxv-meta,.apxv-hint{display:none}.apxv-say{font-size:15px}}
  `;

  const stage = {
    el: null, host: null, visible: false, pinned: false, closeTimer: 0, capturingKey: false, dom: {},

    build() {
      if (this.el) return;
      const style = document.createElement("style");
      style.textContent = CSS;
      document.head.appendChild(style);
      const el = document.createElement("div");
      el.className = "apxv-dock";
      el.setAttribute("aria-label", "Apex voice");
      el.innerHTML = `
        <div class="apxv-top">
          <span class="apxv-meta"></span>
          <span class="apxv-chip" data-s="idle">standing by</span>
          <button class="apxv-btn" data-act="mode" title="Switch microphone mode">Hands-free</button>
          <button class="apxv-btn" data-act="settings" title="Voice settings">Voice</button>
          <button class="apxv-btn done" data-act="close" title="Settle back (Esc)">Done</button>
        </div>
        <div class="apxv-panel" aria-label="Voice settings">
          <div class="apxv-row"><label>Microphone</label>
            <select data-set="micMode"><option value="open">Hands-free</option><option value="ptt">Push to talk</option></select></div>
          <div class="apxv-row"><label>Talk key</label><button class="apxv-btn" data-act="key">Space</button></div>
          <div class="apxv-row"><label>Voice engine</label>
            <select data-set="ttsEngine"><option value="auto">Auto</option><option value="kokoro">Local (Kokoro)</option>
            <option value="elevenlabs">Natural (ElevenLabs)</option><option value="browser">Browser</option></select></div>
          <div class="apxv-row"><label>Local voice</label><select data-set="ttsVoice"><option value="">Default</option></select></div>
          <div class="apxv-row"><label>Speed</label><input data-set="ttsSpeed" type="range" min="0.8" max="1.4" step="0.05"></div>
          <div class="apxv-row"><label>Thinking sound</label><input data-set="thinkingCue" type="checkbox"></div>
          <div class="apxv-row"><label>Grow when talking</label><input data-set="stageAuto" type="checkbox"></div>
          <div class="apxv-row"><button class="apxv-btn" data-act="test">Test voice</button></div>
          <div class="apxv-status"></div>
        </div>
        <div class="apxv-cap">
          <div class="apxv-you"></div>
          <div class="apxv-say"></div>
          <div class="apxv-note"></div>
        </div>
        <button class="apxv-hold" type="button">Hold to talk</button>
        <div class="apxv-hint"></div>`;
      this.el = el;
      const q = (s) => el.querySelector(s);
      this.dom = {
        chip: q(".apxv-chip"), meta: q(".apxv-meta"), mode: q('[data-act="mode"]'), panel: q(".apxv-panel"), you: q(".apxv-you"),
        say: q(".apxv-say"), note: q(".apxv-note"), hold: q(".apxv-hold"), hint: q(".apxv-hint"),
        status: q(".apxv-status"), key: q('[data-act="key"]'), voiceSel: q('[data-set="ttsVoice"]'),
      };

      el.addEventListener("click", (e) => {
        const b = e.target.closest("[data-act]");
        if (!b) return;
        const act = b.getAttribute("data-act");
        if (act === "close") this.close();
        else if (act === "settings") { this.dom.panel.classList.toggle("open"); tts.refresh(true).then(() => this.sync()); }
        else if (act === "mode") { setSetting("micMode", settings.micMode === "open" ? "ptt" : "open"); restartVoice(); }
        else if (act === "key") this.captureKey();
        else if (act === "test") { audio.resume(); mouth.stop(); mouth.say("This is how I sound. Hold the talk key whenever you want to cut in."); }
      });
      el.querySelectorAll("[data-set]").forEach((inp) => {
        inp.addEventListener("change", () => {
          const key = inp.getAttribute("data-set");
          const val = inp.type === "checkbox" ? inp.checked : inp.type === "range" ? parseFloat(inp.value) : inp.value;
          setSetting(key, val);
          if (key === "micMode") restartVoice();
          if (key === "ttsEngine") { tts.downUntil = 0; tts.refresh(true); }
        });
      });
      const hold = this.dom.hold;
      hold.addEventListener("pointerdown", (e) => { e.preventDefault(); try { hold.setPointerCapture(e.pointerId); } catch (x) { /* ignore */ } pttDown("button"); hold.classList.add("on"); });
      const up = () => { if (ptt.source === "button") pttUp(); hold.classList.remove("on"); };
      hold.addEventListener("pointerup", up);
      hold.addEventListener("pointercancel", up);
      this.sync();
    },

    /** Put the dock on a page surface (the Overview's orbital map). */
    mount(host) {
      if (!host) return;
      this.build();
      if (this.el.parentNode !== host) host.appendChild(this.el);
      this.host = host;
      tts.refresh();
      this.sync();
    },
    unmount(host) {
      if (host && this.host !== host) return;
      if (this.el && this.el.parentNode) this.el.parentNode.removeChild(this.el);
      this.host = null;
      if (this.dom.panel) this.dom.panel.classList.remove("open");
    },

    sync() {
      mini.sync();
      if (!this.el) return;
      const d = this.dom;
      const st = shownState();
      const labels = { off: "voice off", idle: settings.micMode === "ptt" ? "hold " + keyName(settings.pttKey).toLowerCase() + " to talk" : "standing by",
        listening: "listening", hearing: "hearing you", thinking: "thinking", speaking: "speaking", error: "fault", writing: "writing" };
      d.chip.textContent = labels[st] || st;
      d.chip.setAttribute("data-s", st);
      const eng = bus.engine || tts.pick();
      const meta = [eng === "kokoro" ? "local voice" + (bus.voice ? " · " + bus.voice.replace("_", " ") : "")
        : eng === "elevenlabs" ? "natural voice" : eng === "browser" ? "browser voice" : ""];
      if (bus.firstAudioMs != null) meta.push("first word " + (bus.firstAudioMs / 1000).toFixed(1) + "s");
      d.meta.textContent = meta.filter(Boolean).join(" · ");
      d.mode.textContent = settings.micMode === "open" ? "Hands-free" : "Push to talk";
      d.mode.setAttribute("aria-pressed", settings.micMode === "open" ? "true" : "false");
      const you = bus.partial || bus.heard;
      d.you.className = "apxv-you" + (bus.partial ? " partial" : "");
      d.you.innerHTML = you ? "<b>You</b>" : "";
      if (you) d.you.appendChild(document.createTextNode(you));
      const say = bus.saying || (bus.state === "thinking" ? "" : bus.said.split(/(?<=[.!?])\s+/).slice(-1)[0] || "");
      d.say.innerHTML = say ? "<b>Apex</b>" : "";
      if (say) d.say.appendChild(document.createTextNode(say));
      d.note.textContent = bus.state === "error" ? bus.err
        : bus.writing ? "Writing “" + bus.writing.title + "” beside me." : (bus.note || "");
      d.hint.textContent = `Hold ${keyName(settings.pttKey)} to talk · Esc stops Apex`;
      d.key.textContent = this.capturingKey ? "Press a key…" : keyName(settings.pttKey);
      d.status.textContent = tts.describe();
      this.el.classList.toggle("engaged", this.visible);
      this.el.classList.toggle("speaking", mouth.speaking);
      this.el.querySelectorAll("[data-set]").forEach((inp) => {
        const key = inp.getAttribute("data-set");
        if (inp.type === "checkbox") inp.checked = !!settings[key];
        else if (document.activeElement !== inp) inp.value = String(settings[key]);
      });
      const voices = (tts.status && tts.status.voices) || [];
      if (d.voiceSel.options.length !== voices.length + 1) {
        d.voiceSel.innerHTML = '<option value="">Default</option>' + voices.map((v) => `<option value="${v.id}">${v.id} · ${v.label}</option>`).join("");
        d.voiceSel.value = settings.ttsVoice;
      }
    },

    captureKey() {
      this.capturingKey = true; this.sync();
      const onKey = (e) => {
        e.preventDefault(); e.stopPropagation();
        window.removeEventListener("keydown", onKey, true);
        this.capturingKey = false;
        if (e.code !== "Escape") setSetting("pttKey", e.code);
        this.sync();
      };
      window.addEventListener("keydown", onKey, true);
    },

    /** Engage: the centre grows in place. `pin` keeps it there until Done or Esc. */
    open(pin) {
      clearTimeout(this.closeTimer);
      if (pin) this.pinned = true;
      if (!this.visible) {
        this.visible = true;
        tts.refresh();
        bus.emit();
      }
      kick();
    },
    close() {
      clearTimeout(this.closeTimer);
      this.pinned = false;
      if (!this.visible) return;
      this.visible = false;
      if (this.dom.panel) this.dom.panel.classList.remove("open");
      bus.emit();
    },
    toggle() { this.visible ? this.close() : this.open(true); },
    autoOpen(force) { if (force || settings.stageAuto) this.open(false); },
    armAutoClose() {
      clearTimeout(this.closeTimer);
      if (this.pinned || !this.visible) return;
      this.closeTimer = setTimeout(() => {
        const busy = mouth.speaking || ptt.held || !!bus.writing || ["hearing", "thinking", "speaking"].includes(bus.state);
        if (busy) this.armAutoClose(); else this.close();
      }, 7000);
    },
  };

  bus.on(() => stage.sync());

  /** The state to show: writing a document reads as its own state. */
  function shownState() {
    if (bus.writing && bus.state !== "speaking" && bus.state !== "hearing" && bus.state !== "error") return "writing";
    return bus.state;
  }

  window.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || stage.capturingKey || isTyping(e.target)) return;
    if (mouth.speaking || bus.state === "thinking") { interrupt("escape"); e.preventDefault(); }
    else if (stage.visible) { stage.close(); e.preventDefault(); }
  });

  /* ---------------------------------------------------------------- mini
     A small live Radial for the top bar on every page but the Overview, with
     a one-line caption beside it while a conversation is on. Clicking it goes
     to the Overview with the centre engaged. */
  const mini = {
    canvas: null, cap: null, radial: null, lastDraw: 0,
    mount(canvas, caption) {
      if (!canvas) return;
      this.canvas = canvas; this.cap = caption || null;
      if (!this.radial) this.radial = new Radial({ grains: 320 });
      this.draw(0.016);
      this.sync();
      kick();
    },
    unmount(canvas) {
      if (canvas && canvas !== this.canvas) return;
      this.canvas = null; this.cap = null;
    },
    live() {
      return !!this.canvas && (recording() || stage.visible || !!bus.writing || mouth.speaking || bus.level > 0.01);
    },
    draw(dt) {
      const cv = this.canvas;
      if (!cv) return;
      const r = cv.getBoundingClientRect();
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      const w = Math.max(2, Math.round(r.width * dpr)), h = Math.max(2, Math.round(r.height * dpr));
      if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
      const g = cv.getContext("2d");
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, r.width, r.height);
      const R = Math.min(r.width, r.height) / 5.4;
      this.radial.drawEmbedded(g, r.width / 2, r.height / 2, R, dt);
      this.lastDraw = now();
    },
    open() {
      audio.resume();
      try { page.showOverview && page.showOverview(); } catch (e) { /* ignore */ }
      stage.open(true);
    },
    sync() {
      if (!this.cap) return;
      const st = shownState();
      let line = "";
      if (stage.visible || st === "writing") {
        if (st === "hearing") line = bus.partial || bus.heard ? "You: " + (bus.partial || bus.heard) : "Listening…";
        else if (st === "thinking") line = "Thinking…";
        else if (st === "speaking") line = bus.saying ? "Apex: " + bus.saying : "Speaking…";
        else if (st === "writing") line = "Writing: " + bus.writing.title;
        else if (st === "error") line = bus.err || "Voice error";
      }
      this.cap.textContent = line;
      this.cap.style.display = line ? "" : "none";
    },
  };

  /* ---------------------------------------------------------- frame loop */
  let raf = 0, lastT = 0, lastSample = 0;
  /** Sample the live audio at most once per display frame, whoever asks first. */
  function tickAudio(dt) {
    const t = now();
    if (t - lastSample < 8) return;
    lastSample = t;
    sampleAudio(dt);
    feedOrbital();
  }
  function frame(t) {
    raf = 0;
    const dt = clamp((t - lastT) / 1000, 0.001, 0.05);
    lastT = t;
    tickAudio(dt);
    if (mini.canvas && (mini.live() || now() - mini.lastDraw > 400)) mini.draw(dt);
    if (needsLoop()) raf = requestAnimationFrame(frame);
  }
  function needsLoop() {
    return !document.hidden && (mini.live() || mouth.speaking || synth.active || synth.env > 0.02
      || fedOrbital || bus.level > 0.01 || ptt.held);
  }
  function kick() {
    if (!raf && !document.hidden) { lastT = performance.now(); raf = requestAnimationFrame(frame); }
  }
  document.addEventListener("visibilitychange", kick);

  // Reflect the page's mic being switched off elsewhere.
  setInterval(() => {
    if (!page.isRecording) return;
    const rec = recording();
    if (!rec && bus.state !== "off" && !mouth.speaking) bus.set({ state: "off", partial: "" });
    if (rec && bus.state === "off") bus.set({ state: settings.micMode === "open" ? "listening" : "idle" });
  }, 1000);

  /* ------------------------------------------------- overview hero core
     The Command Centre's orbital map hands its centre to the Radial: sonar at
     rest, the intake ring while you talk, the radar while Apex thinks or
     writes, the starburst while it answers. While engaged it grows in place
     (`grow` eases 0 to 1) and the map pushes its nodes out and dims them. */
  const STATE_WORDS = {
    listening: "listening", hearing: "hearing you", thinking: "thinking",
    speaking: "speaking", error: "voice error",
  };
  const GROW = 0.75;
  const core = {
    radial: null, last: 0, g: 0,
    /** Draw the centre into `g` (CSS pixels). False means "draw your own". */
    draw(g, cx, cy, R) {
      const t = now();
      const dt = this.last ? clamp((t - this.last) / 1000, 0.001, 0.05) : 0.016;
      this.last = t;
      this.g = lerp(this.g, stage.visible ? 1 : 0, 1 - Math.pow(0.04, dt));
      if (!this.radial) this.radial = new Radial({ grains: 1300 });
      tickAudio(dt);
      this.radial.drawEmbedded(g, cx, cy, R * (1 + GROW * this.g), dt);
      return true;
    },
    /** 0 at rest, 1 fully grown. The map uses it to make room. */
    grow() { return this.g; },
    /** Is (dx, dy) from the centre on the core? */
    hit(dx, dy, R) { return Math.hypot(dx, dy) < R * (1 + GROW * this.g) * 1.45; },
    open() { audio.resume(); stage.toggle(); },
    /** A few words for the overview's caption, or "" when there is nothing to say. */
    words() {
      const st = shownState();
      if (st === "writing") return "writing " + (bus.writing.kind || "a document");
      return STATE_WORDS[st] || "";
    },
  };

  /* ----------------------------------------------------------- documents
     Every document event, from the voice socket or from a typed request
     (apex_outputs.js), passes through here: the Radial shows the radar while
     a draft is written, the page's pane gets the text, and a draft asked for
     by voice is announced when it is done. */
  function docEvent(m) {
    if (!m || !m.type) return;
    switch (m.type) {
      case "doc_start":
        cue.stop();
        bus.set({ writing: { id: m.id, title: m.title || "document", kind: m.kind || "", source: m.source || "" } });
        stage.autoOpen();
        break;
      case "doc_done": case "doc_stopped": case "doc_error": {
        const was = bus.writing;
        if (!was || was.id === m.id) bus.set({ writing: null });
        if (m.spoken && was && was.source === "voice" && recording() && !dropSpeech) mouth.say(m.spoken);
        stage.armAutoClose();
        break;
      }
      default: break;
    }
    try { page.onDoc && page.onDoc(m); } catch (e) { /* the pane must not break voice */ }
  }

  /* ---------------------------------------------------------------- API */
  window.ApexVoice = {
    version: "1.2.0",
    settings, bus, stage, tts, core, mini,
    dock: { mount: (el) => stage.mount(el), unmount: (el) => stage.unmount(el) },
    get engaged() { return stage.visible; },
    attach(hooks) {
      Object.assign(page, hooks || {});
      if (bus.state === "off" && recording()) bus.set({ state: settings.micMode === "open" ? "listening" : "idle" });
      tts.refresh();
    },
    onEvent,
    docEvent,
    speakText,
    speechDone: () => mouth.done(),
    cancel: () => mouth.stop(),
    interrupt,
    micGate,
    wakeWord: (pref) => (settings.micMode === "ptt" ? false : !!pref),
    tapMic, untapMic,
    say: (text, opts) => mouth.say(text, opts),
    get speaking() { return mouth.speaking; },
    demo(state) {
      demo.state = state || "";
      bus.set({ state: state || "idle" });
      if (state === "thinking") { bus.set({ heard: "What changed in the sector this week?" }); }
      if (state === "speaking") bus.set({ heard: "What changed in the sector this week?", saying: "Two TEQSA updates landed overnight, and one affects the micro-credential proposal.", engine: "kokoro", voice: "bf_emma", firstAudioMs: 1240, turns: 3 });
      stage.open(true);
      kick();
    },
  };

  function init() {
    const q = new URLSearchParams(location.search);
    const d = q.get("apexdemo");
    if (d) window.ApexVoice.demo(d);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
