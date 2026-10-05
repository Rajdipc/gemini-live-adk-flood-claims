/*
 * claim.js - browser side of the policyholder "claim call" page.
 *
 * WHAT THIS FILE DOES
 *   1. Loads the brand + limits from GET /api/config (env vars on the server),
 *      so no company name, agent name or phone number is hard-coded here.
 *   2. Creates (or resumes) a claim intake via REST:   POST /api/intakes
 *   3. Opens the live call WebSocket:                  /ws/live?intake_id=...
 *        - microphone -> 16 kHz 16-bit PCM chunks     -> {type:"audio"}
 *        - camera     -> JPEG frames (camera_fps)     -> {type:"video"}
 *        - typed text                                 -> {type:"text"}
 *        - camera on/off notices                      -> {type:"camera_state"}
 *      and plays the agent's 24 kHz PCM speech back.
 *   4. Renders "Your claim, as we build it": steps, key facts, documents,
 *      photos, sketch and a claimant-friendly "what happens next" card.
 *
 * The WebSocket/REST protocol is exactly the one webapp/main.py and
 * webapp/live_bridge.py implement; nothing here needs a newer backend
 * (optional fields such as an error `code` are used when present).
 *
 * AUTH: on Cloud Run the page is behind Identity-Aware Proxy (IAP). The
 * browser's IAP cookie rides along on every same-origin request and on the
 * WebSocket upgrade, so this script never handles tokens.
 *
 * TESTABILITY: nothing touches the DOM at load time. `boot()` runs only in a
 * real page (tests set `CLAIM_NO_BOOT`), and pure helpers are exported for
 * Node at the bottom (tests/test_desk_ui.cjs).
 */
"use strict";

const HAS_DOM = typeof document !== "undefined";
const PAGE_ORIGIN = typeof window !== "undefined" && window.location ? window.location.origin : "";
const SOCKET_ORIGIN = PAGE_ORIGIN.replace(/^http/, "ws");
const FRAME_WIDTH = 512; // camera frames streamed to the model (small = fast)
const UPLOAD_MAX_SIDE = 1600; // photos saved as evidence (sharper)
const STORAGE_KEY = "claimIntakeId";
const THEME_KEY = "claimTheme";
// Same cap as the server (record.tool_activity[-30:]): a long call must not
// grow the activity list (and the page's memory) without limit.
const MAX_ACTIVITY = 30;

/** Neutral fallbacks, used until /api/config answers (or if it fails). */
const DEFAULT_CONFIG = Object.freeze({
  brand_name: "Flood claims",
  brand_tagline: "",
  agent_display_name: "your claims agent",
  claims_phone: "",
  supported_states: [],
  max_photos: 20,
  live_session_minutes: 20,
  camera_fps: 1,
  frame_max_age_seconds: 12,
  user: "",
});

const SELECTORS = {
  brandName: "#brandName",
  brandTagline: "#brandTagline",
  claimsLine: "#claimsLine",
  claimsPhone: "#claimsPhone",
  userChip: "#userChip",
  userName: "#userName",
  userAvatar: "#userAvatar",
  themeBtn: "#themeBtn",
  newClaimBtn: "#newClaimBtn",
  callStatus: "#callStatus",
  callClock: "#callClock",
  orb: "#orb",
  orbCanvas: "#orbCanvas",
  orbLabel: "#orbLabel",
  callBtn: "#callBtn",
  lensBtn: "#lensBtn",
  uploadBtn: "#uploadBtn",
  picker: "#photoPicker",
  notice: "#callNotice",
  noticeTitle: "#noticeTitle",
  noticeBody: "#noticeBody",
  noticeAction: "#noticeAction",
  noticeDismiss: "#noticeDismiss",
  lensStage: "#lensStage",
  lensPreview: "#lensPreview",
  captureBtn: "#captureBtn",
  grabber: "#frameGrabber",
  captions: "#captions",
  srCaptions: "#srCaptions",
  typeForm: "#typeForm",
  typeInput: "#typeInput",
  syncState: "#syncState",
  progressBar: "#progressBar",
  progressFill: "#progressFill",
  progressValue: "#progressValue",
  stepper: "#stepper",
  nextStep: "#nextStep",
  nextTitle: "#nextTitle",
  nextBody: "#nextBody",
  factList: "#factList",
  docList: "#docList",
  evidenceGrid: "#evidenceGrid",
  evidenceCount: "#evidenceCount",
  sketchCard: "#sketchCard",
  sketchImage: "#sketchImage",
  sketchCaption: "#sketchCaption",
  downloadBtn: "#downloadBtn",
  previewPacketBtn: "#previewPacketBtn",
  photoDialog: "#photoDialog",
  photoForm: "#photoForm",
  photoPreview: "#photoPreview",
  photoNote: "#photoNote",
  photoCancelBtn: "#photoCancelBtn",
  viewerDialog: "#viewerDialog",
  viewerImage: "#viewerImage",
  viewerCaption: "#viewerCaption",
  viewerCloseBtn: "#viewerCloseBtn",
  packetDialog: "#packetDialog",
  packetText: "#packetText",
  packetCloseBtn: "#packetCloseBtn",
  packetDownloadBtn: "#packetDownloadBtn",
  tabCall: "#tabCall",
  tabClaim: "#tabClaim",
  tabClaimBadge: "#tabClaimBadge",
  srStatus: "#srStatus",
};
const ui = {};

// ---- mutable page state ----------------------------------------------------
let config = { ...DEFAULT_CONFIG };
let socket = null; // the open WebSocket (or null)
let socketReady = null; // Promise resolved when the server says "ready"
let epoch = 0; // bumps on every hang-up; stale callbacks compare against it
let busy = false; // the claim team is working on an update
let micStarting = false;
let lensStarting = false;
let restarting = false;
const playing = new Set(); // scheduled agent audio (so an interruption can silence it)
let audioCtx = null;
let agentBus = null; // gain node all agent audio flows through (feeds the orb)
let agentAnalyser = null;
let micAnalyser = null;
let micNode = null;
let micSource = null;
let micStream = null;
let lensStream = null;
let frameTimer = null;
let listening = false;
let playhead = 0; // when the next audio chunk should start
let intakeId = null;
let view = null; // last rendered claim state
let activeNotice = null; // code of the notice on screen (or null)
let callStartedAt = 0;
let clockTimer = null;
let orbFrame = 0;
let orbPalette = null;
let pendingPhoto = null; // {blob, url} waiting in the photo dialog
const seenFacts = new Set(); // fact values already shown (for the fill-in effect)
const announced = new Set(); // turn ids already read to screen readers

const blankView = {
  route: "needs_docs",
  progress: 0,
  fields: {},
  transcript: [],
  tool_activity: [],
  missing_blockers: [],
  documents: [],
  evidence_photos: [],
  sketch: null,
  policy: null,
  packet_markdown: "# Flood claim packet\n\nNothing yet.",
};

// ---- claimant-facing copy -------------------------------------------------------
const NEEDED_LABELS = {
  policyholder_name: "Name on the policy",
  policy_number: "Flood policy number",
  contact_method: "Best phone or email",
  date_of_loss: "When the water came in",
  loss_address_or_city: "Property address",
  loss_description: "What happened",
};

const STEPS = [
  { key: "safety", label: "Safety" },
  { key: "policy", label: "Policy" },
  { key: "story", label: "What happened" },
  { key: "evidence", label: "Evidence" },
  { key: "review", label: "Review" },
];

const STEP_STATUS_TEXT = { done: "complete", partial: "in progress", attention: "needs attention", todo: "not started" };

// Routing decisions from the claim pipeline, phrased for the policyholder.
// Deliberately no coverage promises: an adjuster decides that later.
const NEXT_STEPS = {
  emergency_escalation: {
    tone: "danger",
    title: "Your safety comes first",
    body: "If anyone is hurt or in danger, call 911 or leave the building now. A person on our team will review your claim urgently.",
  },
  policy_review: {
    tone: "warning",
    title: "We'll double-check your policy",
    body: "A specialist will confirm your policy details. Keep going; everything you tell {agent} is saved.",
  },
  needs_docs: {
    tone: "info",
    title: "We're putting your claim together",
    body: "Keep talking with {agent}. The facts and checklist below fill in as you go.",
  },
  special_investigation: {
    tone: "info",
    title: "A specialist will review a few details",
    body: "This is a routine step, not a decision on your claim. You can keep adding details and photos.",
  },
  human_triage: {
    tone: "info",
    title: "A colleague will follow up",
    body: "What you described may belong with a different type of policy. A colleague will review it with you; nothing has been decided.",
  },
  ready_for_adjuster: {
    tone: "success",
    title: "Ready for an adjuster",
    body: "You've shared what we need to start. Download your claim packet for your records. An adjuster reviews coverage; nothing has been decided yet.",
  },
};

const FACT_ROWS = [
  { key: "claimant", label: "Policyholder", required: true },
  { key: "policy", label: "Policy number", required: true },
  { key: "policyStatus", label: "Policy status" },
  { key: "contact", label: "Contact", required: true },
  { key: "date", label: "Date of loss", required: true },
  { key: "location", label: "Property", required: true },
  { key: "description", label: "What happened", required: true },
  { key: "waterEntry", label: "How water got in" },
  { key: "waterSource", label: "Water source" },
  { key: "waterDepth", label: "Water depth" },
  { key: "estimate", label: "Estimated loss" },
  { key: "safety", label: "Safety", required: true },
  { key: "floodZone", label: "Flood zone" },
];

const ACTIVITY_TEXT = {
  find_policy: "Checking your policy…",
  refresh_intake_packet: "Updating your claim…",
  capture_evidence_photo: "Saving a photo…",
  render_damage_sketch: "Drawing a sketch…",
  lookup_flood_guidance: "Checking FEMA guidance…",
};

const NOTICE_CODES = new Set(["time_limit", "model_unavailable", "connection_lost", "mic_denied", "mic_unsupported", "camera_denied", "backend"]);

// ---- pure helpers (exported for tests) --------------------------------------
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function hasValue(field) {
  if (!field || field.status === "missing") return false;
  return !/^(missing:|not captured|not specified|unknown$|none)/i.test(String(field.value || "").trim());
}

function agentName(cfg) {
  return (cfg && cfg.agent_display_name) || DEFAULT_CONFIG.agent_display_name;
}

function neededLabel(blocker) {
  if (NEEDED_LABELS[blocker]) return NEEDED_LABELS[blocker];
  let text = String(blocker).replace(/\s*\([^)]*\)/g, "").replaceAll("_", " ").trim();
  if (text.length > 42) text = `${text.slice(0, 40).trim()}…`;
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** "CO, TX, FL" style list for the disclaimer. */
function statesText(codes) {
  const list = (codes || []).filter(Boolean);
  if (!list.length) return "supported states";
  return list.length === 1 ? list[0] : `${list.slice(0, -1).join(", ")} and ${list.at(-1)}`;
}

/** Keep server turns, plus local final turns the server snapshot does not have yet. */
function mergeTurns(authoritative, local) {
  const byId = new Map((authoritative || []).map((turn) => [turn.id, turn]));
  for (const turn of local || []) {
    if (!byId.has(turn.id)) byId.set(turn.id, turn);
  }
  return [...byId.values()];
}

/** Server activity wins, unless our local copy already knows the tool finished. */
function mergeActivity(authoritative, local) {
  const finished = new Set(["done", "error", "cancelled"]);
  const byId = new Map();
  for (const item of authoritative || []) byId.set(item.id, item);
  for (const item of local || []) {
    const current = byId.get(item.id);
    if (!current || (finished.has(item.phase) && !finished.has(current.phase))) byId.set(item.id, item);
  }
  return [...byId.values()].slice(-MAX_ACTIVITY);
}

/**
 * Chip text/tone for a Live connection status from the server.
 * "reconnecting": the server is moving the call to a fresh Gemini Live
 * connection (routine, ~every 10 minutes); mic, camera and clock keep going.
 * "resumed": the call is back. Returns null for unknown codes.
 */
function liveStatusText(code) {
  if (code === "reconnecting") return { text: "Reconnecting…", tone: "warning" };
  if (code === "resumed") return { text: "Live", tone: "live" };
  return null;
}

/**
 * Only touch textContent when the text really changes. WHY: #srStatus,
 * #syncState and #nextStep are aria-live regions; rewriting the same text
 * makes some screen readers announce it again on every repaint.
 */
function setText(el, text) {
  const value = String(text ?? "");
  if (el && el.textContent !== value) el.textContent = value;
}

/**
 * Turn raw audio bytes into 16-bit samples. A chunk with an odd byte count
 * (truncated in transit) would make `new Int16Array(buffer)` throw, so the
 * last half-sample is dropped instead.
 */
function pcmSamples(bytes) {
  return new Int16Array(bytes.buffer, bytes.byteOffset, Math.floor(bytes.byteLength / 2));
}

/** Resample float32 mic samples to 16-bit PCM at `outputRate` (linear interpolation). */
function toPcm16(input, inputRate, outputRate) {
  const ratio = inputRate / outputRate;
  const outputLength = Math.floor(input.length / ratio);
  const pcm = new Int16Array(outputLength);
  for (let i = 0; i < outputLength; i += 1) {
    const index = i * ratio;
    const before = Math.floor(index);
    const after = Math.min(before + 1, input.length - 1);
    const weight = index - before;
    const sample = Math.max(-1, Math.min(1, input[before] * (1 - weight) + input[after] * weight));
    pcm[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
  }
  return pcm;
}

function bytesToBase64(buffer) {
  let binary = "";
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.byteLength; i += 1) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function claimantHasSpoken(state) {
  return ((state && state.transcript) || []).some((turn) => turn.speaker === "Claimant" && String(turn.text || "").trim());
}

/** Status of each step (done | partial | attention | todo) and which one is current. */
function stepStates(state) {
  const f = state.fields || {};
  const docs = state.documents || [];
  const photos = state.evidence_photos || [];
  const status = {};

  status.safety = f.safety && f.safety.status === "urgent" ? "attention" : hasValue(f.safety) ? "done" : "todo";

  const policy = state.policy;
  if (policy && policy.found && policy.status === "active") status.policy = "done";
  else if (policy) status.policy = "attention"; // not found, expired or cancelled
  else status.policy = hasValue(f.policy) ? "partial" : "todo";

  const story = ["description", "date", "location"].filter((key) => hasValue(f[key])).length;
  status.story = story === 3 ? "done" : story ? "partial" : "todo";

  const required = docs.filter((doc) => doc.priority === "required");
  const requiredDone = required.length ? required.every((doc) => doc.already_provided) : false;
  status.evidence = requiredDone ? "done" : photos.length || docs.some((doc) => doc.already_provided) ? "partial" : "todo";

  status.review = state.route === "ready_for_adjuster" ? "done" : state.route === "emergency_escalation" ? "attention" : "todo";

  const spoken = claimantHasSpoken(state);
  const current = spoken ? (STEPS.find((step) => status[step.key] !== "done") || STEPS.at(-1)).key : "safety";
  return STEPS.map((step) => ({ ...step, status: status[step.key], current: step.key === current }));
}

/** The "What happens next" card, phrased for the policyholder. */
function nextStepFor(state, cfg) {
  const agent = agentName(cfg);
  if (!claimantHasSpoken(state)) {
    return {
      tone: "neutral",
      title: "Start whenever you're ready",
      body: `Tap “Start claim call” and ${agent} will guide you. You can also type, or show the damage on camera.`,
    };
  }
  const base = NEXT_STEPS[state.route] || NEXT_STEPS.needs_docs;
  return { ...base, body: base.body.replaceAll("{agent}", agent) };
}

/** Rows for the "Key facts" list; optional facts only appear once known. */
function keyFacts(state) {
  const f = state.fields || {};
  return FACT_ROWS.map((row) => {
    const field = f[row.key];
    const filled = hasValue(field);
    return {
      key: row.key,
      label: row.label,
      required: Boolean(row.required),
      value: filled ? String(field.value) : "",
      status: filled ? (field.status === "urgent" ? "urgent" : "filled") : "pending",
    };
  }).filter((row) => row.status !== "pending" || row.required);
}

/** Label + tone for one checklist document. */
function docStatus(doc) {
  if (doc.already_provided || doc.status === "received") return { label: "Received", tone: "done" };
  if (doc.status === "available") return { label: "You have it", tone: "available" };
  if (doc.status === "planned") return { label: "Planned", tone: "available" };
  return doc.priority === "required" ? { label: "Needed", tone: "needed" } : { label: "Helpful", tone: "optional" };
}

/** Honest badge for a saved photo (never claims more than the image check did). */
function photoBadge(photo) {
  if (photo.confirmed) return { label: "Supports what you described", tone: "ok" };
  if (photo.claimant_description) return { label: "Claim not confirmed by this image", tone: "unconfirmed" };
  if (photo.verified === false) return { label: "Saved · awaiting review", tone: "unconfirmed" };
  return { label: "Saved · adjuster will review", tone: "neutral" };
}

function formatClock(totalSeconds) {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

/** Short status line while background helpers work ("" when idle). */
function activityText(state, isBusy) {
  const running = (state.tool_activity || []).filter((item) => item.phase === "running");
  if (running.length) return ACTIVITY_TEXT[running.at(-1).name] || "Working on your claim…";
  return isBusy ? "Updating your claim…" : "";
}

/** Friendly copy for known problems (codes come from the server or the browser). */
function noticeFor(code, cfg) {
  const agent = agentName(cfg);
  const cap = agent.charAt(0).toUpperCase() + agent.slice(1);
  const minutes = (cfg && cfg.live_session_minutes) || DEFAULT_CONFIG.live_session_minutes;
  const notices = {
    time_limit: {
      tone: "info",
      title: "Call time limit reached",
      body: `Calls are limited to ${minutes} minutes. Your claim is saved; reconnect to continue where you left off.`,
      action: "Reconnect",
    },
    model_unavailable: {
      tone: "warning",
      title: `${cap} is briefly unavailable`,
      body: "Your claim is saved. Try again in a moment, or type your message below.",
      action: "Try again",
    },
    connection_lost: {
      tone: "warning",
      title: "The call was disconnected",
      body: "Your claim is saved. Reconnect to keep going.",
      action: "Reconnect",
    },
    mic_denied: {
      tone: "warning",
      title: "Your microphone is blocked",
      body: `Allow microphone access for this site (look for the icon in the address bar), then try again. You can also type to ${agent}.`,
      action: "Try again",
    },
    mic_unsupported: {
      tone: "info",
      title: "Voice isn't available in this browser",
      body: `You can still type to ${agent} below and add photos.`,
      action: null,
    },
    camera_denied: {
      tone: "warning",
      title: "Your camera is blocked",
      body: "Allow camera access for this site to show the damage, or use “Add photo” instead.",
      action: null,
    },
    backend: {
      tone: "danger",
      title: "We can't reach the claims service",
      body: "Check your connection and try again. Nothing you've shared is lost.",
      action: "Try again",
    },
  };
  return notices[code] || null;
}

// ---- rendering ------------------------------------------------------------------
function bindElements() {
  for (const [key, selector] of Object.entries(SELECTORS)) ui[key] = document.querySelector(selector);
}

function fillAll(selector, text) {
  if (typeof document.querySelectorAll !== "function") return;
  for (const el of document.querySelectorAll(selector)) el.textContent = text;
}

function applyConfig(next) {
  config = { ...DEFAULT_CONFIG, ...(next || {}) };
  ui.brandName.textContent = config.brand_name;
  ui.brandTagline.textContent = config.brand_tagline;
  ui.brandTagline.hidden = !config.brand_tagline;
  if (config.claims_phone) {
    ui.claimsPhone.textContent = config.claims_phone;
    ui.claimsPhone.href = `tel:${config.claims_phone.replace(/[^0-9+]/g, "")}`;
    ui.claimsLine.hidden = false;
  }
  fillAll("[data-brand-name]", config.brand_name);
  fillAll("[data-agent-name]", agentName(config));
  fillAll("[data-states]", statesText(config.supported_states));
  if (config.user) showUser(config.user);
  if (HAS_DOM) document.title = `${config.brand_name} · Flood claim call`;
  if (view) paint();
}

function showUser(email) {
  ui.userName.textContent = email;
  ui.userAvatar.textContent = String(email).trim().charAt(0) || "?";
  ui.userChip.hidden = false;
}

function showStatus(text, tone = "neutral") {
  setText(ui.callStatus, text);
  ui.callStatus.className = `status-chip ${tone}`;
  setText(ui.srStatus, text);
}

function setOrbState(state, label) {
  ui.orb.dataset.state = state;
  const agent = agentName(config);
  const labels = {
    idle: "Tap “Start claim call” when you're ready",
    connecting: `Connecting you to ${agent}…`,
    listening: "Listening. Go ahead, I'm here",
    agent: `${agent.charAt(0).toUpperCase() + agent.slice(1)} is speaking`,
    you: "Listening…",
    typing: `Connected. ${agent.charAt(0).toUpperCase() + agent.slice(1)} can read your messages`,
  };
  ui.orbLabel.textContent = label || labels[state] || "";
}

function showNotice(code) {
  const notice = noticeFor(code, config);
  if (!notice) return;
  activeNotice = code;
  ui.notice.className = `notice tone-${notice.tone}`;
  ui.noticeTitle.textContent = notice.title;
  ui.noticeBody.textContent = notice.body;
  ui.noticeAction.hidden = !notice.action;
  ui.noticeAction.textContent = notice.action || "";
  ui.notice.hidden = false;
}

function clearNotice() {
  activeNotice = null;
  ui.notice.hidden = true;
}

function commit(nextState) {
  const previous = view || blankView;
  view = { ...blankView, ...nextState, tool_activity: nextState.tool_activity ?? previous.tool_activity };
  paint();
}

function paint() {
  paintCaptions();
  paintProgress();
  paintSteps();
  paintNext();
  paintFacts();
  paintDocs();
  paintEvidence();
  paintSketch();
  paintActivity();
  paintPacket();
}

function paintCaptions() {
  ui.captions.innerHTML = (view.transcript || [])
    .map((turn) => {
      if (turn.speaker === "System") return `<li class="caption system"><p>${escapeHtml(turn.text)}</p></li>`;
      const agent = turn.speaker === "Agent";
      const who = agent ? escapeHtml(agentName(config)) : "You";
      return `<li class="caption ${agent ? "agent" : "you"}${turn.streaming ? " streaming" : ""}"><span class="caption-who">${who}</span><p>${escapeHtml(turn.text)}</p></li>`;
    })
    .join("");
  ui.captions.scrollTop = ui.captions.scrollHeight;
}

/** Read one finished turn to screen readers (the visual list re-renders, so it is not live). */
function announce(turn) {
  if (!turn || announced.has(turn.id) || !ui.srCaptions || typeof document.createElement !== "function") return;
  announced.add(turn.id);
  const line = document.createElement("p");
  const who = turn.speaker === "Agent" ? agentName(config) : turn.speaker === "System" ? "Notice" : "You";
  line.textContent = `${who}: ${turn.text}`;
  ui.srCaptions.append(line);
  while (ui.srCaptions.childElementCount > 6) ui.srCaptions.firstElementChild.remove();
}

function paintProgress() {
  const progress = Math.max(0, Math.min(100, Number(view.progress || 0)));
  ui.progressFill.style.width = `${progress}%`;
  ui.progressValue.textContent = `${progress}%`;
  ui.tabClaimBadge.textContent = `${progress}%`;
  if (typeof ui.progressBar.setAttribute === "function") ui.progressBar.setAttribute("aria-valuenow", String(progress));
}

const CHECK_ICON = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>';

function paintSteps() {
  ui.stepper.innerHTML = stepStates(view)
    .map((step, index) => {
      const mark = step.status === "done" ? CHECK_ICON : step.status === "attention" ? "!" : step.status === "partial" ? `<span>${index + 1}</span>` : String(index + 1);
      return `<li class="step ${step.status}${step.current ? " current" : ""}"${step.current ? ' aria-current="step"' : ""}><span class="step-dot">${mark}</span><span class="step-label">${step.label}</span><span class="sr-only">, ${STEP_STATUS_TEXT[step.status]}</span></li>`;
    })
    .join("");
}

function paintNext() {
  const next = nextStepFor(view, config);
  ui.nextStep.className = `next-card tone-${next.tone}`;
  setText(ui.nextTitle, next.title);
  setText(ui.nextBody, next.body);
}

function paintFacts() {
  ui.factList.innerHTML = keyFacts(view)
    .map((row) => {
      const known = row.status !== "pending";
      const fresh = known && !seenFacts.has(`${row.key}:${row.value}`);
      if (known) seenFacts.add(`${row.key}:${row.value}`);
      const value = known ? escapeHtml(row.value) : '<span class="pending">Not yet</span>';
      return `<div class="fact ${row.status}${fresh ? " fresh" : ""}"><dt>${row.label}</dt><dd>${value}</dd></div>`;
    })
    .join("");
}

function paintDocs() {
  const docs = view.documents || [];
  ui.docList.innerHTML = docs.length
    ? docs
        .map((doc) => {
          const status = docStatus(doc);
          const why = doc.reason ? `<span class="doc-why">${escapeHtml(doc.reason)}</span>` : "";
          return `<li class="doc ${status.tone}"><span class="doc-mark" aria-hidden="true"></span><span class="doc-text"><span class="doc-item">${escapeHtml(doc.item)}</span>${why}</span><span class="chip ${status.tone}">${status.label}</span></li>`;
        })
        .join("")
    : `<li class="empty">This list appears as ${escapeHtml(agentName(config))} learns about your claim.</li>`;
}

function paintEvidence() {
  const photos = view.evidence_photos || [];
  ui.evidenceCount.textContent = `${photos.length} of ${config.max_photos}`;
  const keys = photos.map((photo) => `${photo.id}:${photo.confirmed}`).join("|") || "empty";
  if (ui.evidenceGrid.dataset.keys === keys) return; // unchanged: keep images (no flicker)
  ui.evidenceGrid.dataset.keys = keys;
  ui.evidenceGrid.innerHTML = photos.length
    ? photos
        .map((photo) => {
          const badge = photoBadge(photo);
          return `<li><button type="button" class="thumb" data-photo-id="${escapeHtml(photo.id)}" aria-label="View photo: ${escapeHtml(photo.caption || "evidence photo")}"><img src="${escapeHtml(photo.url || "")}" alt="" loading="lazy" /><span class="thumb-badge ${badge.tone}">${badge.label}</span></button></li>`;
        })
        .join("")
    : `<li class="empty">No photos yet. Show the damage on camera or add photos you took before cleanup.</li>`;
}

function paintSketch() {
  const sketch = view.sketch;
  ui.sketchCard.hidden = !sketch;
  if (!sketch) return;
  if (ui.sketchImage.dataset.version !== String(sketch.version)) {
    ui.sketchImage.dataset.version = String(sketch.version);
    ui.sketchImage.src = sketch.url || "";
  }
  ui.sketchCaption.textContent = `Sketch ${sketch.version}: an illustration of what you described, not a photo. Tell ${agentName(config)} if something looks wrong.`;
}

function paintActivity() {
  const text = activityText(view, busy);
  ui.syncState.hidden = !text;
  setText(ui.syncState, text);
}

function paintPacket() {
  const href = intakeId ? `${PAGE_ORIGIN}/api/intakes/${encodeURIComponent(intakeId)}/packet.zip` : "#";
  const ready = Boolean(intakeId) && claimantHasSpoken(view);
  ui.downloadBtn.href = href;
  ui.packetDownloadBtn.href = href;
  if (typeof ui.downloadBtn.setAttribute === "function") ui.downloadBtn.setAttribute("aria-disabled", String(!ready));
  ui.packetText.textContent = view.packet_markdown || blankView.packet_markdown;
}

function systemNote(text) {
  const turn = { id: crypto.randomUUID(), speaker: "System", text };
  commit({ ...view, transcript: [...(view.transcript || []), turn] });
  announce(turn);
}

function acceptServerState(nextState) {
  if (!nextState || nextState.intake_id !== intakeId) return; // ignore a previous claim's late message
  commit({
    ...nextState,
    transcript: mergeTurns(nextState.transcript, view?.transcript),
    tool_activity: mergeActivity(nextState.tool_activity, view?.tool_activity),
  });
}

function acceptToolEvent(message) {
  const { type, intake_id: _ignored, ...entry } = message;
  const others = (view.tool_activity || []).filter((item) => item.id !== entry.id);
  commit({ ...view, tool_activity: [...others, entry].slice(-MAX_ACTIVITY) });
}

function upsertTurn(speaker, text, final = false, id = crypto.randomUUID()) {
  if (!String(text || "").trim()) return;
  const transcript = [...(view.transcript || [])];
  const index = transcript.findIndex((turn) => turn.id === id);
  const turn = { id, speaker, text, streaming: !final };
  if (index >= 0) transcript[index] = turn;
  else transcript.push(turn);
  commit({ ...view, transcript });
  if (final) announce(turn);
}

// ---- REST -----------------------------------------------------------------
async function callApi(path, options = {}) {
  const isForm = typeof FormData !== "undefined" && options.body instanceof FormData;
  const headers = isForm ? {} : { "Content-Type": "application/json" };
  const response = await fetch(`${PAGE_ORIGIN}${path}`, { credentials: "same-origin", headers, ...options });
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401) throw new Error("Your sign-in has expired. Reload the page to sign in again.");
  if (!response.ok) throw new Error(payload.detail || `Request failed with status ${response.status}`);
  return payload;
}

async function loadConfig() {
  try {
    applyConfig(await callApi("/api/config"));
  } catch (_) {
    applyConfig({}); // neutral fallbacks; the claim still works
  }
}

async function refreshState() {
  if (!intakeId) return;
  try {
    const payload = await callApi(`/api/intakes/${intakeId}`);
    acceptServerState(payload.state);
  } catch (_) {
    /* a later refresh or the live call will catch up */
  }
}

// ---- audio ----------------------------------------------------------------
function ensureAudio() {
  audioCtx = audioCtx || new AudioContext();
  return audioCtx;
}

/** Where agent speech is sent: a gain node + analyser (for the orb) when available. */
function agentOutput() {
  const ctx = ensureAudio();
  if (!agentBus && typeof ctx.createGain === "function" && typeof ctx.createAnalyser === "function") {
    agentBus = ctx.createGain();
    agentAnalyser = ctx.createAnalyser();
    agentAnalyser.fftSize = 512;
    agentBus.connect(agentAnalyser);
    agentAnalyser.connect(ctx.destination);
  }
  return agentBus || ctx.destination;
}

function silence() {
  for (const source of playing) {
    try {
      source.stop();
    } catch (_) {}
  }
  playing.clear();
  playhead = audioCtx?.currentTime || 0;
}

function releaseBusy() {
  busy = false;
  view.tool_activity = (view.tool_activity || []).map((item) => (item.phase === "running" ? { ...item, phase: "cancelled", headline: "Connection ended" } : item));
  paint();
}

async function wakeAudio() {
  if (window.AudioContext) await ensureAudio().resume();
}

/** Play one base64 chunk of 24 kHz 16-bit mono PCM, queued after the previous one. */
function playChunk(base64) {
  const ctx = ensureAudio();
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  const pcm = pcmSamples(bytes);
  if (!pcm.length) return; // nothing playable (empty or a single stray byte)
  const buffer = ctx.createBuffer(1, pcm.length, 24000);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < pcm.length; i += 1) channel[i] = pcm[i] / 32768;
  const source = ctx.createBufferSource();
  source.buffer = buffer;
  source.connect(agentOutput());
  const startAt = Math.max(ctx.currentTime, playhead);
  playing.add(source);
  source.onended = () => playing.delete(source);
  source.start(startAt);
  playhead = startAt + buffer.duration;
}

// ---- the voice orb --------------------------------------------------------------
function reducedMotion() {
  return Boolean(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
}

function rmsLevel(analyser, buffer) {
  if (!analyser) return 0;
  analyser.getFloatTimeDomainData(buffer);
  let sum = 0;
  for (let i = 0; i < buffer.length; i += 1) sum += buffer[i] * buffer[i];
  return Math.min(1, Math.sqrt(sum / buffer.length) * 5);
}

function palette() {
  if (!orbPalette) {
    const css = getComputedStyle(document.documentElement);
    orbPalette = {
      agent: css.getPropertyValue("--orb-agent").trim() || "teal",
      agentSoft: css.getPropertyValue("--orb-agent-soft").trim() || "rgba(0,128,128,.2)",
      you: css.getPropertyValue("--orb-you").trim() || "orange",
    };
  }
  return orbPalette;
}

function blobPath(ctx, center, radius, level, time, lobes, phase, still) {
  ctx.beginPath();
  const points = 96;
  for (let i = 0; i <= points; i += 1) {
    const angle = (i / points) * Math.PI * 2;
    const wobble = still ? 0 : (0.018 + level * 0.12) * Math.sin(angle * lobes + time / 650 + phase) + 0.012 * Math.sin(angle * (lobes + 3) - time / 900);
    const r = radius * (1 + wobble);
    const x = center + Math.cos(angle) * r;
    const y = center + Math.sin(angle) * r;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.closePath();
}

function drawOrb(time, mic, agent) {
  const canvas = ui.orbCanvas;
  if (!canvas || typeof canvas.getContext !== "function") return;
  const ratio = window.devicePixelRatio || 1;
  const size = canvas.clientWidth || 280;
  if (canvas.width !== Math.round(size * ratio)) {
    canvas.width = Math.round(size * ratio);
    canvas.height = Math.round(size * ratio);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, size, size);
  const colors = palette();
  const center = size / 2;
  const base = size * 0.24;
  const still = reducedMotion();
  blobPath(ctx, center, base * (1.55 + agent * 0.35), agent, time, 3, 0, still);
  ctx.fillStyle = colors.agentSoft;
  ctx.fill();
  blobPath(ctx, center, base * (1.3 + agent * 0.25), agent, time * 1.25, 5, 1.7, still);
  ctx.fillStyle = colors.agentSoft;
  ctx.fill();
  if (mic > 0.02) {
    blobPath(ctx, center, base * (1.75 + mic * 0.4), mic, time * 1.6, 7, 0.6, still);
    ctx.strokeStyle = colors.you;
    ctx.lineWidth = 1.5 + mic * 5;
    ctx.stroke();
  }
}

function startOrb() {
  if (orbFrame || typeof requestAnimationFrame !== "function") return;
  const buffer = new Float32Array(512);
  let mic = 0;
  let agent = 0;
  const step = (time) => {
    orbFrame = requestAnimationFrame(step);
    mic = mic * 0.75 + rmsLevel(micAnalyser, buffer) * 0.25;
    agent = agent * 0.75 + rmsLevel(agentAnalyser, buffer) * 0.25;
    ui.orb.style.setProperty("--mic", mic.toFixed(3));
    ui.orb.style.setProperty("--agent", agent.toFixed(3));
    if (socket && ui.orb.dataset.state !== "connecting") {
      const next = agent > 0.05 ? "agent" : mic > 0.08 ? "you" : listening ? "listening" : "typing";
      if (ui.orb.dataset.state !== next) setOrbState(next);
    }
    drawOrb(time, mic, agent);
  };
  orbFrame = requestAnimationFrame(step);
}

function stopOrb() {
  if (orbFrame && typeof cancelAnimationFrame === "function") cancelAnimationFrame(orbFrame);
  orbFrame = 0;
  if (ui.orb?.style?.setProperty) {
    ui.orb.style.setProperty("--mic", "0");
    ui.orb.style.setProperty("--agent", "0");
  }
  drawOrb(0, 0, 0);
}

// ---- call clock -------------------------------------------------------------------
function tickClock() {
  const elapsed = (Date.now() - callStartedAt) / 1000;
  const limit = config.live_session_minutes * 60;
  ui.callClock.textContent = `${formatClock(elapsed)} / ${formatClock(limit)}`;
  ui.callClock.className = `call-clock${limit - elapsed <= 60 ? " warn" : ""}`;
}

function startClock() {
  stopClock();
  callStartedAt = Date.now();
  ui.callClock.hidden = false;
  tickClock();
  clockTimer = window.setInterval(tickClock, 1000);
}

function stopClock() {
  if (clockTimer) window.clearInterval(clockTimer);
  clockTimer = null;
  ui.callClock.hidden = true;
}

// ---- call lifecycle -------------------------------------------------------
function setToggle(button, active, label) {
  button.classList.toggle("active", active);
  if (typeof button.setAttribute === "function") button.setAttribute("aria-pressed", String(active));
  button.querySelector(".btn-label").textContent = label;
}

function hangUp() {
  epoch += 1;
  const current = socket;
  socket = null;
  socketReady = null;
  stopMic(false);
  stopLens();
  silence();
  stopClock();
  stopOrb();
  if (current) current.close();
  if (view) releaseBusy();
  setOrbState("idle");
}

async function startIntake(resume = false) {
  if (restarting) return;
  restarting = true;
  ui.newClaimBtn.disabled = true;
  const previous = intakeId || sessionStorage.getItem(STORAGE_KEY);
  hangUp();
  clearNotice();
  const myEpoch = epoch;
  seenFacts.clear();
  showStatus("Connecting", "neutral");
  intakeId = null;
  commit(blankView);
  const configLoaded = loadConfig();
  try {
    let payload;
    if (resume && previous) {
      try {
        payload = await callApi(`/api/intakes/${previous}`);
      } catch (_) {
        sessionStorage.removeItem(STORAGE_KEY);
      }
    } else if (previous) {
      await callApi(`/api/intakes/${previous}`, { method: "DELETE" }).catch(() => {});
    }
    payload = payload || (await callApi("/api/intakes", { method: "POST" }));
    await configLoaded;
    if (myEpoch !== epoch) return;
    intakeId = payload.intake_id;
    sessionStorage.setItem(STORAGE_KEY, intakeId);
    for (const turn of payload.state?.transcript || []) announced.add(turn.id); // history is not "new"
    commit(payload.state);
    if (payload.user) showUser(payload.user);
    showStatus("Ready", "neutral");
    setOrbState("idle");
  } catch (error) {
    showStatus("Offline", "danger");
    showNotice("backend");
  } finally {
    restarting = false;
    ui.newClaimBtn.disabled = false;
  }
}

function openCall() {
  if (socketReady) return socketReady;
  if (!intakeId || restarting) return Promise.reject(new Error("Wait for your claim to be ready."));
  const myEpoch = epoch;
  const ws = new WebSocket(`${SOCKET_ORIGIN}/ws/live?intake_id=${encodeURIComponent(intakeId)}`);
  socket = ws;
  showStatus("Connecting", "neutral");
  setOrbState("connecting");
  socketReady = new Promise((resolve, reject) => {
    let ready = false;
    const timeout = setTimeout(() => {
      reject(new Error("The call took too long to connect."));
      ws.close();
    }, 20000);
    const current = () => myEpoch === epoch && socket === ws;
    ws.onmessage = (event) => {
      if (!current()) return;
      const message = JSON.parse(event.data);
      if (message.intake_id !== intakeId) return;
      if (message.type === "ready") {
        ready = true;
        clearTimeout(timeout);
        clearNotice();
        showStatus("Live", "live");
        setOrbState(listening ? "listening" : "typing");
        startClock();
        startOrb();
        resolve();
      } else if (message.type === "processing") {
        busy = message.active;
        paint();
      } else if (message.type === "transcript") {
        upsertTurn(message.speaker, message.text, message.final, message.id);
      } else if (message.type === "tool") {
        acceptToolEvent(message);
      } else if (message.type === "audio") {
        playChunk(message.data);
      } else if (message.type === "state") {
        acceptServerState(message.state);
      } else if (message.type === "interrupted") {
        silence();
      } else if (message.type === "status") {
        // The server is swapping the Gemini Live connection under this call.
        // Keep the socket, mic, camera and clock; only the chip changes.
        const status = liveStatusText(message.code);
        if (status && ready) {
          showStatus(status.text, status.tone);
          if (message.code === "reconnecting") setOrbState("connecting", status.text);
          else setOrbState(listening ? "listening" : "typing");
        }
      } else if (message.type === "error") {
        busy = false;
        if (message.code && NOTICE_CODES.has(message.code)) {
          showNotice(message.code);
          paint();
        } else {
          systemNote(message.message);
        }
        if (!ready) {
          reject(new Error(message.message));
          ws.close();
        }
      }
    };
    ws.onerror = () => {
      clearTimeout(timeout);
      reject(new Error("The live call could not connect."));
      ws.close();
    };
    ws.onclose = () => {
      clearTimeout(timeout);
      if (!ready) reject(new Error("The call ended before it was ready."));
      if (!current()) return;
      socket = null;
      socketReady = null;
      stopMic(false);
      stopLens();
      silence();
      stopClock();
      stopOrb();
      releaseBusy();
      setOrbState("idle");
      showStatus("Disconnected", "warning");
      if (!activeNotice) showNotice("connection_lost");
    };
  });
  return socketReady;
}

async function sendTyped(text) {
  const myEpoch = epoch;
  try {
    await wakeAudio();
    await openCall();
    if (myEpoch !== epoch) return;
    const id = crypto.randomUUID();
    socket.send(JSON.stringify({ type: "text", text, id }));
    upsertTurn("Claimant", text, true, id);
    busy = true;
    paint();
  } catch (error) {
    if (myEpoch !== epoch) return;
    ui.typeInput.value = text;
    if (!activeNotice) systemNote(`${error.message} Your message is back in the box; send it again to retry.`);
  }
}

async function startMic() {
  if (micStarting || listening) return;
  const myEpoch = epoch;
  if (!navigator.mediaDevices?.getUserMedia || !window.AudioContext) {
    showNotice("mic_unsupported");
    return;
  }
  try {
    micStarting = true;
    clearNotice();
    await wakeAudio();
    await openCall();
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    if (myEpoch !== epoch || !socket || socket.readyState !== WebSocket.OPEN) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    micStream = stream;
    const ctx = ensureAudio();
    micSource = ctx.createMediaStreamSource(micStream);
    if (typeof ctx.createAnalyser === "function") {
      micAnalyser = ctx.createAnalyser();
      micAnalyser.fftSize = 512;
      micSource.connect(micAnalyser);
    }
    // ScriptProcessor is old but universally supported and simple to follow.
    micNode = ctx.createScriptProcessor(4096, 1, 1);
    micNode.onaudioprocess = (event) => {
      event.outputBuffer.getChannelData(0).fill(0);
      if (!socket || socket.readyState !== WebSocket.OPEN) return;
      const pcm16 = toPcm16(event.inputBuffer.getChannelData(0), ctx.sampleRate, 16000);
      socket.send(JSON.stringify({ type: "audio", data: bytesToBase64(pcm16.buffer) }));
    };
    micSource.connect(micNode);
    micNode.connect(ctx.destination);
    listening = true;
    setToggle(ui.callBtn, true, "End call");
    setOrbState("listening");
    showStatus("Live", "live");
  } catch (error) {
    const denied = error.name === "NotAllowedError" || /denied|permission/i.test(error.message);
    if (denied) showNotice("mic_denied");
    else if (!activeNotice) systemNote(`Voice couldn't start: ${error.message}`);
    stopMic(false);
  } finally {
    micStarting = false;
  }
}

function stopMic(closeCall = true) {
  listening = false;
  setToggle(ui.callBtn, false, "Start claim call");
  micNode?.disconnect();
  micSource?.disconnect();
  micAnalyser?.disconnect();
  micStream?.getTracks().forEach((track) => track.stop());
  micNode = null;
  micSource = null;
  micAnalyser = null;
  micStream = null;
  if (closeCall) {
    hangUp();
    showStatus("Call ended · claim saved", "neutral");
  }
}

async function startLens() {
  if (lensStarting || lensStream) return;
  const myEpoch = epoch;
  if (!navigator.mediaDevices?.getUserMedia) {
    showNotice("camera_denied");
    return;
  }
  try {
    lensStarting = true;
    await wakeAudio();
    await openCall();
    const stream = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 }, height: { ideal: 960 }, facingMode: "environment" } });
    if (myEpoch !== epoch || !socket || socket.readyState !== WebSocket.OPEN) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    lensStream = stream;
    stream.getVideoTracks().forEach((track) => track.addEventListener("ended", stopLens, { once: true }));
    socket.send(JSON.stringify({ type: "camera_state", enabled: true }));
    ui.lensPreview.srcObject = lensStream;
    ui.lensStage.hidden = false;
    setToggle(ui.lensBtn, true, "Stop camera");
    const everyMs = Math.round(1000 / Math.max(0.2, Number(config.camera_fps) || 1));
    frameTimer = window.setInterval(pushFrame, everyMs);
  } catch (error) {
    const denied = error.name === "NotAllowedError" || /denied|permission/i.test(error.message);
    if (denied) showNotice("camera_denied");
    else if (!activeNotice) systemNote(`Camera couldn't start: ${error.message}`);
    stopLens();
  } finally {
    lensStarting = false;
  }
}

function stopLens() {
  const wasOn = Boolean(lensStream);
  if (frameTimer) window.clearInterval(frameTimer);
  frameTimer = null;
  lensStream?.getTracks().forEach((track) => track.stop());
  lensStream = null;
  ui.lensPreview.srcObject = null;
  ui.lensStage.hidden = true;
  setToggle(ui.lensBtn, false, "Show camera");
  if (wasOn && socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type: "camera_state", enabled: false }));
  }
}

/** Draw the current video frame onto the hidden canvas (width capped). */
function grabFrame(maxWidth) {
  const video = ui.lensPreview;
  if (!video.videoWidth) return null;
  const scale = Math.min(1, maxWidth / video.videoWidth);
  ui.grabber.width = Math.round(video.videoWidth * scale);
  ui.grabber.height = Math.round(video.videoHeight * scale);
  ui.grabber.getContext("2d").drawImage(video, 0, 0, ui.grabber.width, ui.grabber.height);
  return ui.grabber;
}

function pushFrame() {
  if (!lensStream || !socket || socket.readyState !== WebSocket.OPEN) return;
  const canvas = grabFrame(FRAME_WIDTH);
  if (!canvas) return;
  socket.send(JSON.stringify({ type: "video", data: canvas.toDataURL("image/jpeg", 0.6).split(",")[1] }));
}

// ---- photos ---------------------------------------------------------------------
/** Re-encode any picked image as a JPEG no larger than UPLOAD_MAX_SIDE (smaller upload, EXIF stripped). */
async function shrinkToJpeg(file) {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, UPLOAD_MAX_SIDE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  return canvasToJpeg(canvas, 0.85);
}

function canvasToJpeg(canvas, quality) {
  return new Promise((resolve, reject) => canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("Could not read that image."))), "image/jpeg", quality));
}

function photoLimitReached() {
  if ((view.evidence_photos || []).length < config.max_photos) return false;
  systemNote(`This claim already has ${config.max_photos} photos, the most one claim can hold. Download the packet to keep them.`);
  return true;
}

function openPhotoDialog(blob) {
  closePhotoDialog();
  pendingPhoto = { blob, url: URL.createObjectURL(blob) };
  ui.photoPreview.src = pendingPhoto.url;
  ui.photoNote.value = "";
  ui.photoDialog.showModal();
  ui.photoNote.focus();
}

function closePhotoDialog() {
  if (pendingPhoto) URL.revokeObjectURL(pendingPhoto.url);
  pendingPhoto = null;
  if (ui.photoDialog.open) ui.photoDialog.close();
}

async function captureEvidence() {
  if (photoLimitReached()) return;
  const canvas = grabFrame(UPLOAD_MAX_SIDE);
  if (!canvas) {
    systemNote("The camera isn't ready yet. Hold still for a second and try again.");
    return;
  }
  try {
    openPhotoDialog(await canvasToJpeg(canvas, 0.88));
  } catch (error) {
    systemNote(error.message);
  }
}

async function pickPhoto(file) {
  if (!intakeId || !file || photoLimitReached()) return;
  try {
    openPhotoDialog(await shrinkToJpeg(file));
  } catch (error) {
    systemNote(`That photo couldn't be read: ${error.message}`);
  }
}

async function uploadPhoto(blob, description) {
  if (!intakeId || !blob) return;
  const form = new FormData();
  form.append("photo", blob, "photo.jpg");
  form.append("description", description || "");
  busy = true;
  paint();
  try {
    const payload = await callApi(`/api/intakes/${intakeId}/photos`, { method: "POST", body: form });
    acceptServerState(payload.state);
    // The claim team re-reads the file in the background. During a live call
    // the new state is pushed over the socket; otherwise poll a couple of times.
    if (!socket) [6000, 15000].forEach((ms) => setTimeout(refreshState, ms));
  } catch (error) {
    systemNote(`The photo wasn't added: ${error.message}`);
  } finally {
    busy = false;
    paint();
  }
}

function openViewer(photoId) {
  const photo = (view.evidence_photos || []).find((item) => item.id === photoId);
  if (!photo) return;
  ui.viewerImage.src = photo.url || "";
  ui.viewerImage.alt = photo.caption || "Evidence photo";
  const said = photo.claimant_description ? ` You said: “${photo.claimant_description}”.` : "";
  ui.viewerCaption.textContent = `${photo.caption || "Evidence photo"}.${said} ${photoBadge(photo).label}.`;
  ui.viewerDialog.showModal();
}

// ---- theme + tabs ---------------------------------------------------------------
function currentTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

function syncThemeButton() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  ui.themeBtn.setAttribute("aria-label", `Switch to ${next} theme`);
}

function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch (_) {}
  orbPalette = null; // colours come from CSS variables; re-read them
  syncThemeButton();
  drawOrb(0, 0, 0);
}

function followSystemTheme() {
  const media = window.matchMedia?.("(prefers-color-scheme: dark)");
  media?.addEventListener?.("change", (event) => {
    let saved = null;
    try {
      saved = localStorage.getItem(THEME_KEY);
    } catch (_) {}
    if (saved) return; // the user chose explicitly
    document.documentElement.dataset.theme = event.matches ? "dark" : "light";
    orbPalette = null;
    syncThemeButton();
  });
}

function setTab(name) {
  document.body.dataset.tab = name;
  ui.tabCall.setAttribute("aria-selected", String(name === "call"));
  ui.tabClaim.setAttribute("aria-selected", String(name === "claim"));
}

// ---- wiring -----------------------------------------------------------------
function boot() {
  bindElements();
  syncThemeButton();
  followSystemTheme();
  drawOrb(0, 0, 0);
  ui.themeBtn.addEventListener("click", toggleTheme);
  ui.callBtn.addEventListener("click", () => (listening ? stopMic(true) : startMic()));
  ui.lensBtn.addEventListener("click", () => (lensStream ? stopLens() : startLens()));
  ui.captureBtn.addEventListener("click", captureEvidence);
  ui.uploadBtn.addEventListener("click", () => ui.picker.click());
  ui.picker.addEventListener("change", () => {
    const file = ui.picker.files && ui.picker.files[0];
    ui.picker.value = "";
    pickPhoto(file);
  });
  ui.photoForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const photo = pendingPhoto;
    const note = ui.photoNote.value.trim();
    pendingPhoto = null;
    ui.photoDialog.close();
    if (photo) {
      uploadPhoto(photo.blob, note).finally(() => URL.revokeObjectURL(photo.url));
    }
  });
  ui.photoCancelBtn.addEventListener("click", closePhotoDialog);
  ui.photoDialog.addEventListener("close", () => {
    if (pendingPhoto) closePhotoDialog();
  });
  ui.evidenceGrid.addEventListener("click", (event) => {
    const button = event.target.closest("[data-photo-id]");
    if (button) openViewer(button.dataset.photoId);
  });
  ui.viewerCloseBtn.addEventListener("click", () => ui.viewerDialog.close());
  ui.previewPacketBtn.addEventListener("click", () => ui.packetDialog.showModal());
  ui.packetCloseBtn.addEventListener("click", () => ui.packetDialog.close());
  ui.downloadBtn.addEventListener("click", (event) => {
    if (ui.downloadBtn.getAttribute("aria-disabled") === "true") event.preventDefault();
  });
  ui.noticeDismiss.addEventListener("click", clearNotice);
  ui.noticeAction.addEventListener("click", () => {
    const code = activeNotice;
    clearNotice();
    if (code === "backend") startIntake(true);
    else startMic();
  });
  ui.newClaimBtn.addEventListener("click", () => {
    if (claimantHasSpoken(view) && !window.confirm("Start a new claim? This one will be closed and its photos removed. Download the packet first if you want to keep it.")) return;
    startIntake(false);
  });
  ui.tabCall.addEventListener("click", () => setTab("call"));
  ui.tabClaim.addEventListener("click", () => setTab("claim"));
  window.addEventListener("pagehide", hangUp);
  ui.typeForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const value = ui.typeInput.value.trim();
    if (!value) return;
    ui.typeInput.value = "";
    sendTyped(value);
  });
  startIntake(true);
}

if (HAS_DOM && !globalThis.CLAIM_NO_BOOT) boot();

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    escapeHtml,
    hasValue,
    neededLabel,
    statesText,
    mergeTurns,
    mergeActivity,
    liveStatusText,
    pcmSamples,
    toPcm16,
    bytesToBase64,
    claimantHasSpoken,
    stepStates,
    nextStepFor,
    keyFacts,
    docStatus,
    photoBadge,
    formatClock,
    activityText,
    noticeFor,
  };
}
