// Receives tips from the "Send in a weekly special" form at /specials/add/ and saves them to
// the Submissions table in the Food Specials Airtable base. Every other request is served from
// the static site as before (wrangler.jsonc routes only /api/* through this script).
// Needs the AIRTABLE_TOKEN secret (a token with data.records:write on the Food Specials base).

const BASE = "appjDfoI3OOGJjmrA";
const TABLE = "tbl29x8Y0sMgogElD";
const F = {
  restaurant: "fldjukvvCeq7dHso2",
  screenshot: "fld7lbpvTJPvC1bYE",
  special: "fldPQYFQK2qf2zYux",
  link: "fld9TQr731HkMBiqB",
  name: "fldHjFugBzRTZns3U",
  status: "fldDleirjhiZY5t8Z",
  address: "flde8M3HT6IzBmyIX",
  lat: "fldtQRDyHtJpNW0qp",
  lng: "fldmQlMHlQspfngBN",
};
const MAX_IMAGE_BYTES = 4_500_000; // Airtable's upload limit is 5 MB; the page shrinks photos well below this
const IMAGE_TYPES = /^image\/(jpeg|png|webp)$/;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/api/specials-tip") return handleTip(request, env);
    return env.ASSETS.fetch(request);
  },
};

async function handleTip(request, env) {
  if (request.method !== "POST") return reply({ error: "Use POST." }, 405);
  if (!env.AIRTABLE_TOKEN) return reply({ error: "Sending isn't set up yet. Try again later." }, 503);
  let body;
  try {
    body = await request.json();
  } catch {
    return reply({ error: "Something went wrong. Try again." }, 400);
  }
  // Bots fill the hidden "website" field or submit faster than a person can. Pretend it worked.
  if (body.website || (Number(body.elapsedMs) || 0) < 3000) return reply({ ok: true });

  const text = (value, max) => (typeof value === "string" ? value.trim().slice(0, max) : "");
  const restaurant = text(body.restaurant, 120);
  const special = text(body.special, 2000);
  const image = body.image && typeof body.image.data === "string" && IMAGE_TYPES.test(body.image.type) ? body.image : null;
  if (!restaurant) return reply({ error: "Tell us which restaurant." }, 400);
  if (!special && !image) return reply({ error: "Add a photo or describe the deal." }, 400);
  if (image && image.data.length * 0.75 > MAX_IMAGE_BYTES) return reply({ error: "That photo is too big. Try a screenshot instead." }, 400);

  const fields = { [F.restaurant]: restaurant, [F.status]: "New" };
  if (special) fields[F.special] = special;
  const address = text(body.address, 200);
  if (address) fields[F.address] = address;
  const link = text(body.link, 500);
  if (/^https?:\/\/\S+$/i.test(link)) fields[F.link] = link;
  const name = text(body.name, 40);
  if (name) fields[F.name] = name;
  const lat = Number(body.lat), lng = Number(body.lng);
  if (body.lat != null && Number.isFinite(lat) && Number.isFinite(lng) && Math.abs(lat) <= 90 && Math.abs(lng) <= 180) {
    fields[F.lat] = Math.round(lat * 1e6) / 1e6;
    fields[F.lng] = Math.round(lng * 1e6) / 1e6;
  }

  const created = await airtable(env, `https://api.airtable.com/v0/${BASE}/${TABLE}`, { records: [{ fields }] });
  if (!created.ok) return reply({ error: "Couldn't save that right now. Try again in a minute." }, 502);
  const recordId = (await created.json()).records[0].id;

  if (image) {
    const uploaded = await airtable(env, `https://content.airtable.com/v0/${BASE}/${recordId}/${F.screenshot}/uploadAttachment`, {
      contentType: image.type,
      file: image.data,
      filename: text(image.name, 80) || "photo.jpg",
    });
    if (!uploaded.ok) return reply({ ok: true, note: "Saved, but the photo didn't upload." });
  }
  return reply({ ok: true });
}

function airtable(env, url, payload) {
  return fetch(url, {
    method: "POST",
    headers: { Authorization: `Bearer ${env.AIRTABLE_TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

function reply(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}
