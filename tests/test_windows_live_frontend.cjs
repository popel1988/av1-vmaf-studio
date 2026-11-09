const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const source = fs.readFileSync(path.join(__dirname, "../static/js/app.js"), "utf8");
const start = source.indexOf("  let lastLiveUpdate = 0;");
const end = source.indexOf("  /* ------------------------------------------------------------ HARDWARE */", start);
assert.ok(start >= 0 && end > start);

(async () => {
  let now = 100000, fail = false, intervals = 0;
  const requests = [], hardware = [], queues = [];
  const elements = {
    "conn-dot": { classList: { toggle() {} } },
    "conn-text": { textContent: "" },
  };
  let socket;
  const context = vm.createContext({
    Date: { now: () => now },
    Promise, Error, JSON, AbortSignal,
    location: { protocol: "http:", host: "127.0.0.1:8080" },
    WebSocket: class {
      constructor() { socket = this; }
    },
    setInterval() { intervals++; return 1; },
    setTimeout() {},
    $: (id) => elements[id],
    updateHardware: (value) => hardware.push(value),
    updateQueue: (value) => queues.push(value),
    fetch: async (url) => {
      requests.push(url);
      return { ok: !fail, status: fail ? 503 : 200, json: async () => ({ url }) };
    },
  });
  vm.runInContext(source.slice(start, end), context);
  vm.runInContext("connectWs()", context);
  await new Promise(setImmediate);
  assert.equal(hardware.length, 1, "Initial HTTP fallback displays hardware");
  assert.equal(queues.length, 1);
  assert.match(elements["conn-text"].textContent, /HTTP/);
  vm.runInContext("connectWs()", context);
  assert.equal(intervals, 1, "Reconnection must not duplicate polling timers");

  socket.onmessage({ data: JSON.stringify({ hardware: { cpu_percent: 12 }, queue: {} }) });
  const before = requests.length;
  await vm.runInContext("pollLiveFallback()", context);
  assert.equal(requests.length, before, "Healthy WebSocket suppresses polling");
  now += 6000;
  await vm.runInContext("pollLiveFallback()", context);
  assert.equal(requests.length, before + 2, "Silent WebSocket enables fallback");

  fail = true;
  await vm.runInContext("pollLiveFallback()", context);
  fail = false;
  await vm.runInContext("pollLiveFallback()", context);
  assert.equal(hardware.length, 4, "HTTP failures must not leave polling locked");
  console.log("Windows live fallback: OK");
})().catch((error) => { console.error(error); process.exitCode = 1; });
