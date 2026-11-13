const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../static/js/player.js"), "utf8");
const context = {
  document: {
    getElementById() { return null; },
    addEventListener() {},
    readyState: "loading",
    createElement() { return { canPlayType: () => "" }; },
  },
  navigator: {},
};
context.window = context;
vm.runInNewContext(source, context);
const hdr = context.StudioPlayerHdr;

const hevc = { is_hdr: true, codec: "hevc", height: 2160, dolby_vision: false, dv_profile: 0 };
assert.equal(hdr.contentType(hevc).contentType, 'video/mp4; codecs="hvc1.2.4.L153.B0"');
const playable = hdr.choice(hevc, true);
assert.equal(playable.on, false);
assert.equal(playable.codec, "hevc");
assert.match(playable.reason, /bleibt aus/);
assert.equal(hdr.choice(hevc, false).on, true);
assert.equal(hdr.choice({ is_hdr: true, codec: "hevc", dolby_vision: true, dv_profile: 5 }, true).on, true);
assert.equal(hdr.choice({ is_hdr: true, codec: "vp9", height: 2160 }, true).on, true);
assert.equal(hdr.choice({ is_hdr: false, codec: "hevc" }, false).on, false);
console.log("HDR-Ladecheck: OK");
