// TRMNL "bring your own server" API on Cloudflare Workers.
//
// Every device gets the same card for each REFRESH_SECONDS window; button presses re-show the current
// card instead of advancing it. Cards and their pre-rendered images are uploaded to KV by build.py:
//   manifest               { active: [card ids in rotation order], sizes: [[w, h], ...], rendered: {...} }
//   frame:<id>:<w>x<h>     1-bit PNG of the card for that screen size

const GRACE_SECONDS = 15; // device sleep timers drift; a wake this early still gets the next card
const MAX_SPREAD_SECONDS = 30; // random extra sleep so many devices don't all download at once
const DEFAULT_SIZE = "800x480"; // Xteink X4
const MANIFEST_TTL_MS = 10 * 60 * 1000;

let cachedManifest = null;
let cachedAt = 0;

async function getManifest(env) {
  if (!cachedManifest || Date.now() - cachedAt > MANIFEST_TTL_MS) {
    const manifest = await env.FRAMES.get("manifest", { type: "json", cacheTtl: 300 });
    if (manifest) {
      cachedManifest = manifest;
      cachedAt = Date.now();
    }
  }
  return cachedManifest;
}

function refreshSeconds(env) {
  return Number(env.REFRESH_SECONDS) || 300;
}

function currentCard(manifest, env) {
  const period = refreshSeconds(env);
  const slot = Math.floor((Date.now() / 1000 + GRACE_SECONDS) / period);
  // Walking the list in order shows every card once before any repeats.
  const id = manifest.active[slot % manifest.active.length];
  return { id, changesAt: (slot + 1) * period };
}

function screenSize(request, manifest) {
  const size = `${request.headers.get("Width")}x${request.headers.get("Height")}`;
  return (manifest.sizes || []).some(([w, h]) => `${w}x${h}` === size) ? size : DEFAULT_SIZE;
}

function frameUrl(origin, id, size) {
  return `${origin}/frames/${encodeURIComponent(id)}/${size}.png`;
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function notReady() {
  // Before build.py's first run there's nothing to show; the device shows an error and retries.
  return json({ status: 500, error: "No cards uploaded yet" }, 503);
}

async function handleDisplay(request, env, origin) {
  const manifest = await getManifest(env);
  if (!manifest?.active?.length) return notReady();
  const size = screenSize(request, manifest);
  const { id, changesAt } = currentCard(manifest, env);
  const spread = Math.floor(Math.random() * (Math.min(MAX_SPREAD_SECONDS, refreshSeconds(env) / 10) + 1));
  const refresh = Math.max(5, Math.ceil(changesAt - Date.now() / 1000) + spread);
  const h = request.headers;
  console.log(`[display] ${h.get("ID")} woke by ${h.get("Update-Source")} ${size} fw=${h.get("FW-Version")} ` +
    `battery=${h.get("Battery-Voltage")}V -> ${id}, next in ${refresh}s`);
  return json({
    status: 0,
    image_url: frameUrl(origin, id, size),
    filename: `${id}-${size}`, // unchanged name = the device skips re-downloading
    refresh_rate: refresh,
    reset_firmware: false,
    update_firmware: false,
    firmware_url: null,
  });
}

async function handleSetup(request, env, origin) {
  const manifest = await getManifest(env);
  if (!manifest?.active?.length) return notReady();
  console.log(`[setup] ${request.headers.get("ID")} model=${request.headers.get("Model")}`);
  return json({
    status: 200,
    api_key: "cards",
    friendly_id: "CARDS",
    image_url: frameUrl(origin, currentCard(manifest, env).id, screenSize(request, manifest)),
    message: "Registered",
  });
}

async function handleFrame(match, env) {
  const key = `frame:${decodeURIComponent(match[1])}:${match[2]}`;
  const png = await env.FRAMES.get(key, { type: "arrayBuffer", cacheTtl: 3600 });
  if (!png) return new Response("Not found", { status: 404 });
  return new Response(png, {
    headers: { "Content-Type": "image/png", "Cache-Control": "public, max-age=86400" },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/api/display") return handleDisplay(request, env, url.origin);
    if (request.method === "GET" && url.pathname === "/api/setup") return handleSetup(request, env, url.origin);
    if (request.method === "POST" && url.pathname === "/api/log") {
      console.log(`[device log] ${request.headers.get("ID")} ${await request.text()}`);
      return json({ status: 200 });
    }
    const frame = url.pathname.match(/^\/frames\/([^/]+)\/(\d+x\d+)\.png$/);
    if (request.method === "GET" && frame) return handleFrame(frame, env);
    if (url.pathname === "/") return new Response("TRMNL card server\n");
    return new Response("Not found", { status: 404 });
  },
};
