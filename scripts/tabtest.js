// Copyright (C) 2026 BigPoetry
// SPDX-License-Identifier: AGPL-3.0-or-later

// Behavioural self-test of the REAL web/index.html script, run in Node against a
// minimal DOM stub. This executes the shipped code — not a copy — and drives the
// actual ws.onmessage handler, so what it proves is what the browser will do.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

// Resolved from THIS FILE's location, not the working directory and not a
// hardcoded checkout path: the harness lives in the repo now, so it must find
// its client whatever the checkout is called and wherever it is run from.
// CLIENT= still overrides, which is what lets it be pointed at an older
// revision of the page to prove a regression test fails there.
const DEFAULT_CLIENT = path.join(__dirname, "..", "web", "index.html");
const HTML = fs.readFileSync(process.env.CLIENT || DEFAULT_CLIENT, "utf8");
let SCRIPT = HTML.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
// MASTER_OFF=1 flips the OPERATOR's master switch in the real source, to prove it
// wins over the resident's own setting.
const MASTER_OFF = process.env.MASTER_OFF === "1";
if (MASTER_OFF) {
  const before = SCRIPT;
  SCRIPT = SCRIPT.replace("enabled: true,", "enabled: false,");
  if (SCRIPT === before) { console.log("FAIL  could not flip TAB_ALERT.enabled"); process.exit(1); }
}

let pass = 0, fail = 0;
function check(label, cond, detail) {
  if (cond) { pass++; console.log("PASS  " + label); }
  else { fail++; console.log("FAIL  " + label + (detail ? "   " + detail : "")); }
}

// ---- the smallest DOM that lets the real script run ----------------------
function makeEl(tag) {
  const el = {
    tagName: (tag || "div").toUpperCase(),
    children: [], attrs: {}, dataset: {},
    style: {
      _props: {},
      setProperty(k, v) { this._props[k] = String(v); },
      getPropertyValue(k) { return this._props[k] || ""; },
      removeProperty(k) { delete this._props[k]; },
    },
    className: "", textContent: "", value: "", hidden: false,
    scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    get childElementCount() { return this.children.length; },
    get firstChild() { return this.children[0]; },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    removeChild(c) {
      this.children = this.children.filter(x => x !== c);
      if (c) c.parentNode = null;
      return c;
    },
    parentNode: null,
    setAttribute(k, v) { this.attrs[k] = v; this[k] = v; },
    getAttribute(k) {
      if (k in this.attrs) return this.attrs[k];
      return (k in this && typeof this[k] === "string") ? this[k] : null;
    },
    remove() { if (this.parentNode) this.parentNode.removeChild(this); },
    addEventListener(t, fn) {
      if (this === byId.input && t === "keydown") keydownHandlers.push(fn);
    },
    removeEventListener() {},
    focus() {}, blur() {}, setSelectionRange() {},
    // Width follows the TEXT, so the client's character probe measures
    // something real: a run of N characters is N * harness.charWidth wide.
    getBoundingClientRect() {
      return {width: this.textContent ? this.textContent.length * harness.charWidth
                                      : 600,
              height: 20};
    },
    classList: {add(){}, remove(){}, toggle(){}},
  };
  if ((tag || "").toLowerCase() === "canvas") {
    // Enough canvas for dotDataURL(): a context whose calls are no-ops and a
    // toDataURL that returns a recognisable stand-in.
    el.width = el.height = 0;
    el.getContext = () => ({
      beginPath(){}, arc(){}, fill(){}, set fillStyle(v){}, get fillStyle(){ return ""; },
    });
    el.toDataURL = () => "data:image/png;base64,STUBDOT";
  }
  return el;
}

// A <head> with the page's own blank icon in it, so the fix's
// remove-and-append can be observed as element identity, not just an href value.
const head = makeEl("head");
const originalIconEl = makeEl("link");
originalIconEl.setAttribute("rel", "icon");
originalIconEl.setAttribute("href", "data:,");
head.appendChild(originalIconEl);

function iconLinks() {
  return head.children.filter(
    (c) => c.tagName === "LINK" &&
           String(c.getAttribute("rel") || "").split(/\s+/).indexOf("icon") !== -1);
}
function iconEl_() { return iconLinks()[0] || null; }
function iconHref() { const e = iconEl_(); return e ? e.getAttribute("href") : null; }

const keydownHandlers = [];
const sent = [];
const byId = {screen: makeEl("div"), input: makeEl("textarea"), mask: makeEl("input")};
const docListeners = {};
const winListeners = {};

// The pre-connect placeholder as the PAGE actually ships it, not a copy — so a
// change to <title> is caught here rather than silently agreed with.
const PLACEHOLDER = (HTML.match(/<title>([^<]*)<\/title>/) || [, ""])[1];

const document = {
  title: PLACEHOLDER,
  hidden: false,
  hasFocus: () => harness.focused,
  documentElement: makeEl("html"),
  head,
  getElementById: (id) => byId[id] || makeEl("div"),
  querySelector: (sel) => (/link\[rel/.test(sel) ? iconEl_() : null),
  querySelectorAll: (sel) => (/link\[rel/.test(sel) ? iconLinks() : []),
  getElementsByTagName: (t) => (t === "head" ? [head] : []),
  createElement: makeEl,
  addEventListener: (t, fn) => { (docListeners[t] = docListeners[t] || []).push(fn); },
};

const wsInstances = [];
class FakeWebSocket {
  constructor(url) { this.url = url; this.readyState = 1; wsInstances.push(this); }
  send(text) { sent.push(text); }
  close() {}
}
FakeWebSocket.OPEN = 1;

const window = {
  addEventListener: (t, fn) => { (winListeners[t] = winListeners[t] || []).push(fn); },
  getSelection: () => ({toString: () => ""}),
  AudioContext: undefined, webkitAudioContext: undefined,
  get screen() { return screenStub; },
};

// charWidth + the screen's clientWidth are what the client measures; setting
// them IS setting the simulated device. 9px glyphs and a 384px screen (with 24px
// of padding) come to exactly 40 usable columns — a portrait phone.
const harness = {focused: true, fakeNow: null, charWidth: 9};
function setDeviceColumns(cols) {
  byId.screen.clientWidth = cols * harness.charWidth + 24;
}
setDeviceColumns(40);

// Every setInterval the client registers, so the harness can assert there is
// exactly ONE and can drive it with a controlled clock.
const intervals = [];

// A REAL localStorage stub (the .raise_prompt offset persists through it) and a
// screen the first-load heuristic reads. Both are swapped per run by env vars,
// so each load-time question is asked in its own process — exactly once, as a
// real page asks it.
const store = (() => {
  const data = Object.create(null);
  if (process.env.SAVED_RAISE !== undefined) data["heresay-raise"] = process.env.SAVED_RAISE;
  if (process.env.SAVED_TITLE !== undefined) data["heresay-title"] = process.env.SAVED_TITLE;
  if (process.env.SAVED_RESUME !== undefined) data["heresay-resume"] = process.env.SAVED_RESUME;
  return {
    data,
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => { data[k] = String(v); },
    removeItem: (k) => { delete data[k]; },
  };
})();
const screenStub = process.env.NO_SCREEN === "1" ? undefined : {
  height: 1080,
  availHeight: process.env.AVAIL_HEIGHT ? Number(process.env.AVAIL_HEIGHT) : 1080,
};

const sandbox = {
  document, window, console,
  location: {protocol: "http:", host: "localhost:8099", hostname: "localhost", search: ""},
  localStorage: store,
  WebSocket: FakeWebSocket,
  requestAnimationFrame: (fn) => setTimeout(fn, 0),
  getComputedStyle: () => ({lineHeight: "20px", fontSize: "16px", paddingTop: "0px",
                            paddingBottom: "0px", borderTopWidth: "0px",
                            borderBottomWidth: "0px",
                            // #screen's real horizontal padding (10px 12px 4px).
                            paddingLeft: "12px", paddingRight: "12px"}),
  setTimeout, clearTimeout,
  setInterval: (fn, ms) => {
    const id = setInterval(fn, ms);
    intervals.push({fn, ms, id, cleared: false});
    return id;
  },
  // Wrapped so `cleared` is actually maintained. It was recorded as false and
  // never updated, which made any "is the interval still running?" assertion
  // silently meaningless — it could only ever have read false.
  clearInterval: (id) => {
    const rec = intervals.find(i => i.id === id);
    if (rec) rec.cleared = true;
    return clearInterval(id);
  },
  // The client prefers performance.now() for the blink phase. Real time unless
  // the harness pins it, so the existing timing tests are unaffected.
  performance: {now: () => (harness.fakeNow === null ? Date.now() : harness.fakeNow)},
  Math, Date, JSON, parseFloat, parseInt, isFinite, String, Number, Object, Array, Error,
  URLSearchParams, Set, Map, Promise, RegExp, Boolean, Symbol,
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;

store.__titleAtLoad = document.title;      // before the script runs
vm.createContext(sandbox);
try {
  vm.runInContext(SCRIPT, sandbox, {filename: "web/index.html <script>"});
  check("the real client script runs without throwing", true);
} catch (e) {
  check("the real client script runs without throwing", false, String(e));
  process.exit(1);
}

// Drive the REAL submit path: put the line in the compose box and fire Enter,
// exactly as typing it does — which is what lets us watch the stub socket and
// prove a client command never escaped to the server.
function frame() {
  // One requestAnimationFrame tick, awaitable — the client defers its scroll
  // re-pin by a frame so layout has run.
  return new Promise((r) => setTimeout(r, 5));
}

function typeLine(text) {
  byId.input.value = text;
  const before = sent.length;
  keydownHandlers.forEach((fn) => fn({key: "Enter", shiftKey: false, preventDefault() {}}));
  return sent.length > before;              // true = it reached the socket
}
function raiseValue() {
  return document.documentElement.style.getPropertyValue("--raise-lines");
}
function lastLine() {
  const c = byId.screen.children;
  return c.length ? String(c[c.length - 1].textContent) : "";
}

function fire(map, type) { (map[type] || []).forEach(fn => fn({type})); }
// A GENUINE reconnect, the way returning to a backgrounded tab produces one:
// the socket died while we were away, and coming back to the page opens a new
// one. Driven entirely through the client's own paths — the close handler, then
// the focus listener it registered — so what is exercised is the real trigger,
// not a shortcut into connect().
function connectAgain() {
  const before = wsInstances.length;
  if (ws().onclose) ws().onclose();       // the socket died while backgrounded
  fire(winListeners, "focus");            // ...and we came back to the tab
  if (wsInstances.length === before) {
    check("a return to the tab opens a new socket", false,
          "no reconnect happened");
    return;
  }
  if (ws().onopen) ws().onopen();
}
function ws() { return wsInstances[wsInstances.length - 1]; }
function incoming(text) { ws().onmessage({data: text}); }

// The client connects on load (or on the first Enter); make sure we have a socket.
if (!wsInstances.length) {
  check("a WebSocket was opened", false, "none created");
  process.exit(1);
}
check("a WebSocket was opened", true);
// Until the socket reports open the client treats Enter as "reconnect", so fire
// onopen the way a real connection would before driving the compose box.
if (ws().onopen) ws().onopen();
// ...and then LOG IN. Since v1.25.1 the tab mark only blinks for a connected,
// in-world session, so every check below that expects a blink is checking the
// logged-in state — which is what a real page is in when a room line arrives.
// The gating itself (no blink at the login prompt, none while disconnected) has
// its own mode, INWORLD=1.
incoming("\u0001inworld on");

// The title in force for most of the run: the cached/placeholder value until a
// \x01title frame arrives.
const ORIGINAL = PLACEHOLDER;
const DOT = "data:image/png;base64,STUBDOT";

if (process.env.TITLE_INIT === "1") {
  // The load-time title question, asked once per process as a real page asks it:
  // a cached house name must be showing BEFORE any frame arrives; with nothing
  // cached, the neutral placeholder stands.
  const cached = process.env.SAVED_TITLE;
  if (cached === undefined) {
    check("no cache -> the neutral placeholder, not the engine's name",
          document.title === PLACEHOLDER && document.title !== "Heresay",
          document.title);
  } else {
    check('a cached name is showing at load, before any frame ("' + cached + '")',
          document.title === cached, document.title);
    // And the blink must alternate the CACHED name, not the placeholder.
    harness.focused = false; fire(winListeners, "blur");
    incoming("Corvid says: blink before the frame");
    check("...and a blink before the frame marks the cached name",
          document.title === "● " + cached, document.title);
  }
  finish();
} else if (process.env.RAISE_INIT === "1") {
  // The first-load question, asked once per process: what offset does the page
  // start at? A saved value must win over the heuristic — including a saved 0,
  // which is somebody having deliberately turned the nudge off.
  const applied = raiseValue();
  const saved = process.env.SAVED_RAISE;
  const label = saved !== undefined
    ? 'a saved "' + saved + '" wins over the heuristic'
    : (process.env.NO_SCREEN === "1"
        ? "no screen API -> 0, and no throw"
        : "screen " + screenStub.height + "/" + screenStub.availHeight
          + " -> " + process.env.EXPECT_RAISE);
  check(label, applied === process.env.EXPECT_RAISE, "applied " + applied);
  check("  ...and the chosen value is persisted",
        store.getItem("heresay-raise") === applied,
        String(store.getItem("heresay-raise")));
  finish();
} else if (process.env.WIDTH === "1") {
  // ---- the width report (client -> server) --------------------------------
  //
  // The ONE frame that travels client -> server. The server lays every screen
  // out, so it has to be told how wide the screen is, and only this page can
  // measure that. Everything here is about the REPORT: the client still renders
  // nothing itself.
  const widthFrames = () => sent.filter(f => f.indexOf("\u0001width ") === 0);

  check("a width report was sent on connect", widthFrames().length === 1,
        JSON.stringify(sent));
  check("...and it was the FIRST frame on the socket",
        sent[0] === widthFrames()[0], JSON.stringify(sent[0]));
  check("...well-formed: \\x01width <integer>",
        /^\u0001width (\d+)$/.test(widthFrames()[0]), widthFrames()[0]);
  check("...reporting the measured columns, not a guess",
        widthFrames()[0] === "\u0001width 40", widthFrames()[0]);

  // Turned sideways: a new measurement, and therefore a new report.
  setDeviceColumns(88);
  fire(winListeners, "resize");
  check("a resize does not report synchronously (it is debounced)",
        widthFrames().length === 1, String(widthFrames().length));
  setTimeout(() => {
    check("a resize reports the new width once it settles",
          widthFrames().length === 2, JSON.stringify(sent));
    check("...with the new column count",
          widthFrames()[1] === "\u0001width 88", widthFrames()[1]);

    // A resize that does not change the column count says nothing. A drag fires
    // dozens of these; each one is a frame we are choosing not to send.
    fire(winListeners, "resize");
    setTimeout(() => {
      check("a resize that changes nothing sends nothing",
            widthFrames().length === 2, JSON.stringify(sent));

      // Older iOS fires orientationchange without a useful resize.
      setDeviceColumns(40);
      fire(winListeners, "orientationchange");
      setTimeout(() => {
        check("an orientationchange reports too",
              widthFrames().length === 3, JSON.stringify(sent));
        check("...back to the portrait width",
              widthFrames()[2] === "\u0001width 40", widthFrames()[2]);
        // Re-audit, automated rather than remembered: the width report adds a
        // client -> server MESSAGE, and must not have added a request.
        const externals = HTML.match(/\b(fetch|XMLHttpRequest|EventSource|importScripts)\s*\(/g) || [];
        check("zero external requests: no fetch/XHR/EventSource anywhere",
              externals.length === 0, JSON.stringify(externals));
        const urls = (HTML.match(/url\(([^)]*)\)/g) || [])
              .filter(u => !/^url\(["']?(data:|#)/.test(u));
        check("...and the only URL is still the self-hosted font",
              urls.length === 1 && /IBMPlexMono/.test(urls[0]), JSON.stringify(urls));
        check("the report is never stored (it is true only right now)",
              store.getItem("heresay-width") === null &&
              Object.keys(store.data).indexOf("heresay-width") === -1,
              JSON.stringify(Object.keys(store.data)));
        finish();
      }, 400);
    }, 400);
  }, 400);
} else if (process.env.RESUME === "1") {
  // ---- remember me: the client's custody of the resume token --------------
  //
  // The client's whole job here is custody. It does not decide anything: the
  // server issues a token only for an account that opted in, and the browser
  // holds it, presents it, replaces it when rotated, and drops it when told to.
  const resumeFrames = () => sent.filter(f => f.indexOf("\u0001resume") === 0);
  const stored = () => store.getItem("heresay-resume");
  const SAVED = process.env.SAVED_RESUME;

  check("a resume frame is sent on connect", resumeFrames().length === 1,
        JSON.stringify(sent));
  check("...after the width report, never before",
        sent.indexOf(resumeFrames()[0]) > sent.findIndex(f => f.indexOf("\u0001width ") === 0),
        JSON.stringify(sent));
  if (SAVED) {
    check("...carrying the stored token", resumeFrames()[0] === "\u0001resume " + SAVED,
          resumeFrames()[0]);
  } else {
    // The empty frame is not politeness: it is how the server learns the hello
    // is over, so it can paint instead of sitting out its whole timeout.
    check("...bare when there is no token, so the server can stop waiting",
          resumeFrames()[0] === "\u0001resume", resumeFrames()[0]);
    check("nothing was stored when nothing was issued", stored() === null,
          String(stored()));
  }

  // The server issues one.
  incoming("\u0001resume issued-token-one");
  check("an issued token is stored", stored() === "issued-token-one", String(stored()));
  check("...and never printed to the screen", lastLine().indexOf("issued-token") === -1,
        lastLine());

  // Rotation: a second push REPLACES rather than accumulating.
  incoming("\u0001resume issued-token-two");
  check("a rotated token replaces the old one", stored() === "issued-token-two",
        String(stored()));

  // The empty push is a revoke — .quit, .remember off, or an expired token.
  incoming("\u0001resume");
  check("an empty push clears the stored token", stored() === null, String(stored()));
  check("...leaving no key behind at all",
        Object.keys(store.data).indexOf("heresay-resume") === -1,
        JSON.stringify(Object.keys(store.data)));

  // Exactly one new storage key, and only while remembered.
  incoming("\u0001resume back-again");
  const keys = Object.keys(store.data).filter(k => k.indexOf("heresay-") === 0);
  check("the only new storage key is heresay-resume",
        keys.indexOf("heresay-resume") !== -1 &&
        keys.every(k => ["heresay-theme","heresay-raise","heresay-title","heresay-resume"]
                        .indexOf(k) !== -1),
        JSON.stringify(keys));

  // A reconnect presents whatever is stored NOW.
  const before = resumeFrames().length;
  connectAgain();
  check("a reconnect presents the current token",
        resumeFrames().length === before + 1 &&
        resumeFrames()[before] === "\u0001resume back-again",
        JSON.stringify(resumeFrames()));

  // Re-audit: a token is a message, not a request.
  const externals = HTML.match(/\b(fetch|XMLHttpRequest|EventSource|importScripts)\s*\(/g) || [];
  check("zero external requests: no fetch/XHR/EventSource anywhere",
        externals.length === 0, JSON.stringify(externals));
  finish();
} else if (process.env.INWORLD === "1") {
  // ---- the mark only blinks for a connected, in-world session (v1.25.1) ----
  //
  // Before this, a line arriving while the tab was hidden would blink the tab
  // during the greeting, at the name prompt, mid-password, and over a dead
  // socket. None of those are the room talking to you.
  //
  // NB: the shared setup above sends `\u0001inworld on`, so this mode starts by
  // undoing that — it needs the pre-login state a real page opens in.
  incoming("\u0001inworld off");
  harness.focused = false; fire(winListeners, "blur");

  check("a fresh page is NOT in-world until told", document.title === ORIGINAL,
        document.title);

  // 1. The login screen: hidden tab, lines arriving, nothing marks.
  incoming("Name (or 'quit'): ");
  incoming("Password: ");
  check("no blink at the login prompt", document.title === ORIGINAL,
        document.title);
  check("...and no dot on the favicon", iconHref() === "data:,", iconHref());
  check("...and no blink interval was started",
        intervals.filter(i => !i.cleared).length === 0,
        String(intervals.filter(i => !i.cleared).length));

  // 2. In-world: exactly as before.
  incoming("\u0001inworld on");
  incoming("Corvid says: hello");
  check("in-world, a hidden tab DOES mark", document.title !== ORIGINAL,
        document.title);
  check("...with the dot", iconHref() === DOT, iconHref());

  // 3. Dropping out of the world clears an ACTIVE blink at once — not at the
  //    next refocus, which would leave it blinking over a login prompt.
  incoming("\u0001inworld off");
  check("leaving the world clears the mark immediately",
        document.title === ORIGINAL, document.title);
  check("...and restores the favicon", iconHref() === "data:,", iconHref());
  check("...and stops the interval",
        intervals.filter(i => !i.cleared).length === 0,
        String(intervals.filter(i => !i.cleared).length));

  // 4. ...and it stays off afterwards.
  incoming("Corvid says: still nothing");
  check("back at the login screen, later lines do not mark",
        document.title === ORIGINAL, document.title);

  // 5. A dead socket never marks, even if the server last said in-world.
  incoming("\u0001inworld on");
  ws().onclose();
  check("a disconnect clears the in-world state", document.title === ORIGINAL,
        document.title);
  incoming("Corvid says: into the void");
  check("a disconnected tab does not mark", document.title === ORIGINAL,
        document.title);
  check("...and left no interval running",
        intervals.filter(i => !i.cleared).length === 0,
        String(intervals.filter(i => !i.cleared).length));
  finish();
} else if (process.env.SNAP === "1") {
  // ---- sending snaps the output to the newest line (v1.25.2) --------------
  //
  // Assertable because pinToBottom's rAF is setTimeout(0) in this sandbox, and
  // the DOM stub carries scrollTop/scrollHeight as plain numbers — so "scrolled
  // up" is just setting them, exactly as the .raise_prompt re-pin checks do.
  const scrolledUp = () => { byId.screen.scrollHeight = 5000; byId.screen.scrollTop = 0; };
  const atBottom = () => byId.screen.scrollTop === byId.screen.scrollHeight;

  // Drain the load-time rAF first. `applyRaise(initialRaise())` runs at page
  // load and calls pinToBottom, so a pin is already queued before this mode
  // starts; measuring without letting it fire credits THAT scroll to the send
  // and makes the first check pass on a client with no fix in it at all.
  // (It did, until this line — caught by running the mode against the previous
  // release with CLIENT=, which is what that override is for.)
  setTimeout(() => {

  scrolledUp();
  check("precondition: we are scrolled up, not at the bottom", !atBottom(),
        String(byId.screen.scrollTop));

  // 1. A line to the SERVER snaps, even though we had scrolled up to read back.
  typeLine(".who");
  setTimeout(() => {
    check("sending a command snaps to the newest line", atBottom(),
          byId.screen.scrollTop + "/" + byId.screen.scrollHeight);

    // 2. A CLIENT-only command snaps too — it never reaches the socket, but it
    //    still produces output you asked for and must be able to see.
    scrolledUp();
    typeLine(".theme green");
    setTimeout(() => {
      check("a client-only command snaps as well", atBottom(),
            byId.screen.scrollTop + "/" + byId.screen.scrollHeight);

      // 3. Ordinary speech (no leading dot) snaps.
      scrolledUp();
      typeLine("hello the room");
      setTimeout(() => {
        check("plain speech snaps", atBottom(),
              byId.screen.scrollTop + "/" + byId.screen.scrollHeight);

        // 4. The guards still guard: a masked prompt returns before anything,
        //    so Enter on a password neither sends nor scrolls.
        scrolledUp();
        const before = sent.length;
        byId.mask.hidden = false;
        typeLine("hunter2");
        byId.mask.hidden = true;
        setTimeout(() => {
          check("a masked prompt neither sends nor snaps",
                !atBottom() && sent.length === before,
                byId.screen.scrollTop + " sent+" + (sent.length - before));

          // 5. The pin is the EXISTING one, not new scroll logic: .raise_prompt
          //    still re-pins, which is what it shipped doing in v1.22.1.
          scrolledUp();
          typeLine(".raise_prompt 2");
          setTimeout(() => {
            check("the .raise_prompt re-pin still works", atBottom(),
                  String(byId.screen.scrollTop));
            check("...and its offset still applies", raiseValue() === "2",
                  raiseValue());
            finish();
          }, 30);
        }, 30);
      }, 30);
    }, 30);
  }, 30);

  }, 30);   // end of the drain wrapper
} else if (MASTER_OFF) {
  // The operator has switched the feature off in the code. NOTHING may mark the
  // tab — not a blurred line, and not a resident asking for it to be on.
  harness.focused = false; fire(winListeners, "blur");
  incoming("Corvid says: hello");
  check("master switch off: a blurred line does not mark", document.title === ORIGINAL,
        document.title);
  incoming("\u0001tab_alert on");            // the resident asks for it explicitly
  incoming("Corvid says: still nothing");
  check("master switch off: an explicit 'on' from the resident cannot re-enable it",
        document.title === ORIGINAL, document.title);
  check("master switch off: the favicon is untouched",
        iconHref() === "data:,", iconHref());
  const t = document.title;
  setTimeout(() => {
    check("master switch off: no blink interval exists at all", document.title === t,
          document.title);
    finish();
  }, 1500);
} else {

// ---- 1. active tab: an incoming line must NOT mark the tab ---------------
harness.focused = true; document.hidden = false;
incoming("Corvid says: hello");
check("active tab is left alone (title)", document.title === ORIGINAL, document.title);
check("active tab is left alone (favicon)", iconHref() === "data:,",
      iconHref());

// ---- 2. blurred window + incoming line -> title marked, favicon flipped --
harness.focused = false; fire(winListeners, "blur");
incoming("Corvid says: anyone there?");
check("blurred: title is marked at once", document.title === "● " + ORIGINAL, document.title);
check("blurred: favicon is the canvas dot", iconHref() === DOT,
      iconHref());

// ---- 3. it BLINKS (alternates on the interval) ---------------------------
const seen = new Set([document.title]);
setTimeout(() => {
  seen.add(document.title);
  setTimeout(() => {
    seen.add(document.title);
    check("the title alternates (blinks)",
          seen.has("● " + ORIGINAL) && seen.has(ORIGINAL),
          JSON.stringify([...seen]));

    // ---- 4. refocus clears both -------------------------------------------
    harness.focused = true; fire(winListeners, "focus");
    check("refocus restores the title exactly", document.title === ORIGINAL, document.title);
    check("refocus restores the original favicon",
          iconHref() === "data:,", iconHref());

    // ---- 5. the blink timer is really gone (no leak) -----------------------
    const titleAfterClear = document.title;
    setTimeout(() => {
      check("no orphan interval keeps blinking after clear",
            document.title === titleAfterClear, document.title);

      // ---- 6. hidden (Page Visibility) also raises, unhide clears ----------
      document.hidden = true; harness.focused = true;
      fire(docListeners, "visibilitychange");
      incoming("Wren waves.");
      check("hidden tab raises even while 'focused'",
            document.title === "● " + ORIGINAL, document.title);
      document.hidden = false;
      fire(docListeners, "visibilitychange");
      check("becoming visible clears it", document.title === ORIGINAL, document.title);
      check("becoming visible restores the favicon",
            iconHref() === "data:,", iconHref());

      // ---- 7. control frames must NOT mark the tab ------------------------
      harness.focused = false; fire(winListeners, "blur");
      incoming("\x01echo off");
      incoming("\x01echo on");
      check("control frames do not mark the tab", document.title === ORIGINAL, document.title);

      // ---- 8. ...but a real line still does, after a clear -----------------
      incoming("Corvid says: still here");
      check("a later line raises again", document.title === "● " + ORIGINAL, document.title);
      harness.focused = true; fire(winListeners, "focus");
      check("and clears again", document.title === ORIGINAL, document.title);


      // ---- 9. the resident's own opt-out (v1.19.0) -------------------------
      // `\x01tab_alert off` is the PERSON's switch, pushed by the server.
      harness.focused = true; fire(winListeners, "focus");   // clean slate
      incoming("\x01tab_alert off");
      check("the tab_alert frame is never printed",
            byId.screen.children.every(c => (c.textContent || "").indexOf("tab_alert") === -1));
      harness.focused = false; fire(winListeners, "blur");
      incoming("Corvid says: opted out");
      check("opted out: a line while blurred does NOT mark the tab",
            document.title === ORIGINAL, document.title);
      check("opted out: the favicon is untouched",
            iconHref() === "data:,", iconHref());

      // ---- 10. turning it back on restores the mark ------------------------
      incoming("\x01tab_alert on");
      incoming("Corvid says: opted back in");
      check("opted back in: the mark returns",
            document.title === "● " + ORIGINAL, document.title);
      harness.focused = true; fire(winListeners, "focus");
      check("and still clears on refocus", document.title === ORIGINAL, document.title);

      // ---- 11. switching OFF mid-blink clears immediately ------------------
      harness.focused = false; fire(winListeners, "blur");
      incoming("Corvid says: marking now");
      check("marked, before opting out", document.title === "● " + ORIGINAL, document.title);
      incoming("\x01tab_alert off");
      check("opting out mid-blink clears at once, not at next refocus",
            document.title === ORIGINAL, document.title);
      check("...and restores the favicon too",
            iconHref() === "data:,", iconHref());

      // ---- 12. THE REGRESSION (v1.19.1): the favicon LINK ELEMENT is replaced
      //
      // v1.19.0 mutated the existing link's href. The DOM read back correctly —
      // this harness passed — while the browser went on painting the old icon,
      // so the dot never came down on refocus. Asserting the href is therefore
      // not enough: what has to be true is that the ELEMENT is a new one, on
      // BOTH the set and the restore.
      harness.focused = true; fire(winListeners, "focus");
      incoming("\u0001tab_alert on");
      // (By now earlier tests have already cycled it, so the element in <head>
      // is a fresh blank one rather than the page's literal original — which is
      // itself the fix working. What matters is: one link, blank href.)
      const beforeRaise = iconEl_();
      check("baseline: exactly one icon link, showing the blank icon",
            iconLinks().length === 1 && iconHref() === "data:,");

      harness.focused = false; fire(winListeners, "blur");
      incoming("Corvid says: mark it");
      const dotEl = iconEl_();
      check("raise: the icon element was REPLACED, not mutated",
            dotEl !== null && dotEl !== beforeRaise);
      check("raise: the old element was removed from <head>",
            head.children.indexOf(beforeRaise) === -1 && beforeRaise.parentNode === null);
      check("raise: the new element is appended to <head> and carries the dot",
            head.children.indexOf(dotEl) !== -1 && iconHref() === DOT);

      harness.focused = true; fire(winListeners, "focus");
      const restoredEl = iconEl_();
      check("restore: the icon element was REPLACED again (the actual bug)",
            restoredEl !== null && restoredEl !== dotEl);
      check("restore: the dot element was removed from <head>",
            head.children.indexOf(dotEl) === -1 && dotEl.parentNode === null);
      check("restore: the fresh element carries the ORIGINAL blank icon",
            iconHref() === "data:,");

      // ---- 13. no accumulation over many cycles ---------------------------
      for (let i = 0; i < 5; i++) {
        harness.focused = false; fire(winListeners, "blur");
        incoming("Corvid says: cycle " + i);
        harness.focused = true; fire(winListeners, "focus");
      }
      check("exactly ONE icon link after repeated cycles (no pile-up)",
            iconLinks().length === 1, String(iconLinks().length));
      check("and it is the blank one", iconHref() === "data:,", String(iconHref()));

      const afterOptOut = document.title;
      setTimeout(async () => {
        check("...and kills the blink interval (no orphan timer)",
              document.title === afterOptOut, document.title);
        incoming("\x01tab_alert on");        // leave it on for the summary
        harness.focused = true; fire(winListeners, "focus");

        // ---- 14. .raise_prompt — SIGNED numbers (v1.22.0) -----------------
        //
        // Positive sets an absolute height; negative lowers BY that much from
        // where it is. The worked example from the spec is the load-bearing one.
        check("an offset is applied at load", raiseValue() !== "", raiseValue());
        check("the command is NOT forwarded to the server",
              typeLine(".raise_prompt 0") === false);
        check("0 is the floor", raiseValue() === "0", raiseValue());
        check("...and says so", lastLine() === "Input at the bottom edge.", lastLine());

        typeLine(".raise_prompt 4");
        check("a positive number SETS it (4 -> 4)", raiseValue() === "4", raiseValue());
        check("...reported", lastLine() === "Input raised 4 lines.", lastLine());
        typeLine(".raise_prompt -1");
        check("a negative LOWERS by that much (4, -1 -> 3)", raiseValue() === "3",
              raiseValue());
        typeLine(".raise_prompt -10");
        check("...and clamps at the min (3, -10 -> 0)", raiseValue() === "0",
              raiseValue());

        typeLine(".raise_prompt 2");
        check("absolute again, not relative (0 then 2 -> 2)", raiseValue() === "2",
              raiseValue());
        check("...singular reads right at 1",
              (typeLine(".raise_prompt 1"), lastLine()) && lastLine() === "Input raised 1 line.",
              lastLine());
        typeLine(".raise_prompt");
        check("bare reports without changing",
              raiseValue() === "1" && lastLine() === "Input raised 1 line.", lastLine());

        typeLine(".raise_prompt 999");
        check("clamped at the built-in max until the house says otherwise",
              raiseValue() === "10", raiseValue());

        for (const junk of ["sideways", "+", "-", "3.5", "one"]) {
          typeLine(".raise_prompt " + junk);
          check("junk (" + junk + ") gets the usage line",
                lastLine().indexOf("Usage: .raise_prompt") === 0, lastLine());
        }
        check("...and junk changed nothing", raiseValue() === "10", raiseValue());
        check("junk is still not forwarded",
              sent.indexOf(".raise_prompt sideways") === -1);
        check("the value is persisted", store.getItem("heresay-raise") === "10",
              String(store.getItem("heresay-raise")));

        // ---- 15. the house bounds frame (Part C) --------------------------
        incoming("\u0001raise_bounds 2 6");
        check("a now-too-high offset is RE-CLAMPED live (10 -> 6)",
              raiseValue() === "6", raiseValue());
        typeLine(".raise_prompt 0");
        check("the house FLOOR replaces 0 (min 2)", raiseValue() === "2", raiseValue());
        typeLine(".raise_prompt 99");
        check("the house CEILING replaces the built-in (max 6)", raiseValue() === "6",
              raiseValue());
        incoming("\u0001raise_bounds 0 10");     // back to the defaults
        check("bounds can be widened again", raiseValue() === "6", raiseValue());

        // ---- 14b. the raise re-pins the output scroll (v1.22.1) -----------
        //
        // A DOM harness cannot see visual overlap, so assert the two mechanical
        // things it CAN: the offset still lands on the custom property the CSS
        // consumes (the fix moved which rule reads it, not where JS writes it),
        // and changing the offset re-pins the scrolling output to the bottom —
        // a shrinking scroll box otherwise strands the newest line above the
        // fold, which looks exactly like the bug being fixed.
        byId.screen.scrollHeight = 5000;
        byId.screen.scrollTop = 0;
        typeLine(".raise_prompt 3");
        check("the offset still lands on --raise-lines", raiseValue() === "3",
              raiseValue());
        await frame();          // the re-pin waits one rAF for layout
        check("raising re-pins the output to the bottom",
              byId.screen.scrollTop === 5000, String(byId.screen.scrollTop));

        byId.screen.scrollTop = 0;
        typeLine(".raise_prompt 0");
        await frame();
        check("lowering re-pins it too", byId.screen.scrollTop === 5000,
              String(byId.screen.scrollTop));
        check("...and 0 is still flush", raiseValue() === "0", raiseValue());

        // ---- 16. the house name + favicon frames (Part D) -----------------
        // v1.22.2: the placeholder is NEUTRAL. Naming the engine here made a
        // returning visitor read "Heresay" until the socket opened, then snap.
        check("the pre-connect placeholder is not the engine's name",
              store.__titleAtLoad !== "Heresay" && !!store.__titleAtLoad,
              String(store.__titleAtLoad));
        incoming("\u0001title Commons");
        check("the tab takes the HOUSE's name", document.title === "Commons",
              document.title);
        const HOUSE_ICON = "data:image/svg+xml;base64,BUNNY";
        incoming("\u0001favicon " + HOUSE_ICON);
        check("the house favicon is applied", iconHref() === HOUSE_ICON, iconHref());
        check("...as a fresh link element", iconLinks().length === 1);

        // ---- 16b. the house name is CACHED for the next load (v1.22.2) -----
        check("the title frame wrote the name to localStorage",
              store.getItem("heresay-title") === "Commons",
              String(store.getItem("heresay-title")));

        // ---- 16c. the blink runs off ONE steady interval -------------------
        //
        // The uneven blink was a 1200ms period beating against a hidden tab's
        // ~1s timer clamp. The period is 1000 now, and the state is derived from
        // ELAPSED TIME rather than counted ticks — so a throttled, late tick
        // paints the state that moment should be in instead of inverting the
        // phase for the rest of the run. Driven here with a pinned clock.
        harness.focused = true; fire(winListeners, "focus");   // clear first
        intervals.length = 0;
        harness.fakeNow = 500000;
        harness.focused = false; fire(winListeners, "blur");
        incoming("Corvid says: start the blink");
        check("exactly ONE interval drives the blink", intervals.length === 1,
              String(intervals.length));
        const period = intervals[0].ms;
        check("the period is 1000ms (clean against the ~1s hidden-tab clamp)",
              period === 1000, String(period));
        check("t+0 shows the dot", iconHref() === DOT, iconHref());

        const tick = (elapsed) => {
          harness.fakeNow = 500000 + elapsed;
          intervals[0].fn();
        };
        tick(1000);
        check("t+1000 shows the normal icon", iconHref() === HOUSE_ICON, iconHref());
        tick(2000);
        check("t+2000 shows the dot again (symmetric: 1s in each state)",
              iconHref() === DOT, iconHref());
        tick(3000);
        check("t+3000 normal", iconHref() === HOUSE_ICON, iconHref());
        // A LATE tick — the throttled case. A tick-counter would invert here and
        // stay wrong; deriving from elapsed time paints what t+3500 should be.
        tick(3500);
        check("a LATE tick self-corrects rather than inverting the phase",
              iconHref() === HOUSE_ICON, iconHref());
        // Two whole periods skipped: still the right state for the moment.
        tick(7000);
        check("a tick after two skipped periods is still in phase",
              iconHref() === HOUSE_ICON, iconHref());
        tick(8000);
        check("...and the next one flips as it should", iconHref() === DOT,
              iconHref());
        check("still exactly one interval — never restarted while raised",
              intervals.length === 1, String(intervals.length));
        incoming("Corvid says: more activity");
        check("further activity does not start a second interval",
              intervals.length === 1, String(intervals.length));
        harness.focused = true; fire(winListeners, "focus");
        // ---- 17. THE POINT: the FAVICON blinks, in step with the title -----
        //
        // A background tab is often too narrow to show any title text, so a
        // title-only blink signals nothing. The icon is the part you can see.
        //
        // Driven on the PINNED CLOCK, like section 16. It used to run on real
        // rampart-clock timers — `fakeNow = null` and two nested 1300ms
        // setTimeouts sampling a 1000ms blink — which flaked under suite load:
        // when the event loop is contended the timers fire late and every
        // sample lands in the same phase, so the alternation is never observed.
        // (Reproduced 1-in-30 with parallel harness processes.) Sampling the
        // interval directly at chosen instants tests the same thing and cannot
        // be starved.
        intervals.length = 0;
        harness.fakeNow = 900000;
        harness.focused = false; fire(winListeners, "blur");
        incoming("Corvid says: look at the tab");
        check("marked: the title carries the house name", document.title === "● Commons",
              document.title);
        check("marked: the favicon is the alert dot", iconHref() === DOT, iconHref());

        const blink = intervals.find(i => !i.cleared && i.ms === 1000);
        const seenIcons = new Set([iconHref()]);
        const seenTitles = new Set([document.title]);
        // One full period each way: t+1000 must be the house icon, t+2000 the
        // dot again. Both states get seen, deterministically.
        [1000, 2000].forEach((elapsed) => {
          harness.fakeNow = 900000 + elapsed;
          if (blink) blink.fn();
          seenIcons.add(iconHref()); seenTitles.add(document.title);
        });
        check("the FAVICON alternates dot <-> house icon",
              seenIcons.has(DOT) && seenIcons.has(HOUSE_ICON),
              JSON.stringify([...seenIcons].map(x => x.slice(0, 30))));
        check("the title alternates in step",
              seenTitles.has("● Commons") && seenTitles.has("Commons"),
              JSON.stringify([...seenTitles]));
        check("still exactly one icon link through the blinking",
              iconLinks().length === 1, String(iconLinks().length));

        harness.focused = true; fire(winListeners, "focus");
        check("clear restores the HOUSE title, not the engine's",
              document.title === "Commons", document.title);
        check("clear restores the HOUSE favicon", iconHref() === HOUSE_ICON,
              iconHref());
        // An orphan blink would be an interval still live after the clear:
        // advance the clock and fire anything that survived. Nothing should move.
        const after = iconHref();
        harness.fakeNow = 900000 + 3000;
        intervals.filter(i => !i.cleared).forEach(i => i.fn());
        check("no orphan blink after clear", iconHref() === after, iconHref());
        check("...and the title stayed put", document.title === "Commons",
              document.title);
        harness.fakeNow = null;
        finish();

      }, 1500);
    }, 1500);
  }, 1300);
}, 1300);
}

function finish() {
  console.log("\n" + (fail === 0
    ? `ALL ${pass} CHECKS PASSED`
    : `${fail} FAILED, ${pass} passed`));
  process.exit(fail === 0 ? 0 : 1);
}
