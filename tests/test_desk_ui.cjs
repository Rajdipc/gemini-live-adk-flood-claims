/*
 * test_desk_ui.cjs - regression tests for the browser client (webapp/static/claim.js).
 *
 * Run:  node tests/test_desk_ui.cjs
 *
 * WHY THIS EXISTS
 *   The browser client has subtle lifecycle rules: a new claim must drop the
 *   old WebSocket and ignore its late messages, an interruption must silence
 *   every queued audio chunk, the camera on/off state has to be announced
 *   honestly, and known problems (mic blocked, call time limit, model
 *   unavailable) must show friendly notices. Those are easy to break and hard
 *   to notice by hand.
 *
 * HOW IT WORKS
 *   claim.js is loaded into a Node `vm` sandbox with fake browser primitives
 *   (document, WebSocket, AudioContext, fetch, sessionStorage). No real
 *   devices, network or DOM are used. `CLAIM_NO_BOOT` stops claim.js from
 *   wiring up the page automatically, so each test drives it directly.
 *   The pure helpers are also tested through `require()` (module.exports).
 */
"use strict";

const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");

const CLIENT_PATH = path.join(__dirname, "../webapp/static/claim.js");
const SOURCE = fs.readFileSync(CLIENT_PATH, "utf8");
const HTML = fs.readFileSync(path.join(__dirname, "../webapp/static/index.html"), "utf8");

const CONFIG = {
  brand_name: "Harbor Mutual",
  brand_tagline: "Here when the water rises.",
  agent_display_name: "Sam",
  claims_phone: "1-800-555-0199",
  supported_states: ["TX", "FL"],
  max_photos: 3,
  live_session_minutes: 15,
  camera_fps: 2,
  frame_max_age_seconds: 12,
  user: "ana@example.com",
};

/** A fake DOM element with just the properties claim.js touches. */
function fakeElement() {
  const attributes = {};
  const classes = new Set();
  return {
    textContent: "",
    className: "",
    innerHTML: "",
    value: "",
    href: "",
    hidden: false,
    disabled: false,
    open: false,
    style: { setProperty() {} },
    dataset: {},
    attributes,
    classes,
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
    },
    setAttribute(name, value) {
      attributes[name] = String(value);
    },
    getAttribute(name) {
      return attributes[name] ?? null;
    },
    querySelector() {
      if (!this._child) this._child = fakeElement();
      return this._child;
    },
    querySelectorAll() {
      return [];
    },
    addEventListener() {},
    focus() {},
    showModal() {
      this.open = true;
    },
    close() {
      this.open = false;
    },
  };
}

/** Build a fresh sandbox with claim.js loaded and a claim called "initial". */
function sandbox({ configResponse = CONFIG } = {}) {
  const elements = new Map();
  const sockets = [];
  const sources = [];
  const storage = new Map();
  const fetched = [];
  let counter = 0;

  class FakeSocket {
    static OPEN = 1;
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.sent = undefined;
      sockets.push(this);
      // Like the real server: say "ready" once the Live session is up.
      setImmediate(() => {
        if (this.closed || this.silent) return;
        this.readyState = 1;
        const intake = new URL(url).searchParams.get("intake_id");
        this.onmessage?.({ data: JSON.stringify({ type: "ready", intake_id: intake }) });
      });
    }
    send(raw) {
      this.sent ??= [];
      this.sent.push(JSON.parse(raw));
    }
    close() {
      this.closed = true;
      this.readyState = 3;
      this.onclose?.();
    }
  }

  class FakeAudio {
    constructor() {
      this.currentTime = 10;
      this.destination = {};
      this.sampleRate = 48000;
    }
    async resume() {}
    createBuffer(_channels, length, rate) {
      return { duration: length / rate, getChannelData: () => new Float32Array(length) };
    }
    createBufferSource() {
      const source = {
        stopped: false,
        connect(target) {
          this.target = target;
        },
        start(at) {
          this.startAt = at;
        },
        stop() {
          this.stopped = true;
        },
      };
      sources.push(source);
      return source;
    }
  }

  async function fakeFetch(url, options = {}) {
    fetched.push({ url, method: options.method || "GET" });
    if (url.endsWith("/api/config")) {
      if (configResponse instanceof Error) return { ok: false, status: 503, json: async () => ({ detail: "down" }) };
      return { ok: true, status: 200, json: async () => configResponse };
    }
    if (options.method === "DELETE") return { ok: true, status: 200, json: async () => ({ deleted: true }) };
    counter += 1;
    const id = `intake-${counter}`;
    const body = { intake_id: id, user: "ana@example.com", state: { intake_id: id, transcript: [], tool_activity: [] } };
    return { ok: true, status: 200, json: async () => body };
  }

  const context = vm.createContext({
    console,
    crypto,
    CLAIM_NO_BOOT: true,
    document: {
      title: "",
      documentElement: { dataset: {} },
      querySelector(selector) {
        if (!elements.has(selector)) elements.set(selector, fakeElement());
        return elements.get(selector);
      },
    },
    window: {
      location: { protocol: "http:", origin: "http://127.0.0.1:8080" },
      AudioContext: FakeAudio,
      addEventListener() {},
      setInterval() {
        return 1;
      },
      clearInterval() {},
      confirm: () => true,
    },
    navigator: {},
    sessionStorage: {
      getItem: (key) => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, value),
      removeItem: (key) => storage.delete(key),
    },
    WebSocket: FakeSocket,
    AudioContext: FakeAudio,
    fetch: fakeFetch,
    setTimeout,
    clearTimeout,
    atob: (s) => Buffer.from(s, "base64").toString("binary"),
    btoa: (s) => Buffer.from(s, "binary").toString("base64"),
  });
  vm.runInContext(SOURCE, context, { filename: "claim.js" });
  vm.runInContext('bindElements(); view = { ...blankView }; intakeId = "initial";', context);
  const el = (selector) => {
    if (!elements.has(selector)) elements.set(selector, fakeElement());
    return elements.get(selector);
  };
  return { sockets, sources, el, fetched, storage, run: (code) => vm.runInContext(code, context) };
}

const FAKE_CAMERA = `
  var tracksStopped = 0;
  var deviceEnded;
  var videoTrack = { stop() { tracksStopped++; }, addEventListener(name, fn) { if (name === "ended") deviceEnded = fn; } };
  navigator.mediaDevices = { getUserMedia: async () => ({ getTracks: () => [videoTrack], getVideoTracks: () => [videoTrack] }) };
`;

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

// ---- configuration + brand ------------------------------------------------------
test("brand, agent, phone and user come from /api/config", async () => {
  const e = sandbox();
  await e.run("startIntake(false)");
  assert.equal(e.el("#brandName").textContent, "Harbor Mutual");
  assert.equal(e.el("#brandTagline").textContent, "Here when the water rises.");
  assert.equal(e.el("#brandTagline").hidden, false);
  assert.equal(e.el("#claimsPhone").href, "tel:18005550199");
  assert.equal(e.el("#claimsLine").hidden, false);
  assert.equal(e.el("#userName").textContent, "ana@example.com");
  assert.equal(e.run("document.title"), "Harbor Mutual · Flood claim call");
  assert.equal(e.el("#evidenceCount").textContent, "0 of 3");
  assert(e.fetched.some((f) => f.url.endsWith("/api/config")));
});

test("a failed /api/config falls back to neutral wording", async () => {
  const e = sandbox({ configResponse: new Error("down") });
  await e.run("startIntake(false)");
  assert.equal(e.el("#brandName").textContent, "Flood claims");
  assert.equal(e.el("#claimsPhone").textContent, ""); // no phone number invented
  assert.equal(e.run("intakeId"), "intake-1"); // the claim still starts
});

test("no brand text is hard-coded in the page or the script", () => {
  for (const text of ["Demo Tideline", "Maya", "1-800-555-0142"]) {
    assert(!HTML.includes(text), `index.html contains ${text}`);
    assert(!SOURCE.includes(text), `claim.js contains ${text}`);
  }
  // unique ids (browser tests rely on them)
  const ids = [...HTML.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]);
  assert.equal(new Set(ids).size, ids.length, "duplicate id in index.html");
  assert.equal((HTML.match(/<h1[\s>]/g) || []).length, 1, "exactly one <h1>");
});

// ---- call lifecycle -----------------------------------------------------------
test("a fresh claim closes the old socket and ignores its late messages", async () => {
  const e = sandbox();
  await e.run("openCall()");
  const old = e.sockets[0];
  await e.run("startIntake(false)");
  old.onmessage({ data: JSON.stringify({ type: "transcript", intake_id: "initial", id: "late", speaker: "Claimant", text: "Old claim" }) });
  await e.run('sendTyped("Second claimant")');
  assert(old.closed);
  assert.equal(e.sockets.length, 2);
  assert.equal(e.run("view.transcript.length"), 1);
  assert.equal(old.sent, undefined);
  // The previous claim is deleted server-side before the new one is created.
  const writes = e.fetched.filter((f) => f.method !== "GET").map((f) => f.method);
  assert.deepEqual(writes, ["DELETE", "POST"]);
  assert.equal(e.storage.get("claimIntakeId"), "intake-1");
  e.run("hangUp()");
});

test("concurrent connects share one socket", async () => {
  const e = sandbox();
  await Promise.all([e.run("openCall()"), e.run("openCall()")]);
  assert.equal(e.sockets.length, 1);
  e.run("hangUp()");
});

test("reconnecting reuses the same claim", async () => {
  const e = sandbox();
  await e.run("openCall()");
  e.run("stopMic()");
  await e.run("openCall()");
  assert.equal(e.sockets[0].url, e.sockets[1].url);
  assert(e.sockets[0].url.startsWith("ws://127.0.0.1:8080/ws/live?intake_id=initial"));
  e.run("hangUp()");
});

test("ready starts the live status and the call clock", async () => {
  const e = sandbox();
  e.run("config = { ...config, live_session_minutes: 15 }");
  await e.run("openCall()");
  assert.equal(e.el("#callStatus").textContent, "Live");
  assert.equal(e.el("#callClock").hidden, false);
  assert.match(e.el("#callClock").textContent, /^0:00 \/ 15:00$/);
  e.run("hangUp()");
  assert.equal(e.el("#callClock").hidden, true);
});

test("an interruption stops every scheduled audio chunk", async () => {
  const e = sandbox();
  await e.run("openCall()");
  e.run('playChunk(btoa("\\0\\0".repeat(24000))); playChunk(btoa("\\0\\0".repeat(24000)))');
  assert.equal(e.sources[1].startAt, 11); // queued right after the first 1-second chunk
  e.sockets[0].onmessage({ data: JSON.stringify({ type: "interrupted", intake_id: "initial" }) });
  assert.equal(e.sources.filter((s) => s.stopped).length, 2);
  e.run("hangUp()");
});

test("starting a new claim stops queued speech", async () => {
  const e = sandbox();
  e.run('playChunk(btoa("\\0\\0".repeat(24000)))');
  await e.run("startIntake(false)");
  assert(e.sources[0].stopped);
});

test("a dropped socket releases mic and camera and offers to reconnect", async () => {
  const e = sandbox();
  await e.run("openCall()");
  e.run("var released = 0; listening = true; micStream = { getTracks: () => [{ stop() { released++; } }] }; lensStream = { getTracks: () => [{ stop() { released++; } }] };");
  e.sockets[0].close();
  assert.equal(e.run("released"), 2);
  assert.equal(e.run("listening"), false);
  assert.equal(e.run("lensStream"), null);
  assert.equal(e.el("#callNotice").hidden, false);
  assert.equal(e.el("#noticeTitle").textContent, "The call was disconnected");
  assert.equal(e.el("#noticeAction").textContent, "Reconnect");
  assert.equal(e.el("#callStatus").textContent, "Disconnected");
});

test("messages for another claim are ignored", async () => {
  const e = sandbox();
  await e.run("openCall()");
  e.sockets[0].onmessage({ data: JSON.stringify({ type: "transcript", intake_id: "someone-else", id: "x", speaker: "Agent", text: "Hi", final: true }) });
  assert.equal(e.run("view.transcript.length"), 0);
  e.run("hangUp()");
});

test("a Live reconnect shows Reconnecting… and keeps the call running", async () => {
  const e = sandbox();
  await e.run("openCall()");
  const ws = e.sockets[0];
  ws.onmessage({ data: JSON.stringify({ type: "status", code: "reconnecting", attempt: 1, intake_id: "initial" }) });
  assert.equal(e.el("#callStatus").textContent, "Reconnecting…");
  assert.equal(e.el("#srStatus").textContent, "Reconnecting…");
  assert.equal(e.el("#callStatus").className, "status-chip warning");
  assert(!ws.closed, "the browser socket stays open");
  assert.equal(e.el("#callClock").hidden, false); // the call clock keeps running
  assert.equal(e.el("#callNotice").hidden, true); // no "disconnected" notice
  ws.onmessage({ data: JSON.stringify({ type: "status", code: "resumed", location: "global", intake_id: "initial" }) });
  assert.equal(e.el("#callStatus").textContent, "Live");
  assert.equal(e.el("#callStatus").className, "status-chip live");
  assert.equal(e.run("view.transcript.length"), 0); // a status is not a caption
  e.run("hangUp()");
});

test("an unchanged status is not re-announced to screen readers", async () => {
  const e = sandbox();
  await e.run("openCall()");
  const sr = e.el("#srStatus");
  let writes = 0;
  let text = sr.textContent;
  Object.defineProperty(sr, "textContent", { get: () => text, set: (v) => { writes += 1; text = v; } });
  e.run('showStatus("Live", "live"); showStatus("Live", "live")');
  assert.equal(writes, 0); // it already said "Live"
  e.run('showStatus("Reconnecting…", "warning")');
  assert.equal(writes, 1);
  e.run("hangUp()");
});

test("an odd-length audio chunk is played without throwing", async () => {
  const e = sandbox();
  await e.run("openCall()");
  e.run('playChunk(btoa("\\0\\0\\0"))'); // 3 bytes: one sample + a stray byte
  assert.equal(e.sources.length, 1);
  e.run('playChunk(btoa("\\0"))'); // a single byte: nothing to play
  assert.equal(e.sources.length, 1);
  e.run("hangUp()");
});

// ---- friendly error states ----------------------------------------------------------
test("the call time limit shows a friendly notice instead of a raw error", async () => {
  const e = sandbox();
  e.run("config = { ...config, live_session_minutes: 15 }");
  await e.run("openCall()");
  e.sockets[0].onmessage({ data: JSON.stringify({ type: "error", intake_id: "initial", code: "time_limit", message: "Calls are limited to 15 minutes." }) });
  e.sockets[0].close();
  assert.equal(e.el("#noticeTitle").textContent, "Call time limit reached");
  assert.match(e.el("#noticeBody").textContent, /15 minutes\. Your claim is saved/);
  assert.equal(e.run("view.transcript.length"), 0); // not dumped into the captions
});

test("model unavailable before ready rejects the call with a notice", async () => {
  const e = sandbox();
  e.run("config = { ...config, agent_display_name: 'Sam' }");
  const pending = e.run("openCall()");
  e.sockets[0].silent = true; // never says ready
  e.sockets[0].onmessage({ data: JSON.stringify({ type: "error", intake_id: "initial", code: "model_unavailable", message: "unavailable" }) });
  await assert.rejects(pending);
  assert.equal(e.el("#noticeTitle").textContent, "Sam is briefly unavailable");
});

test("a blocked microphone explains how to fix it", async () => {
  const e = sandbox();
  e.run('navigator.mediaDevices = { getUserMedia: async () => { const error = new Error("Permission denied"); error.name = "NotAllowedError"; throw error; } }');
  await e.run("startMic()");
  assert.equal(e.el("#noticeTitle").textContent, "Your microphone is blocked");
  assert.equal(e.run("listening"), false);
  assert.equal(e.el("#callBtn").getAttribute("aria-pressed"), "false");
  e.run("hangUp()");
});

test("an unknown server error is shown as a caption notice", async () => {
  const e = sandbox();
  await e.run("openCall()");
  e.sockets[0].onmessage({ data: JSON.stringify({ type: "error", intake_id: "initial", message: "Workflow failed" }) });
  assert(e.el("#captions").innerHTML.includes("Workflow failed"));
  e.run("hangUp()");
});

test("an unreachable backend shows the offline notice", async () => {
  const e = sandbox();
  e.run("fetch = async () => { throw new Error('offline'); }");
  await e.run("startIntake(false)");
  assert.equal(e.el("#noticeTitle").textContent, "We can't reach the claims service");
  assert.equal(e.el("#callStatus").textContent, "Offline");
});

// ---- state merging and rendering ----------------------------------------------
test("state merge keeps final turns the snapshot does not have yet", () => {
  const e = sandbox();
  assert.equal(
    e.run('mergeTurns([{id:"1",speaker:"Agent",text:"Earlier reply"}],[{id:"2",speaker:"Claimant",text:"Correction",streaming:false}]).length'),
    2,
  );
});

test("short answers are distinct; a repeated final id is idempotent", () => {
  const e = sandbox();
  e.run('upsertTurn("Claimant","No injuries",true,"a"); upsertTurn("Claimant","No",true,"b"); upsertTurn("Claimant","No",true,"b")');
  assert.equal(e.run("view.transcript.length"), 2);
});

test("captions use the agent's configured name and escape HTML", () => {
  const e = sandbox();
  e.run("config = { ...config, agent_display_name: 'Sam' }");
  e.run('upsertTurn("Agent", "Hi, this is Sam", true, "g"); upsertTurn("Claimant", "<img src=x onerror=alert(1)>", true, "xss")');
  const html = e.el("#captions").innerHTML;
  assert(html.includes('<span class="caption-who">Sam</span>'));
  assert(html.includes('class="caption you"'));
  assert(!html.includes("<img"));
  assert(html.includes("&lt;img"));
});

test("background work shows a status line and clears when finished", () => {
  const e = sandbox();
  e.run('view = { ...blankView, tool_activity: [{ id: "1", name: "find_policy", phase: "running" }] }; paint()');
  assert.equal(e.el("#syncState").textContent, "Checking your policy…");
  assert.equal(e.el("#syncState").hidden, false);
  for (const phase of ["done", "error", "cancelled"]) {
    e.run(`view = { ...blankView, tool_activity: [{ id: "1", name: "refresh_intake_packet", phase: "${phase}" }] }; busy = false; paint()`);
    assert.equal(e.el("#syncState").hidden, true);
  }
});

test("an error message releases the busy state", async () => {
  const e = sandbox();
  await e.run("openCall()");
  e.run("busy = true; paint()");
  assert.equal(e.el("#syncState").hidden, false);
  e.sockets[0].onmessage({ data: JSON.stringify({ type: "error", intake_id: "initial", message: "Workflow failed" }) });
  assert.equal(e.el("#syncState").hidden, true);
  e.run("hangUp()");
});

test("documents are labelled honestly; only received ones are ticked", () => {
  const e = sandbox();
  e.run(`view = { ...blankView, documents: [
    { item: "Photo", status: "available", already_provided: false, priority: "required" },
    { item: "Estimate", status: "unknown", already_provided: false, priority: "recommended" },
    { item: "Deed", status: "received", already_provided: true, priority: "required" }] }; paintDocs()`);
  const html = e.el("#docList").innerHTML;
  assert(html.includes('class="doc available"') && html.includes("You have it"));
  assert(html.includes("Helpful"));
  assert.equal((html.match(/class="doc done"/g) || []).length, 1);
});

test("an unconfirmed photo is labelled; bytes come through the app", () => {
  const e = sandbox();
  e.run(`view = { ...blankView, evidence_photos: [
    { id: "p", caption: "Wall", url: "/api/intakes/initial/evidence/p", confirmed: false, claimant_description: "water line" }] }; paintEvidence()`);
  const html = e.el("#evidenceGrid").innerHTML;
  assert(html.includes("Claim not confirmed by this image"));
  assert(html.includes('src="/api/intakes/initial/evidence/p"')); // not a signed URL
});

test("the sketch card appears with an honest caption", () => {
  const e = sandbox();
  e.run('view = { ...blankView, sketch: { version: 2, url: "/api/intakes/initial/sketch?v=2" } }; paintSketch()');
  assert.equal(e.el("#sketchCard").hidden, false);
  assert.equal(e.el("#sketchImage").src, "/api/intakes/initial/sketch?v=2");
  assert.match(e.el("#sketchCaption").textContent, /not a photo/);
});

test("the download link is enabled once the claimant has spoken", () => {
  const e = sandbox();
  e.run("paint()");
  assert.equal(e.el("#downloadBtn").href, "http://127.0.0.1:8080/api/intakes/initial/packet.zip");
  assert.equal(e.el("#downloadBtn").getAttribute("aria-disabled"), "true");
  e.run('upsertTurn("Claimant", "Our street flooded", true, "c1")');
  assert.equal(e.el("#downloadBtn").getAttribute("aria-disabled"), "false");
});

test("progress feeds the bar, the label and the mobile tab badge", () => {
  const e = sandbox();
  e.run("commit({ ...blankView, progress: 45 })");
  assert.equal(e.el("#progressFill").style.width, "45%");
  assert.equal(e.el("#progressValue").textContent, "45%");
  assert.equal(e.el("#tabClaimBadge").textContent, "45%");
  assert.equal(e.el("#progressBar").getAttribute("aria-valuenow"), "45");
});

test("the photo limit from config is enforced before uploading", () => {
  const e = sandbox();
  e.run('config = { ...config, max_photos: 1 }; view = { ...blankView, evidence_photos: [{ id: "a" }] }');
  assert.equal(e.run("photoLimitReached()"), true);
  assert(e.el("#captions").innerHTML.includes("already has 1 photos"));
});

// ---- camera honesty -----------------------------------------------------------------
test("turning the camera on and off sends explicit mode changes", async () => {
  const e = sandbox();
  e.run(FAKE_CAMERA);
  await e.run("startLens()");
  assert.deepEqual(e.sockets[0].sent, [{ type: "camera_state", enabled: true }]);
  assert.equal(e.el("#lensStage").hidden, false);
  assert.equal(e.el("#lensBtn").getAttribute("aria-pressed"), "true");
  e.run("stopLens()");
  assert.deepEqual(e.sockets[0].sent.at(-1), { type: "camera_state", enabled: false });
  assert.equal(e.run("tracksStopped"), 1);
  assert.equal(e.el("#lensStage").hidden, true);
  e.run("hangUp()");
});

test("the camera device ending turns camera mode off", async () => {
  const e = sandbox();
  e.run(FAKE_CAMERA);
  await e.run("startLens()");
  e.run("deviceEnded()");
  assert.deepEqual(e.sockets[0].sent.at(-1), { type: "camera_state", enabled: false });
  assert.equal(e.run("lensStream"), null);
  e.run("hangUp()");
});

test("a denied camera never announces camera on", async () => {
  const e = sandbox();
  e.run('navigator.mediaDevices = { getUserMedia: async () => { throw new Error("Permission denied"); } }');
  await e.run("startLens()");
  assert.equal(e.sockets[0].sent, undefined);
  assert.equal(e.run("lensStream"), null);
  assert.equal(e.el("#noticeTitle").textContent, "Your camera is blocked");
  e.run("hangUp()");
});

// ---- pure helpers via module.exports ----------------------------------------------
const client = require(CLIENT_PATH);

test("escapeHtml escapes all HTML-significant characters", () => {
  assert.equal(client.escapeHtml(`<a href="x">'&'</a>`), "&lt;a href=&quot;x&quot;&gt;&#039;&amp;&#039;&lt;/a&gt;");
  assert.equal(client.escapeHtml(null), "");
});

test("neededLabel and statesText read naturally", () => {
  assert.equal(client.neededLabel("policy_number"), "Flood policy number");
  assert.equal(client.neededLabel("proof_of_ownership (deed)"), "Proof of ownership");
  assert.equal(client.statesText(["CO", "TX", "FL"]), "CO, TX and FL");
  assert.equal(client.statesText([]), "supported states");
});

test("stepStates walks Safety -> Policy -> What happened -> Evidence -> Review", () => {
  const blank = client.stepStates({ transcript: [] });
  assert.deepEqual(
    blank.map((s) => s.label),
    ["Safety", "Policy", "What happened", "Evidence", "Review"],
  );
  assert(blank.every((s) => s.status === "todo"));
  assert.equal(blank.find((s) => s.current).key, "safety");

  const mid = client.stepStates({
    transcript: [{ speaker: "Claimant", text: "Hi" }],
    fields: {
      safety: { value: "No injuries or hazards reported", status: "complete" },
      policy: { value: "FLD-TX-7Q2K9M", status: "complete" },
      description: { value: "Bayou flooded", status: "complete" },
    },
    policy: { found: true, status: "active" },
    documents: [{ priority: "required", already_provided: false }],
    evidence_photos: [{ id: "p" }],
    route: "needs_docs",
  });
  const byKey = Object.fromEntries(mid.map((s) => [s.key, s]));
  assert.equal(byKey.safety.status, "done");
  assert.equal(byKey.policy.status, "done");
  assert.equal(byKey.story.status, "partial");
  assert.equal(byKey.evidence.status, "partial");
  assert.equal(byKey.story.current, true);

  const lapsed = client.stepStates({ transcript: [{ speaker: "Claimant", text: "x" }], fields: { safety: { value: "Gas smell", status: "urgent" } }, policy: { found: true, status: "expired" } });
  assert.equal(lapsed[0].status, "attention");
  assert.equal(lapsed[1].status, "attention");
});

test("nextStepFor never promises coverage and uses the agent's name", () => {
  const cfg = { agent_display_name: "Sam" };
  assert.match(client.nextStepFor({ transcript: [] }, cfg).body, /Sam will guide you/);
  const spoke = [{ speaker: "Claimant", text: "hi" }];
  for (const route of ["emergency_escalation", "policy_review", "needs_docs", "special_investigation", "human_triage", "ready_for_adjuster"]) {
    const next = client.nextStepFor({ transcript: spoke, route }, cfg);
    assert(next.title && next.body && next.tone, route);
    assert(!/\b(approved|covered|you will be paid|guarantee)/i.test(next.body), `${route} promises coverage`);
    assert(!next.body.includes("{agent}"));
  }
  assert.match(client.nextStepFor({ transcript: spoke, route: "emergency_escalation" }, cfg).body, /911/);
  assert.equal(client.nextStepFor({ transcript: spoke, route: "ready_for_adjuster" }, cfg).tone, "success");
});

test("keyFacts hides optional facts until known and flags urgent ones", () => {
  const rows = client.keyFacts({ fields: { waterDepth: { value: "8 inches", status: "complete" }, safety: { value: "Live wires in water", status: "urgent" } } });
  const keys = rows.map((r) => r.key);
  assert(keys.includes("waterDepth") && !keys.includes("floodZone"));
  assert.equal(rows.find((r) => r.key === "safety").status, "urgent");
  assert.equal(rows.find((r) => r.key === "claimant").status, "pending");
});

test("docStatus and photoBadge stay honest", () => {
  assert.deepEqual(client.docStatus({ already_provided: true }), { label: "Received", tone: "done" });
  assert.equal(client.docStatus({ status: "unknown", priority: "required" }).label, "Needed");
  assert.equal(client.photoBadge({ confirmed: true }).tone, "ok");
  assert.equal(client.photoBadge({ confirmed: false, claimant_description: "mud" }).label, "Claim not confirmed by this image");
  assert.equal(client.photoBadge({ confirmed: false, verified: false }).label, "Saved · awaiting review");
});

test("formatClock, activityText and noticeFor", () => {
  assert.equal(client.formatClock(0), "0:00");
  assert.equal(client.formatClock(1199.9), "19:59");
  assert.equal(client.activityText({ tool_activity: [{ name: "render_damage_sketch", phase: "running" }] }, false), "Drawing a sketch…");
  assert.equal(client.activityText({ tool_activity: [] }, true), "Updating your claim…");
  assert.equal(client.activityText({ tool_activity: [] }, false), "");
  assert.match(client.noticeFor("time_limit", { live_session_minutes: 7 }).body, /7 minutes/);
  assert.equal(client.noticeFor("nope", {}), null);
});

test("toPcm16 downsamples and clamps; bytesToBase64 encodes", () => {
  const pcm = client.toPcm16(new Float32Array([0, 0.5, 1, 2, -1, -2]), 48000, 16000);
  assert.equal(pcm.length, 2);
  assert.equal(pcm[0], 0);
  assert.equal(pcm[1], 0x7fff); // 2.0 is clamped to 1.0
  assert.equal(client.bytesToBase64(new Uint8Array([104, 105]).buffer), "aGk=");
});

test("mergeActivity prefers a local finished phase over a stale running one", () => {
  const merged = client.mergeActivity([{ id: "1", phase: "running" }, { id: "2", phase: "done" }], [{ id: "1", phase: "done" }, { id: "2", phase: "running" }]);
  assert.deepEqual(
    merged.map((m) => m.phase),
    ["done", "done"],
  );
});

test("tool activity is capped like the server's list", () => {
  const many = Array.from({ length: 40 }, (_, i) => ({ id: String(i), phase: "done" }));
  const merged = client.mergeActivity(many, []);
  assert.equal(merged.length, 30);
  assert.equal(merged[0].id, "10"); // the oldest entries are dropped
  assert.equal(merged.at(-1).id, "39");
});

test("liveStatusText and pcmSamples", () => {
  assert.deepEqual({ ...client.liveStatusText("reconnecting") }, { text: "Reconnecting…", tone: "warning" });
  assert.deepEqual({ ...client.liveStatusText("resumed") }, { text: "Live", tone: "live" });
  assert.equal(client.liveStatusText("whatever"), null);
  assert.equal(client.pcmSamples(new Uint8Array([1, 0, 2, 0, 9])).length, 2);
  assert.equal(client.pcmSamples(new Uint8Array([7])).length, 0);
});

(async () => {
  for (const [name, fn] of tests) {
    await fn();
    console.log(`PASS ${name}`);
  }
  console.log(`${tests.length} claim UI tests passed`);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
