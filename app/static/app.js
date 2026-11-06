/* Hangul Buddy - web-versie van het toestel. Praat met dezelfde API als de ESP32 later. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = { config: null, card: null, revealed: false, assessed: null, busy: false, recording: null, practice: null, lessons: [] };
  const LOCALES = { ko: "ko-KR", de: "de-DE", it: "it-IT", ja: "ja-JP", zh: "zh-CN" };
  const CJK = new Set(["ko", "ja", "zh"]);
  const GRADE_BUTTON = { 1: 1, 2: 1, 3: 3, 4: 4, 5: 5 }; // voorgestelde grade -> knop

  // ------------------------------------------------------------ helpers
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* privémodus */ } },
  };

  // Automatisch beoordelen: drempels komen uit /api/config, dit zijn de standaardwaarden
  const thresholds = () => state.config?.auto || { easy: 90, retry: 80 };
  const autoOn = () => store.get("hb_auto") === "1";
  const manualGrading = () => !state.practice && !autoOn();
  // Slim opnemen: stopt vanzelf kort na je laatste woord en knipt stilte weg (bespaart ook Azure-tijd)
  const VAD = { minRms: 0.012, startChunks: 2, silenceMs: 800, noSpeechMs: 4500, maxMs: 8000, preChunks: 4, postChunks: 4,
    sentenceSilenceMs: 1500, sentenceMaxMs: 12000, wordNoSpeechMs: 6000, wordMaxMs: 4500 };
  const wordwiseOn = () => store.get("hb_wordwise") !== "0";
  const typing = () => store.get("hb_mode") === "type";
  const isSentence = (it) => (it?.words?.length || 0) > 1 || (["ja", "zh"].includes(it?.lang) && (it.text || "").length > 6);
  const gradeFor = (s) => (s >= 90 ? 5 : s >= 75 ? 4 : s >= 60 ? 3 : s >= 40 ? 2 : 1); // zelfde grenzen als de server
  const vadOn = () => store.get("hb_vad") !== "0";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const shuffle = (a) => {
    const out = [...a];
    for (let i = out.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [out[i], out[j]] = [out[j], out[i]];
    }
    return out;
  };

  function toast(msg, ms = 3200) {
    const t = $("toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { t.hidden = true; }, ms);
  }

  async function api(path, opts = {}) {
    const headers = { ...(opts.headers || {}) };
    const token = store.get("hb_token");
    if (token) headers["X-App-Token"] = token;
    if (opts.json !== undefined) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.json);
    }
    const res = await fetch(path, { ...opts, headers });
    if (res.status === 401) {
      await askToken();
      return api(path, opts);
    }
    return res;
  }

  // Inloggen: één dialoog, ook als meerdere calls tegelijk een 401 krijgen
  let loginPromise = null;
  function askToken() {
    if (loginPromise) return loginPromise;
    loginPromise = new Promise((resolve) => {
      const dlg = $("login-dialog");
      const show = (claim) => {
        $("login-form").hidden = claim;
        $("claim-form").hidden = !claim;
        $(claim ? "claim-code" : "login-user").focus();
      };
      const submit = (path, body, msgId) => async (e) => {
        e.preventDefault();
        const msg = $(msgId);
        msg.hidden = true;
        let res, data = {};
        try {
          res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body()) });
          data = await res.json().catch(() => ({}));
        } catch { msg.textContent = "Server niet bereikbaar."; msg.hidden = false; return; }
        if (!res.ok) { msg.textContent = typeof data.detail === "string" ? data.detail : "Dat lukt niet, controleer je gegevens."; msg.hidden = false; return; }
        store.set("hb_token", data.token);
        $("login-pass").value = $("claim-pass").value = "";
        dlg.close();
        loginPromise = null;
        resolve();
      };
      $("login-form").onsubmit = submit("/api/login", () => ({ username: $("login-user").value, password: $("login-pass").value }), "login-msg");
      $("claim-form").onsubmit = submit("/api/claim", () => ({ code: $("claim-code").value, username: $("claim-user").value, password: $("claim-pass").value }), "claim-msg");
      $("to-claim").onclick = () => show(true);
      $("to-login").onclick = () => show(false);
      dlg.oncancel = (e) => e.preventDefault(); // Esc sluit het inlogscherm niet
      dlg.showModal();
      show(false);
    });
    return loginPromise;
  }

  async function getJSON(path) {
    const res = await api(path);
    if (!res.ok) throw new Error(`${path}: ${res.status}`);
    return res.json();
  }

  const scoreClass = (s) => (s >= 75 ? "ok" : s >= 55 ? "mid" : "bad");
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ------------------------------------------------------------ kaart tonen
  async function loadNext(extra = false) {
    resetCard();
    let data;
    try {
      data = await getJSON(`/api/next${extra ? "?extra=1" : ""}`);
    } catch (e) {
      $("word").textContent = "Server niet bereikbaar";
      return;
    }
    refreshStats();
    if (data.done) return showDone(data);

    state.card = data;
    $("done").hidden = true;
    $("screen").parentElement.hidden = false;
    fillCard(data.item, data.lesson, { new: "Nieuw", review: "Herhaling", relearn: "Opnieuw" }[data.kind], data.kind);

    if (data.show_note && data.lesson.note) {
      $("note-title").textContent = `Les ${data.lesson.id}: ${data.lesson.title}`;
      $("note-text").textContent = data.lesson.note;
      $("note-card").hidden = false;
    }
    // Nieuwe woorden meteen laten horen (browsers laten dat pas toe na een klik/toets)
    if (data.kind === "new" && state.interacted) listen();
  }

  function fillCard(item, lesson, badgeText, badgeClass) {
    const badge = $("kind-badge");
    badge.textContent = badgeText;
    badge.className = `badge ${badgeClass}`;
    $("lesson-name").textContent = `Les ${lesson.id} · ${lesson.title}`;
    const word = $("word");
    const text = item.text || item.ko;
    const lang = item.lang || "ko";
    word.textContent = text;
    word.lang = lang;
    word.classList.toggle("long", text.length > (CJK.has(lang) ? 6 : 12));
    $("rom").textContent = item.rom || "";
    $("rom").hidden = !item.rom;
    $("nl").textContent = item.nl;
    $("tip").textContent = item.tip ? `💡 ${item.tip}` : "";
    $("tip").hidden = !item.tip;
    prepareTyping(item, badgeClass);
  }

  // ------------------------------------------------------------ typen
  // Herhalingen: je ziet het Nederlands en typt het woord. Nieuwe woorden: je typt ze over.
  function applyMode() {
    const t = typing();
    document.querySelectorAll(".mode-btn").forEach((b) => {
      const on = (b.dataset.mode === "type") === t;
      b.classList.toggle("active", on);
      b.setAttribute("aria-pressed", on);
    });
    document.querySelector(".device .buttons").hidden = t;
    $("type-form").hidden = !t;
  }

  function prepareTyping(item, kind) {
    applyMode();
    state.typing = null;
    if (!typing()) return;
    const copy = kind === "new";
    state.typing = { item, copy, hint: false, done: false, advanced: false };
    if (!copy) {
      const word = $("word");
      word.textContent = item.nl;
      word.lang = "nl";
      word.classList.toggle("long", item.nl.length > 14);
    }
    $("reveal").hidden = !copy;
    const inp = $("type-input");
    inp.value = "";
    inp.readOnly = false;
    inp.className = "";
    inp.lang = item.lang || "ko";
    inp.placeholder = copy ? "Typ het woord over" : `Typ het in het ${langName(item.lang || "ko").toLowerCase()}`;
    $("type-go").textContent = "Controleer";
    $("type-hint").hidden = copy;
    $("type-skip").hidden = copy;
    if (!$("view-practice").hidden && matchMedia("(hover: hover)").matches) inp.focus({ preventScroll: true });
  }

  async function judgeTyped(typed, skipped = false) {
    const t = state.typing, card = state.card;
    if (!t || !card || state.busy || t.done) return;
    state.busy = true;
    let r;
    try {
      if (skipped) r = { verdict: "wrong", score: 0, expected: t.item.text };
      else {
        const res = await api(`/api/typing/${encodeURIComponent(t.item.id)}`, { method: "POST", json: { typed } });
        if (!res.ok) throw new Error(res.status);
        r = await res.json();
      }
    } catch (e) {
      state.busy = false;
      return toast(`Controleren mislukt (${e.message}).`);
    }
    state.busy = false;
    if (state.card !== card || state.typing !== t) return;
    t.done = true;
    const g = r.verdict === "exact" ? (t.hint || t.copy ? 4 : 5) : r.verdict === "close" ? 3 : 1;
    const inp = $("type-input");
    inp.readOnly = true;
    inp.className = { exact: "ok", close: "mid", wrong: "bad" }[r.verdict];
    const word = $("word");
    word.textContent = r.expected;
    word.lang = t.item.lang || "ko";
    word.classList.toggle("long", r.expected.length > (CJK.has(word.lang) ? 6 : 12));
    $("reveal").hidden = false;
    const mine = typed ? `<div class="heard">Jij typte: <b lang="${esc(t.item.lang || "ko")}">${esc(typed)}</b></div>` : "";
    const msg = {
      exact: `<div class="score ok">✅ Juist!</div>${t.hint ? '<div class="heard">Met hint, dus Goed in plaats van Makkelijk.</div>' : ""}`,
      close: `<div class="score mid">Bijna!</div>${mine}<div class="heard">Let op een typfout of een ontbrekend accent.</div>`,
      wrong: `<div class="score bad">${skipped ? "Het antwoord" : "Niet juist"}</div>${mine}`,
    }[r.verdict];
    $("feedback").hidden = false;
    $("feedback").innerHTML = msg;
    $("type-hint").hidden = $("type-skip").hidden = true;
    $("type-go").textContent = "Volgende";
    $("type-go").focus({ preventScroll: true });
    if (!state.practice) {
      try { await saveGrade(g, { score: r.score, provider: "typing" }); }
      catch (e) { toast(`Opslaan mislukt (${e.message}).`); }
    }
    if (r.verdict === "exact") {
      await sleep(1300);
      if (state.typing === t && state.card === card && !t.advanced) typingNext();
    }
  }

  function typingNext() {
    const t = state.typing;
    if (!t || !t.done || t.advanced || state.busy) return;
    t.advanced = true;
    if (state.practice) nextPractice();
    else loadNext();
  }

  $("type-form").onsubmit = (e) => {
    e.preventDefault();
    const t = state.typing;
    if (!t) return;
    if (t.done) return typingNext();
    const typed = $("type-input").value.trim();
    if (typed) judgeTyped(typed);
  };
  $("type-hint").onclick = () => { if (state.typing) { state.typing.hint = true; listen(); } };
  $("type-skip").onclick = () => judgeTyped("", true);
  document.querySelectorAll(".mode-btn").forEach((b) => {
    b.onclick = () => {
      store.set("hb_mode", b.dataset.mode);
      applyMode();
      if (state.practice) showPracticeCard(); else loadNext();
    };
  });

  function resetCard() {
    state.card = null;
    state.revealed = false;
    state.assessed = null;
    $("reveal").hidden = true;
    $("feedback").hidden = true;
    $("feedback").innerHTML = "";
    $("grades").hidden = true;
    $("practice-bar").hidden = true;
    $("note-card").hidden = true;
    document.querySelectorAll(".grade").forEach((b) => b.classList.remove("suggested"));
  }

  function showDone(data) {
    $("screen").parentElement.hidden = true;
    $("grades").hidden = true;
    const d = $("done");
    d.hidden = false;
    let txt;
    if (data.course_finished) txt = "Je hebt alle lessen doorlopen. Herhalingen blijven terugkomen.";
    else if (data.next_due) txt = `Volgende herhaling: ${new Date(data.next_due).toLocaleDateString("nl-BE", { weekday: "long", day: "numeric", month: "long" })}.`;
    else txt = "Kom morgen terug voor nieuwe woorden.";
    $("done-text").textContent = txt;
    $("btn-extra").hidden = !data.more_new_available;
  }

  function reveal() {
    if (!state.card) return;
    state.revealed = true;
    $("reveal").hidden = false;
    $("grades").hidden = !manualGrading() || typing();
  }

  // ------------------------------------------------------------ luisteren
  // Safari (iPhone) laat geluid alleen afspelen via een element dat bij een tik is "ontgrendeld"; daarom hergebruiken we er één
  const audio = new Audio();
  audio.preload = "auto";
  const SILENCE = "data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA=";
  let unlocked = false;
  function unlockAudio() {
    if (unlocked) return;
    unlocked = true;
    try {
      audio.src = SILENCE;
      const p = audio.play();
      if (p && p.catch) p.catch(() => { unlocked = false; });
    } catch { unlocked = false; }
  }
  async function listen(item = state.card?.item) {
    if (!item) return;
    press("btn-listen");
    const text = item.text || item.ko;
    if (state.config?.azure) {
      try {
        const res = await api(`/api/tts/${encodeURIComponent(item.id)}`);
        if (res.ok) {
          const url = URL.createObjectURL(await res.blob());
          audio.pause();
          audio.src = url;
          audio.onended = () => URL.revokeObjectURL(url);
          await audio.play();
          return;
        }
      } catch { /* val terug op browserstem */ }
    }
    speakBrowser(text, item.lang || "ko");
  }

  function speakBrowser(text, lang = "ko") {
    if (!("speechSynthesis" in window)) return toast("Je browser kan geen tekst voorlezen.");
    const u = new SpeechSynthesisUtterance(text);
    u.lang = LOCALES[lang] || lang;
    u.rate = 0.8;
    const voice = speechSynthesis.getVoices().find((v) => v.lang.replace("_", "-").startsWith(lang));
    if (voice) u.voice = voice;
    else toast(`Geen stem voor ${langName(lang).toLowerCase()} gevonden op dit toestel. Stel Azure in voor betere uitspraak.`);
    speechSynthesis.cancel();
    speechSynthesis.speak(u);
  }

  // ------------------------------------------------------------ inspreken
  async function speak() {
    if (!state.card || state.busy) return;
    if (state.recording) return state.recording.stop();
    if (state.config?.azure) return speakAzure();
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (SR) return recognizeBrowser(SR);
    toast("Spraakcheck werkt niet in deze browser. Gebruik Chrome, of stel Azure in. Beoordeel jezelf intussen.");
    reveal();
  }

  const postWav = (id, wav, word) => api(`/api/assess/${encodeURIComponent(id)}${word == null ? "" : `?word=${word}`}`, {
    method: "POST", body: wav, headers: { "Content-Type": "audio/wav" },
  });

  async function speakAzure() {
    const item = state.card.item;
    if (wordwiseOn() && item.words?.length > 1) return speakWords();
    if (state.card.wordResults) { state.card.wordResults = null; $("word").textContent = item.text; } // oude woordkleuren weg
    // Hele zinnen krijgen meer ruimte voor pauzes, anders stopt de opname midden in je zin
    const wav = await recordClip(isSentence(item) ? { silenceMs: VAD.sentenceSilenceMs, maxMs: VAD.sentenceMaxMs, noSpeechMs: 6000 } : {});
    if (wav) await sendAssessment(() => postWav(item.id, wav));
  }

  // Zin woord voor woord: elk woord wordt apart opgenomen en beoordeeld, de zin krijgt het gemiddelde
  function renderWords(words, results, cur) {
    const html = words.map((w, i) => {
      const cls = i === cur ? "cur" : results[i] ? scoreClass(results[i].score) : "todo";
      return `<span class="w ${cls}">${esc(w)}</span>`;
    }).join(" ");
    $("word").innerHTML = `<span class="words">${html}</span>`;
  }

  async function speakWords() {
    const card = state.card;
    const words = card.item.words;
    if (!card.wordResults || card.wordResults.length >= words.length) card.wordResults = [];
    const results = card.wordResults;
    const stillHere = () => state.card === card;
    while (results.length < words.length && stillHere()) {
      const i = results.length;
      renderWords(words, results, i);
      $("feedback").hidden = false;
      $("feedback").innerHTML = `<div class="heard">Woord ${i + 1} van ${words.length}: zeg <b lang="${esc(card.item.lang)}">${esc(words[i])}</b></div>`;
      const wav = await recordClip({ noSpeechMs: VAD.wordNoSpeechMs, maxMs: VAD.wordMaxMs });
      if (!stillHere()) return;
      if (!wav) {
        $("feedback").innerHTML = `<div class="heard">Tik op Spreek om verder te gaan bij <b lang="${esc(card.item.lang)}">${esc(words[i])}</b>.</div>`;
        return;
      }
      state.busy = true;
      let r = null;
      try {
        const res = await postWav(card.item.id, wav, i);
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.status);
        r = await res.json();
      } catch (e) {
        $("feedback").innerHTML = `<div class="heard">Beoordelen mislukt: ${esc(e.message)}. Tik op Spreek om het woord opnieuw te proberen.</div>`;
      } finally {
        state.busy = false;
      }
      if (!r) return;
      results.push(r);
    }
    if (!stillHere() || results.length < words.length) return;
    renderWords(words, results, -1);
    const avg = (key) => {
      const v = results.map((r) => r[key]).filter((x) => typeof x === "number");
      return v.length ? v.reduce((a, b) => a + b, 0) / v.length : undefined;
    };
    const score = avg("score");
    const agg = {
      score, grade: gradeFor(score), provider: "azure", heard: "",
      accuracy: avg("accuracy"), completeness: avg("completeness"),
      words: results.map((r, i) => ({ word: words[i], score: r.score, syllables: r.words?.[0]?.syllables || [] })),
    };
    showAssessment(agg);
    await autoJudge(card, agg);
  }

  function recordClip(opts = {}) {
    return new Promise((resolve) => recordInto(opts, resolve));
  }

  async function recordInto(opts, resolve) {
    const silenceMs = opts.silenceMs ?? VAD.silenceMs;
    const noSpeechMs = opts.noSpeechMs ?? VAD.noSpeechMs;
    const maxMs = opts.maxMs ?? VAD.maxMs;
    let stream;
    const AC = window.AudioContext || window.webkitAudioContext;
    const ctx = new AC(); // binnen de tik aanmaken: Safari houdt hem anders op "suspended"
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    } catch {
      ctx.close().catch(() => {});
      toast("Geen toegang tot je microfoon. De app moet via https of localhost draaien.");
      return resolve(null);
    }
    if (ctx.state === "suspended") { try { await ctx.resume(); } catch { /* opnemen lukt dan niet */ } }
    const src = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(4096, 1, 1);
    const chunks = [];
    const chunkMs = (4096 / ctx.sampleRate) * 1000;
    const rmsOf = (a) => { let s = 0; for (let i = 0; i < a.length; i++) s += a[i] * a[i]; return Math.sqrt(s / a.length); };
    let noise = Infinity, calibrated = 0, above = 0, first = -1, last = -1, silentFor = 0;
    const finish = () => state.recording?.stop({ auto: true });
    proc.onaudioprocess = (e) => {
      const buf = new Float32Array(e.inputBuffer.getChannelData(0));
      chunks.push(buf);
      if (!vadOn()) return;
      const idx = chunks.length - 1;
      const r = rmsOf(buf);
      if (calibrated < 3) { noise = Math.min(noise, r); calibrated++; return; } // eerste ±250 ms: omgevingsgeluid
      if (r > Math.max(VAD.minRms, noise * 2.5)) {
        above++;
        silentFor = 0;
        if (first < 0 && above >= VAD.startChunks) { first = idx - VAD.startChunks + 1; setSpeakLabel("Ik hoor je"); }
        if (first >= 0) last = idx;
      } else {
        above = 0;
        if (first >= 0 && (silentFor += chunkMs) >= silenceMs) finish();
      }
    };
    src.connect(proc);
    proc.connect(ctx.destination);
    setRecording(true);

    const timers = [setTimeout(() => state.recording?.stop(), maxMs)];
    if (vadOn()) timers.push(setTimeout(() => { if (first < 0) finish(); }, noSpeechMs));
    state.recording = {
      stop: async ({ auto = false } = {}) => {
        timers.forEach(clearTimeout);
        state.recording = null;
        setRecording(false);
        proc.disconnect();
        src.disconnect();
        stream.getTracks().forEach((t) => t.stop());
        const rate = ctx.sampleRate;
        await ctx.close();
        let use = chunks;
        if (vadOn() && first >= 0) use = chunks.slice(Math.max(0, first - VAD.preChunks), last + 1 + VAD.postChunks);
        else if (vadOn() && auto) { toast("Ik hoorde niets. Spreek iets luider en probeer opnieuw."); return resolve(null); }
        resolve(encodeWav(downsample(concat(use), rate, 16000), 16000));
      },
    };
  }

  function recognizeBrowser(SR) {
    const rec = new SR();
    rec.lang = LOCALES[state.card?.item?.lang || "ko"] || "ko-KR";
    rec.maxAlternatives = 3;
    rec.interimResults = false;
    let heard = "";
    rec.onresult = (e) => { heard = e.results[0][0].transcript; };
    rec.onerror = (e) => {
      if (e.error === "not-allowed") toast("Geen toegang tot je microfoon.");
      else if (e.error !== "no-speech" && e.error !== "aborted") toast(`Spraakherkenning: ${e.error}`);
    };
    rec.onend = async () => {
      state.recording = null;
      setRecording(false);
      if (!heard) return toast("Ik hoorde niets. Probeer opnieuw.");
      await sendAssessment(() => api(`/api/assess-text/${encodeURIComponent(state.card.item.id)}`, {
        method: "POST", json: { heard },
      }));
    };
    state.recording = { stop: () => rec.stop() };
    setRecording(true);
    rec.start();
  }

  function setSpeakLabel(text) { $("btn-speak").querySelector(".hw-label").textContent = text; }

  function setRecording(on) {
    const b = $("btn-speak");
    b.classList.toggle("recording", on);
    setSpeakLabel(on ? (vadOn() || !state.config?.azure ? "Luister…" : "Stop") : "Spreek");
  }

  async function sendAssessment(request) {
    const card = state.card;
    let result = null;
    state.busy = true;
    $("feedback").hidden = false;
    $("feedback").innerHTML = '<div class="heard">Beoordelen…</div>';
    try {
      const res = await request();
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || res.status);
      }
      result = await res.json();
      showAssessment(result);
    } catch (e) {
      $("feedback").innerHTML = `<div class="heard">Beoordelen mislukt: ${esc(e.message)}</div>`;
    } finally {
      state.busy = false;
    }
    if (result) await autoJudge(card, result);
  }

  // Automatisch beoordelen. Bij gewone herhalingen:
  //  >= easy  : Makkelijk, volgend woord (heb je eerder al Moeilijk gekregen: gewoon door)
  //  >= retry : niets opgeslagen, opnieuw proberen
  //  <  retry : meteen Moeilijk opgeslagen, blijven tot je easy haalt
  async function autoJudge(card, r) {
    if (!autoOn() || !card || state.card !== card) return;
    const { easy, retry } = thresholds();
    const say = (text, cls) => $("feedback").insertAdjacentHTML("beforeend", `<div class="auto-msg ${cls}">${text}</div>`);
    if (state.practice) {
      // Vrij oefenen: nooit iets opslaan, wel automatisch door vanaf easy
      if (r.score >= easy) {
        say("✅ Gelukt! Volgend woord…", "ok");
        state.busy = true;
        await sleep(1600);
        state.busy = false;
        if (state.practice && state.card === card) nextPractice();
      } else {
        say(`Probeer opnieuw voor ${easy}%, of ga naar het volgende woord.`, "mid");
      }
      return;
    }
    state.busy = true;
    try {
      if (r.score >= easy) {
        if (card.hardMarked) {
          say("✅ Gelukt! Volgend woord…", "ok");
        } else {
          await saveGrade(5, r);
          say("✅ Makkelijk! Volgend woord…", "ok");
        }
        await sleep(1600);
        state.busy = false;
        await loadNext();
        return;
      }
      if (r.score >= retry) {
        say(`Bijna! Je hebt ${easy}% nodig. Probeer opnieuw.`, "mid");
        return;
      }
      if (!card.hardMarked) {
        await saveGrade(3, r);
        card.hardMarked = true;
        say(`Gemarkeerd als Moeilijk. Probeer opnieuw tot je ${easy}% haalt.`, "bad");
      } else {
        say(`Probeer opnieuw tot je ${easy}% haalt.`, "bad");
      }
    } catch (e) {
      toast(`Opslaan mislukt (${e.message}).`);
    } finally {
      state.busy = false;
    }
  }

  function showAssessment(r) {
    state.assessed = r;
    const s = Math.round(r.score);
    let detail = "";
    const groups = (r.words || []).map((w) => (w.syllables?.length ? w.syllables : [{ text: w.word, score: w.score }]));
    if (groups.length) {
      detail = `<div>${groups.map((g) => `<span class="wgrp">${g.map((x) => `<span class="syl ${scoreClass(x.score)}" title="${Math.round(x.score)}%">${esc(x.text)}</span>`).join(" ")}</span>`).join(" ")}</div>`;
    }
    const parts = [["Uitspraak", r.accuracy], ["Vloeiendheid", r.fluency], ["Volledigheid", r.completeness]]
      .filter(([, v]) => typeof v === "number").map(([k, v]) => `${k} ${Math.round(v)}`);
    const sub = parts.length ? `<div class="sub">${parts.join(" · ")}</div>` : "";
    const verdict = s >= 90 ? "Uitstekend!" : s >= 75 ? "Goed!" : s >= 55 ? "Bijna" : "Nog eens proberen";
    $("feedback").innerHTML = `<div class="score ${scoreClass(s)}">${s}%</div><div>${verdict}</div>${detail}${sub}`;
    reveal();
    const btn = document.querySelector(`.grade[data-grade="${GRADE_BUTTON[r.grade]}"]`);
    document.querySelectorAll(".grade").forEach((b) => b.classList.toggle("suggested", b === btn));
  }

  // ------------------------------------------------------------ beoordelen
  async function saveGrade(g, assessed) {
    const body = { item_id: state.card.item.id, grade: g };
    if (assessed) {
      body.score = Math.max(0, Math.min(100, assessed.score));
      body.source = assessed.provider;
    }
    const res = await api("/api/grade", { method: "POST", json: body });
    if (!res.ok) throw new Error(res.status);
    const r = await res.json();
    if (r.xp) showXp(r.xp, r.lang_xp, r.lang);
    if (r.xp_gained) { xpToast(r); }
    return r;
  }

  // ------------------------------------------------------------ xp
  function langFlag(code) { return (state.config?.languages || []).find((l) => l.code === code)?.flag || ""; }
  function langName(code) { return (state.config?.languages || []).find((l) => l.code === code)?.name || code; }

  function showXp(x, lx, lang) {
    if (lx) $("xp-lang").textContent = `${langFlag(lang)} ${lx.total} XP`;
    $("xp-level").textContent = `Lv ${x.level}`;
    $("xp-fill").style.width = `${Math.min(100, Math.round((x.into_level / x.level_size) * 100))}%`;
    $("xp-text").textContent = `${x.total} XP`;
  }

  function xpToast(r) {
    toast(`+${r.xp_gained} XP${r.xp.today === r.xp_gained ? "" : ` · vandaag ${r.xp.today}`}`, 1400);
    const t = $("xp-text");
    t.classList.remove("pop"); void t.offsetWidth; t.classList.add("pop");
  }

  async function grade(g) {
    if (!state.card || state.busy) return;
    state.busy = true;
    try {
      const r = await saveGrade(g, state.assessed);
      if (g >= 3) {
        const when = r.interval === 1 ? "Morgen zie je dit woord terug." : `Terug over ${r.interval} dagen.`;
        toast(r.xp_gained ? `+${r.xp_gained} XP · ${when}` : when, 1800);
      }
      await loadNext();
    } catch (e) {
      toast(`Opslaan mislukt (${e.message}).`);
    } finally {
      state.busy = false;
    }
  }

  // ------------------------------------------------------------ audio-hulpjes
  function concat(chunks) {
    const out = new Float32Array(chunks.reduce((n, c) => n + c.length, 0));
    let o = 0;
    for (const c of chunks) { out.set(c, o); o += c.length; }
    return out;
  }

  function downsample(buf, from, to) {
    if (from === to) return buf;
    const ratio = from / to;
    const out = new Float32Array(Math.floor(buf.length / ratio));
    for (let i = 0; i < out.length; i++) {
      const start = Math.floor(i * ratio), end = Math.min(buf.length, Math.floor((i + 1) * ratio));
      let sum = 0;
      for (let j = start; j < end; j++) sum += buf[j];
      out[i] = sum / Math.max(1, end - start);
    }
    return out;
  }

  function encodeWav(samples, rate) {
    const buf = new ArrayBuffer(44 + samples.length * 2);
    const v = new DataView(buf);
    const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    str(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); str(8, "WAVE");
    str(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
    str(36, "data"); v.setUint32(40, samples.length * 2, true);
    for (let i = 0; i < samples.length; i++) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return new Blob([buf], { type: "audio/wav" });
  }

  // ------------------------------------------------------------ statistieken & lessen
  async function refreshStats() {
    try {
      const s = await getJSON("/api/stats");
      const n = state.card?.counts || {};
      $("st-due").textContent = `🔁 ${s.due_today}`;
      $("st-new").textContent = `✨ ${n.new_left ?? 0}`;
      $("st-streak").textContent = `🔥 ${s.streak}`;
      $("st-learned").textContent = `📚 ${s.learned}/${s.total}`;
      if (s.xp) showXp(s.xp, s.lang_xp, s.lang);
    } catch { /* niet kritisch */ }
  }

  async function renderLessons() {
    const list = await getJSON("/api/lessons");
    state.lessons = list;
    $("lessons").innerHTML = list.map((l) => {
      const pct = Math.round((l.learned / l.total) * 100);
      const seen = l.items.filter((it) => it.seen).length;
      const items = l.unlocked
        ? `<ul>${l.items.map((it) => `<li class="${it.seen ? "" : "unseen"}"><span class="ko" lang="${esc(it.lang || "ko")}">${esc(it.text || it.ko)}</span><span class="tr" title="${esc(it.nl)}">${esc(it.nl)}</span><button class="mini" data-listen="${esc(it.id)}" aria-label="Luister naar ${esc(it.text || it.ko)}">🔊</button></li>`).join("")}</ul>`
        : "";
      const actions = l.unlocked
        ? `<div class="lesson-actions"><button class="primary sm" data-practice="${l.id}" ${seen ? "" : "disabled"}>🎙️ Oefenen${seen ? ` (${seen} woorden)` : ""}</button>${seen ? "" : "<small>Je hebt hier nog geen woorden gezien.</small>"}</div>`
        : "";
      return `<article class="lesson ${l.unlocked ? "" : "locked"}">
        <div class="lesson-head"><h3>${l.unlocked ? "" : "🔒 "}Les ${l.id}: ${esc(l.title)}${l.current ? " · bezig" : ""}</h3><span>${l.learned}/${l.total}</span></div>
        <div class="bar"><div style="width:${pct}%"></div></div>
        ${l.unlocked ? `<p>${esc(l.note)}</p>${actions}${items}` : ""}
      </article>`;
    }).join("");
  }

  // ------------------------------------------------------------ vrij oefenen per les
  // Telt niet mee voor je herhalingsschema: je krijgt alleen een score.
  function startPractice(lessonId) {
    const les = state.lessons.find((l) => l.id === lessonId);
    const items = les ? shuffle(les.items.filter((it) => it.seen)) : [];
    if (!items.length) return toast("Je hebt nog geen woorden uit deze les gezien.");
    state.practice = { lesson: les, items, idx: 0 };
    switchView("practice");
    showPracticeCard();
  }

  function showPracticeCard() {
    const p = state.practice;
    resetCard();
    $("done").hidden = true;
    $("screen").parentElement.hidden = false;
    const item = p.items[p.idx];
    state.card = { kind: "practice", item, lesson: p.lesson, counts: {} };
    fillCard(item, p.lesson, "Oefenen", "practice");
    $("practice-progress").textContent = `Woord ${p.idx + 1} van ${p.items.length} · telt niet mee voor je herhalingen`;
    $("practice-bar").hidden = false;
    if (!typing()) reveal();
    if (state.interacted && !typing()) listen();
  }

  function nextPractice() {
    const p = state.practice;
    if (!p || state.busy) return;
    if (p.idx + 1 >= p.items.length) {
      toast("Klaar! Je hebt alle woorden van deze les geoefend.");
      return stopPractice();
    }
    p.idx++;
    showPracticeCard();
  }

  function stopPractice() {
    state.practice = null;
    resetCard();
    switchView("lessons");
    loadNext();
  }

  function initAuto() {
    const { easy, retry } = thresholds();
    const sw = $("auto-switch");
    sw.checked = autoOn();
    $("auto-help").textContent = `Vanaf ${easy}% Makkelijk. Onder ${retry}% Moeilijk, en je blijft op het woord tot je ${easy}% haalt.`;
    sw.onchange = () => {
      store.set("hb_auto", sw.checked ? "1" : "0");
      if (state.card && state.revealed) $("grades").hidden = !manualGrading();
    };
  }

  function switchView(view) {
    if (view !== "settings") state.lastView = view;
    document.querySelectorAll(".tab").forEach((t) => {
      const on = t.dataset.view === view;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", on);
    });
    $("btn-settings").setAttribute("aria-pressed", view === "settings");
    $("view-practice").hidden = view !== "practice";
    $("view-lessons").hidden = view !== "lessons";
    $("view-settings").hidden = view !== "settings";
    $("view-board").hidden = view !== "board";
    $("lang-bar").hidden = !(view === "practice" || view === "lessons");
    window.scrollTo(0, 0);
    if (view === "board") loadBoard();
    if (view === "lessons") renderLessons();
    if (view === "settings") loadSettings();
  }

  // ------------------------------------------------------------ ranglijst
  async function loadBoard(period = state.period || "week", lang = state.boardLang || "all") {
    state.period = period;
    state.boardLang = lang;
    const sel = $("board-lang");
    if (sel.options.length !== (state.config?.languages?.length || 0) + 1) {
      sel.replaceChildren(new Option("🌍 Totaal (alle talen)", "all"),
        ...(state.config?.languages || []).map((l) => new Option(`${l.flag} ${l.name}`, l.code)));
    }
    sel.value = lang;
    document.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("active", b.dataset.period === period));
    let data;
    try { data = await getJSON(`/api/leaderboard?period=${period}&lang=${lang}`); } catch { $("board-info").textContent = "Ranglijst laden mislukt."; return; }
    $("board-info").textContent = period === "week"
      ? `De week begint op maandag en loopt nog ${data.days_left} ${data.days_left === 1 ? "dag" : "dagen"}.`
      : "Alle XP sinds het begin.";
    if (lang !== "all") $("board-info").textContent += ` Alleen XP in het ${langName(lang).toLowerCase()}.`;
    const list = $("board-list");
    list.replaceChildren();
    const medal = { 1: "🥇", 2: "🥈", 3: "🥉" };
    if (!data.rows.length) {
      const li = document.createElement("li");
      li.className = "board-row empty";
      li.textContent = "Nog niemand heeft XP in deze taal.";
      list.append(li);
    }
    data.rows.forEach((r) => {
      const li = document.createElement("li");
      li.className = "board-row" + (r.you ? " you" : "");
      const mk = (cls, text) => { const s = document.createElement("span"); s.className = cls; s.textContent = text; return s; };
      li.append(
        mk("b-rank", r.xp > 0 && medal[r.rank] ? medal[r.rank] : String(r.rank)),
        mk("b-name", r.you ? `${r.name} (jij)` : r.name),
        mk("b-meta", `Lv ${r.level}${r.streak ? ` · 🔥 ${r.streak}` : ""}`),
        mk("b-xp", `${r.xp} XP`),
      );
      list.append(li);
    });
  }

  document.querySelectorAll(".seg-btn").forEach((b) => { b.onclick = () => loadBoard(b.dataset.period, state.boardLang); });
  $("board-lang").onchange = () => loadBoard(state.period, $("board-lang").value);

  // ------------------------------------------------------------ taalkeuze
  function initLanguages() {
    const sel = $("lang-select");
    sel.replaceChildren(...(state.config?.languages || []).map((l) => new Option(`${l.flag} ${l.name}`, l.code)));
    sel.value = state.config?.lang || "ko";
    sel.onchange = async () => {
      sel.disabled = true;
      try {
        const res = await api("/api/lang", { method: "PUT", json: { lang: sel.value } });
        if (!res.ok) throw new Error(res.status);
        state.practice = null;
        resetCard();
        await refreshConfig();
        await loadNext();
        if (!$("view-lessons").hidden) renderLessons();
      } catch (e) {
        toast("Taal wisselen mislukt.");
        sel.value = state.config?.lang || "ko";
      } finally { sel.disabled = false; }
    };
  }

  // ------------------------------------------------------------ instellingen
  function setMsg(text, bad) {
    const m = $("set-msg");
    m.textContent = text || "";
    m.hidden = !text;
    m.classList.toggle("bad", !!bad);
  }

  function showSettings(s) {
    $("set-user").textContent = s.name ? `Hoi ${s.name}` : "Instellingen";
    const src = { own: "Je gebruikt je eigen sleutel", shared: "Je gebruikt de sleutel van de eigenaar" }[s.azure_source];
    $("set-status").textContent = src || "Er is geen Azure-sleutel: luisteren en uitspraak beoordelen werken niet.";
    if (s.azure_region) $("set-region").value = s.azure_region;
    $("set-remove").hidden = s.azure_source !== "own";
    $("pw-section").hidden = !s.has_password;
    $("set-save").disabled = !s.can_store_key;
    if (!s.can_store_key) setMsg("Eigen sleutels opslaan staat uit op deze server (KEY_SECRET ontbreekt).", true);
  }

  // ---- spreektijd per maand
  function fmtTime(sec) {
    sec = Math.round(sec || 0);
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), r = sec % 60;
    if (h) return `${h} u ${String(m).padStart(2, "0")} min`;
    if (m) return `${m} min ${String(r).padStart(2, "0")} s`;
    return `${r} s`;
  }
  const monthKey = (d = new Date()) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
  function monthLabel(key) {
    const [y, m] = key.split("-").map(Number);
    return new Date(y, m - 1, 1).toLocaleDateString("nl-BE", { month: "long", year: "numeric" });
  }
  function shiftMonth(key, delta) {
    const [y, m] = key.split("-").map(Number);
    return monthKey(new Date(y, m - 1 + delta, 1));
  }
  const langLabel = (c) => { const l = state.config?.languages?.find((x) => x.code === c); return l ? `${l.flag} ${l.name}` : c; };

  function meterRow(name, sec, max, extra = "") {
    const row = document.createElement("div");
    row.className = "meter-row";
    const n = document.createElement("span");
    n.className = "m-name";
    n.textContent = name;
    if (extra) { const sm = document.createElement("small"); sm.textContent = ` ${extra}`; n.append(sm); }
    const bar = document.createElement("div");
    bar.className = "meter";
    const fill = document.createElement("i");
    fill.style.width = `${max ? Math.max(sec ? 3 : 0, Math.round((sec / max) * 100)) : 0}%`;
    bar.append(fill);
    const v = document.createElement("span");
    v.className = "m-val";
    v.textContent = fmtTime(sec);
    row.append(n, bar, v);
    return row;
  }

  function histList(el, details, items, current, onPick) {
    el.replaceChildren();
    const past = items.filter((h) => h.seconds > 0);
    details.hidden = past.length === 0;
    past.forEach((h) => {
      const li = document.createElement("li");
      li.className = "btnrow";
      const a = document.createElement("span"); a.textContent = monthLabel(h.month);
      const b = document.createElement("span"); b.textContent = fmtTime(h.seconds);
      li.append(a, b);
      li.onclick = () => onPick(h.month);
      el.append(li);
    });
  }

  async function loadUsage(month = state.usageMonth || monthKey()) {
    state.usageMonth = month;
    $("usage-month").textContent = monthLabel(month);
    $("usage-next").disabled = month >= monthKey();
    let u;
    try { u = await getJSON(`/api/usage?month=${month}`); } catch { $("usage-sub").textContent = "Spreektijd laden mislukt."; return; }
    if (u.month !== state.usageMonth) return; // ondertussen een andere maand gekozen
    $("usage-big").textContent = fmtTime(u.seconds);
    $("usage-sub").textContent = u.attempts
      ? `${u.attempts} ${u.attempts === 1 ? "poging" : "pogingen"} in ${monthLabel(month)}`
      : `Nog niets ingesproken in ${monthLabel(month)}.`;
    const langs = $("usage-langs");
    langs.replaceChildren();
    const entries = Object.entries(u.by_lang).sort((a, b) => b[1] - a[1]);
    entries.forEach(([c, sec]) => langs.append(meterRow(langLabel(c), sec, entries[0][1])));
    histList($("usage-hist-list"), $("usage-hist"), u.history, month, (m) => loadUsage(m));

    const adm = $("admin-card");
    adm.hidden = !(u.admin && u.all);
    if (adm.hidden) return;
    const a = u.all;
    $("admin-big").textContent = fmtTime(a.seconds);
    $("admin-sub").textContent = `${a.attempts} ${a.attempts === 1 ? "poging" : "pogingen"} door ${a.users.filter((x) => x.seconds > 0).length} van ${a.users.length} gebruikers · ${monthLabel(month)}`;
    const pct = a.azure_limit ? Math.min(100, Math.round((a.azure_estimate / a.azure_limit) * 100)) : 0;
    $("admin-meter").hidden = $("admin-meter-text").hidden = false;
    const fill = $("admin-meter-fill");
    fill.style.width = `${pct}%`;
    fill.className = pct >= 100 ? "full" : pct >= 80 ? "warn" : "";
    $("admin-meter-text").textContent = `Gedeelde sleutel: ${fmtTime(a.shared_seconds)} ingesproken, dat is ${fmtTime(a.azure_estimate)} van de ${fmtTime(a.azure_limit)} gratis Azure-tijd per maand.`;
    const box = $("admin-users");
    box.replaceChildren();
    const top = Math.max(1, ...a.users.map((x) => x.seconds));
    a.users.forEach((x) => box.append(meterRow(x.name, x.seconds, top, x.own > 0 && x.own >= x.seconds ? "eigen sleutel" : "")));
  }

  $("usage-prev").onclick = () => loadUsage(shiftMonth(state.usageMonth || monthKey(), -1));
  $("usage-next").onclick = () => loadUsage(shiftMonth(state.usageMonth || monthKey(), 1));

  async function loadSettings() {
    setMsg("");
    state.usageMonth = monthKey();
    loadUsage();
    $("set-logout").hidden = !store.get("hb_token");
    $("pw-msg").hidden = true;
    $("set-vad").checked = vadOn();
    $("set-wordwise").checked = wordwiseOn();
    try { showSettings(await getJSON("/api/settings")); } catch { setMsg("Instellingen laden mislukt.", true); }
  }

  async function refreshConfig() {
    try { state.config = await getJSON("/api/config"); } catch { /* blijft zoals het was */ }
  }

  $("set-vad").onchange = () => store.set("hb_vad", $("set-vad").checked ? "1" : "0");
  $("set-wordwise").onchange = () => store.set("hb_wordwise", $("set-wordwise").checked ? "1" : "0");

  $("set-form").onsubmit = async (e) => {
    e.preventDefault();
    const btn = $("set-save");
    btn.disabled = true;
    setMsg("Sleutel controleren…");
    try {
      const res = await api("/api/settings/azure", { method: "PUT", json: { key: $("set-key").value.trim(), region: $("set-region").value.trim() } });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) { setMsg(data.detail || "Opslaan mislukt.", true); return; }
      $("set-key").value = "";
      showSettings(data);
      await refreshConfig();
      setMsg("Opgeslagen. Vanaf nu gebruik je je eigen sleutel.");
    } finally { btn.disabled = false; }
  };

  $("set-remove").onclick = async () => {
    const res = await api("/api/settings/azure", { method: "DELETE" });
    if (!res.ok) { setMsg("Verwijderen mislukt.", true); return; }
    showSettings(await res.json());
    await refreshConfig();
    setMsg("Eigen sleutel verwijderd.");
  };

  $("pw-form").onsubmit = async (e) => {
    e.preventDefault();
    const m = $("pw-msg");
    const res = await api("/api/settings/password", { method: "PUT", json: { old: $("pw-old").value, new: $("pw-new").value } });
    const data = await res.json().catch(() => ({}));
    m.hidden = false;
    m.classList.toggle("bad", !res.ok);
    m.textContent = res.ok ? "Wachtwoord gewijzigd. Andere apparaten zijn uitgelogd." : (data.detail || "Wijzigen mislukt.");
    if (res.ok) { $("pw-old").value = ""; $("pw-new").value = ""; }
  };

  $("set-logout").onclick = async () => {
    try { await api("/api/logout", { method: "POST" }); } catch { /* ook zonder server uitloggen */ }
    try { localStorage.removeItem("hb_token"); } catch { /* niets */ }
    location.reload();
  };

  function press(id) {
    const b = $(id);
    b.classList.add("pressed");
    setTimeout(() => b.classList.remove("pressed"), 120);
  }

  // ------------------------------------------------------------ events
  $("btn-listen").onclick = () => listen();
  $("btn-speak").onclick = speak;
  $("btn-reveal").onclick = () => { press("btn-reveal"); reveal(); };
  $("note-close").onclick = () => { $("note-card").hidden = true; };
  $("btn-extra").onclick = () => loadNext(true);
  $("pr-next").onclick = nextPractice;
  $("pr-stop").onclick = stopPractice;
  $("lessons").addEventListener("click", (e) => {
    const l = e.target.closest("[data-listen]");
    if (l) {
      const it = state.lessons.flatMap((x) => x.items).find((x) => x.id === l.dataset.listen);
      if (it) listen(it);
      return;
    }
    const p = e.target.closest("[data-practice]");
    if (p && !p.disabled) startPractice(Number(p.dataset.practice));
  });
  document.querySelectorAll(".grade").forEach((b) => { b.onclick = () => grade(Number(b.dataset.grade)); });
  document.querySelectorAll(".tab").forEach((t) => { t.onclick = () => switchView(t.dataset.view); });
  $("btn-settings").onclick = () => switchView($("view-settings").hidden ? "settings" : (state.lastView || "practice"));

  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, dialog") || e.metaKey || e.ctrlKey || e.altKey) return;
    if ($("view-practice").hidden || typing()) return;
    if (e.key === " ") { e.preventDefault(); listen(); }
    else if (e.key === "r" || e.key === "R") speak();
    else if (e.key === "Enter") { e.preventDefault(); reveal(); }
    else if (state.revealed && !$("grades").hidden && ["1", "2", "3", "4"].includes(e.key)) grade([1, 3, 4, 5][Number(e.key) - 1]);
  });

  ["pointerdown", "keydown"].forEach((ev) => document.addEventListener(ev, () => { state.interacted = true; }, { once: true, capture: true }));
  ["touchend", "click", "keydown"].forEach((ev) => document.addEventListener(ev, unlockAudio, { capture: true, passive: true }));

  // Stemmen laden asynchroon in sommige browsers
  if ("speechSynthesis" in window) speechSynthesis.onvoiceschanged = () => {};

  (async () => {
    try { state.config = await getJSON("/api/config"); } catch { state.config = { azure: false }; }
    initAuto();
    initLanguages();
    applyMode();
    await loadNext();
  })();
})();
