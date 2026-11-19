// Regression test for the orb's microphone downsampler.
//
// The shipped worklet had a bug that made JARVIS unable to hear spoken input:
// it read `ch[o]` (the OUTPUT index) instead of the input position for each
// output sample, so it kept only the first third of every 128-sample block and
// stitched those fragments together. Crucially it still emitted exactly the
// right NUMBER of samples, so any length-based check passed while Whisper and
// the wake word received garbage. These checks therefore measure CONTENT.
//
// The worklet is extracted from orb/static/index.html at run time - there is no
// copy here that could drift away from what the orb actually loads.
//
//   node orb/test_worklet_decimation.js

const fs = require("fs");
const path = require("path");

const HTML = fs.readFileSync(path.join(__dirname, "static", "index.html"), "utf8");
const MARKER = "const code = `";
const start = HTML.indexOf(MARKER);
if (start < 0) throw new Error("worklet block not found in static/index.html");
const s = start + MARKER.length;
const e = HTML.indexOf("`;", s);
if (e < 0) throw new Error("unterminated worklet block in static/index.html");
// Inside the page this is a template literal, so backticks arrive escaped.
const WORKLET = HTML.slice(s, e).replace(/\\\x60/g, "'");

if (!WORKLET.includes("class JWorklet") || !WORKLET.includes("registerProcessor")) {
  throw new Error("extracted text does not look like the AudioWorklet source");
}

class AudioWorkletProcessor {
  constructor() { this.port = { postMessage() {} }; }
}

function buildProcessor(sampleRate) {
  const factory = new Function(
    "sampleRate", "AudioWorkletProcessor", "Int16Array", "registerProcessor",
    WORKLET.replace("registerProcessor('jworklet', JWorklet);", "return JWorklet;")
  );
  return factory(sampleRate, AudioWorkletProcessor, Int16Array, () => {});
}

// Drive it one 128-sample render quantum at a time, exactly like the browser.
function capture(source, sampleRate, kind) {
  const P = buildProcessor(sampleRate);
  const p = new P();
  const frames = [];
  p.port.postMessage = (m) => { if (m && m.pcm) frames.push(new Int16Array(m.pcm)); };
  // A real AudioWorklet receives inputs[inputIndex][channelIndex], hence the
  // double nesting. Passing one level makes ch a single number and the
  // worklet's own guard correctly bails out - that was a harness bug, not a
  // product bug, and it cost time to find.
  for (let i = 0; i + 128 <= source.length; i += 128) {
    p.process([[Float32Array.from(source.slice(i, i + 128))]]);
  }
  const total = frames.reduce((a, f) => a + f.length, 0);
  const out = new Int16Array(total);
  let off = 0;
  for (const f of frames) { out.set(f, off); off += f.length; }
  return out;
}

function tone(sr, seconds, hz, amp) {
  const n = new Float32Array(sr * seconds);
  for (let i = 0; i < n.length; i++) n[i] = amp * Math.sin(2 * Math.PI * hz * i / sr);
  return n;
}

function sineChirp(sr, seconds, f0, f1, amp) {
  const n = new Float32Array(sr * seconds);
  let phase = 0;
  for (let i = 0; i < n.length; i++) {
    const t = i / (sr * seconds);
    const hz = f0 + (f1 - f0) * t;
    phase += (2 * Math.PI * hz) / sr;
    n[i] = amp * Math.sin(phase);
  }
  return n;
}

// Full-rate DFT. Striding the input makes the effective rate lower and mirrors
// high frequencies back (440 Hz read as 7560 Hz in an earlier version of this
// very test), so every sample is used.
function dominantHz(samples, rate) {
  let bestF = 0, bestMag = 0;
  const N = samples.length;
  for (let f = 50; f < rate / 2 - 200; f += 10) {
    let re = 0, im = 0;
    for (let n = 0; n < N; n++) {
      const x = samples[n] / 32768;
      const ang = (-2 * Math.PI * f * n) / rate;
      re += x * Math.cos(ang);
      im += x * Math.sin(ang);
    }
    const mag = Math.hypot(re, im);
    if (mag > bestMag) { bestMag = mag; bestF = f; }
  }
  return bestF;
}

function peakAbs(samples) {
  let peak = 0;
  for (let i = 0; i < samples.length; i++) peak = Math.max(peak, Math.abs(samples[i]));
  return peak;
}

let failures = 0;
function check(label, pass, detail) {
  console.log(`${pass ? "PASS" : "FAIL"}  ${label}${detail ? " - " + detail : ""}`);
  if (!pass) failures++;
}

const DEVICE_RATES = [48000, 44100, 24000, 16000];
for (const sr of DEVICE_RATES) {
  const out = capture(tone(sr, 1, 440, 0.25), sr);
  const secs = out.length / 16000;
  const hz = dominantHz(out, 16000);
  const peak = peakAbs(out);
  check(`${(sr / 1000).toFixed(1)} kHz: duration`, Math.abs(secs - 1) < 0.03,
    `${secs.toFixed(3)} s from 1.000 s of mic audio`);
  check(`${(sr / 1000).toFixed(1)} kHz: pitch preserved`, Math.abs(hz - 440) <= 30,
    `440 Hz in -> ${hz} Hz out`);
  check(`${(sr / 1000).toFixed(1)} kHz: level preserved`, peak > 6000 && peak < 10000,
    `0.25 full-scale -> peak ${peak}`);
}

// The check that the original bug actually fails: a 200 -> 3000 Hz chirp must
// still be climbing at the end. Sample *count* was correct in the broken
// version; only the content gives it away.
const chirp = capture(sineChirp(48000, 1, 200, 3000, 0.3), 48000);
const tail = chirp.subarray(Math.floor(chirp.length * 0.85));
const tailHz = dominantHz(tail, 16000);
check("no audio dropped (chirp tail)", tailHz > 2400,
  `final 15% of a 200->3000 Hz sweep measures ${tailHz} Hz (broken reader stayed ~1000 Hz)`);

// Frames must keep arriving in the 320-sample (20 ms) shapes the server expects.
const P = buildProcessor(48000);
const p = new P();
const sizes = [];
p.port.postMessage = (m) => { if (m && m.pcm) sizes.push(new Int16Array(m.pcm).length); };
for (let i = 0; i < 4800; i += 128) {
  p.process([[Float32Array.from(tone(48000, 0.1, 440, 0.25).slice(i, i + 128))]]);
}
check("emits 320-sample frames (20 ms at 16 kHz)", sizes.length > 0 && sizes.every(n => n % 320 === 0),
  `frame sizes: ${[...new Set(sizes)].join(", ")}`);

console.log(failures ? `\n${failures} check(s) failed` : `\nall ${DEVICE_RATES.length * 3 + 2} checks passed`);
process.exit(failures ? 1 : 0);
