"use strict";
// Settings card for white labelling: brand colours, tagline and logo.

const BRAND_PRESETS = [["Blue", "#3b9eff"], ["Teal", "#14b8a6"], ["Green", "#22c55e"], ["Amber", "#f59e0b"],
  ["Orange", "#f97316"], ["Red", "#ef4444"], ["Pink", "#ec4899"], ["Purple", "#a855f7"], ["Slate", "#64748b"]];
const MAX_LOGO_BYTES = 400 * 1024;

// Any raster image -> PNG no larger than 800x400 (keeps transparency, strips metadata, keeps the upload small).
async function logoToPng(file) {
  if (!/^image\/(png|jpeg|gif|webp)$/.test(file.type)) throw new Error("Choose a PNG, JPEG, GIF or WebP image.");
  let bmp;
  try { bmp = await createImageBitmap(file); } catch (e) { throw new Error("That image could not be read."); }
  const scale = Math.min(1, 800 / bmp.width, 400 / bmp.height);
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(bmp.width * scale));
  canvas.height = Math.max(1, Math.round(bmp.height * scale));
  canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
  if (!blob) throw new Error("That image could not be converted.");
  if (blob.size > MAX_LOGO_BYTES) throw new Error("This logo is too detailed. Use a simpler or smaller image.");
  return blob;
}

async function blobToBase64(blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

function brandingCard() {
  const saved = BRAND || { primary_color: "", header_color: "", tagline: "", show_name: true, has_logo: false, logo_version: "" };
  const st = { primary: saved.primary_color, header: saved.header_color };
  const msg = h("span", { class: "muted" });
  const logoMsg = h("div", { class: "error", role: "alert" });
  const preview = () => paintBrand({ primary_color: st.primary, header_color: st.header });

  const colorPicker = (key, fallback, title, help) => {
    const input = h("input", { type: "color", value: st[key] || fallback, "aria-label": title });
    input.addEventListener("input", () => { st[key] = input.value; preview(); });
    const sw = h("div", { class: "swatches" }, BRAND_PRESETS.map(([n, c]) => h("button", { type: "button", class: "swatch", title: n, "aria-label": n,
      onclick: () => { st[key] = c; input.value = c; preview(); } })));
    sw.querySelectorAll(".swatch").forEach((el, i) => { el.style.background = BRAND_PRESETS[i][1]; });
    const reset = h("button", { type: "button", onclick: () => { st[key] = ""; input.value = fallback; preview(); } }, "Use default");
    return h("div", { class: "color-row" }, h("label", null, title), h("div", { class: "color-controls" }, input, sw, reset), h("p", { class: "muted small" }, help));
  };

  const tagline = h("input", { value: saved.tagline, maxlength: 120, placeholder: "e.g. Fast, friendly repairs since 2009" });
  const showName = h("input", { type: "checkbox", checked: saved.show_name });

  const logoImg = h("img", { class: "logo-preview", alt: "Current logo" });
  const logoBox = h("div", { class: "logo-box" });
  const file = h("input", { type: "file", accept: "image/png,image/jpeg,image/gif,image/webp", "aria-label": "Upload logo" });
  function drawLogo() {
    const url = logoUrl();
    if (url) { logoImg.src = url; logoBox.replaceChildren(logoImg); }
    else logoBox.replaceChildren(h("span", { class: "muted" }, "No logo yet"));
    remove.style.display = url ? "" : "none";
  }
  const remove = h("button", { type: "button", onclick: async () => {
    logoMsg.textContent = "";
    try { const r = await api("DELETE", "/api/branding/logo"); setBrand({ ...BRAND, has_logo: r.has_logo, logo_version: r.logo_version }); preview(); drawLogo(); refreshBrandHeader(); }
    catch (e) { logoMsg.textContent = e.message; }
  } }, "Remove logo");
  file.addEventListener("change", async () => {
    logoMsg.textContent = "";
    const f = file.files[0];
    if (!f) return;
    try {
      const data = await blobToBase64(await logoToPng(f));
      const r = await api("POST", "/api/branding/logo", { data });
      setBrand({ ...BRAND, has_logo: r.has_logo, logo_version: r.logo_version }); preview(); drawLogo(); refreshBrandHeader();
    } catch (e) { logoMsg.textContent = e.message; }
    file.value = "";
  });
  drawLogo();

  return h("section", { class: "card" }, h("h2", null, "Branding"),
    h("p", { class: "muted small" }, "Your colours and logo appear throughout ShopDesk for your whole team, and on printed invoices."),
    h("form", { onsubmit: async (e) => {
        e.preventDefault(); msg.className = "muted"; msg.textContent = "Saving…";
        try {
          const r = await api("PUT", "/api/branding", { primary_color: st.primary, header_color: st.header, tagline: tagline.value, show_name: showName.checked });
          setBrand(r); refreshBrandHeader(); msg.className = "ok"; msg.textContent = "Saved";
        } catch (err) { msg.className = "error"; msg.textContent = err.message; }
      } },
      colorPicker("primary", "#3b9eff", "Brand colour", "Buttons, highlights, active tabs and the invoice accent."),
      colorPicker("header", "#182029", "Top bar colour", "Background of the bar at the top of every page."),
      h("h3", null, "Logo"), logoBox,
      h("div", { class: "form-actions wrap" }, file, remove), logoMsg,
      h("p", { class: "muted small" }, "PNG, JPEG, GIF or WebP. Large images are shrunk automatically. A wide logo on a transparent background looks best."),
      checkRow("Show the business name next to the logo", showName),
      field("Tagline (printed under your name on invoices)", tagline),
      h("div", { class: "form-actions" }, h("button", { class: "primary", type: "submit" }, "Save branding"), msg)));
}
