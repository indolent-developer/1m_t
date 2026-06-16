// Run once with: node make_icons.js
// Creates simple icon PNGs using the Canvas API (Node + canvas package)
// If you don't want to run this, just use any 16x16 / 48x48 / 128x128 PNG files.

const { createCanvas } = require("canvas");
const fs = require("fs");

function makeIcon(size) {
  const c   = createCanvas(size, size);
  const ctx = c.getContext("2d");
  // Background
  ctx.fillStyle = "#0f1419";
  ctx.fillRect(0, 0, size, size);
  // Circle
  ctx.fillStyle = "#0d7a47";
  ctx.beginPath();
  ctx.arc(size/2, size/2, size*0.42, 0, Math.PI*2);
  ctx.fill();
  // "K" text
  ctx.fillStyle = "#fff";
  ctx.font = `bold ${Math.floor(size*0.55)}px sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText("K", size/2, size/2 + size*0.03);
  return c.toBuffer("image/png");
}

for (const s of [16, 48, 128]) {
  fs.writeFileSync(`icons/icon${s}.png`, makeIcon(s));
  console.log(`icons/icon${s}.png`);
}
