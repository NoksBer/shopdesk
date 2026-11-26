"use strict";
// Ticket notes with screenshots and files. Loaded before app.js; uses h(), api(), csrf, can(), enc, route() from it.

const NOTE_FILE_MAX = 10 * 1024 * 1024, NOTE_FILES_MAX = 10;
const NOTE_ACCEPT = ".png,.jpg,.jpeg,.gif,.webp,.pdf,.txt,.log,.csv,.md,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.zip";
const fmtBytes = (n) => (n < 1024 ? n + " B" : n < 1048576 ? Math.round(n / 1024) + " KB" : (n / 1048576).toFixed(1) + " MB");

async function uploadTicketFile(ticketId, file) {          // raw body = the file; the server checks name, type and contents
  const res = await fetch("/api/tickets/" + enc(ticketId) + "/files", { method: "POST", credentials: "same-origin",
    headers: { "X-CSRF-Token": csrf, "X-Filename": enc(file.name), "Content-Type": "application/octet-stream" }, body: file });
  let data = {};
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) throw new Error(data.error || "Upload failed (" + res.status + ")");
  return data;
}

function noteFile(ticketId, f) {
  const url = "/api/tickets/" + enc(ticketId) + "/files/" + enc(f.id);
  const link = h("a", { class: "file-chip", href: url + "?download=1" }, "📎 " + f.filename + " (" + fmtBytes(f.size) + ")");
  return h("div", { class: "note-file" },
    f.is_image ? h("a", { href: url, target: "_blank", rel: "noopener" },
      h("img", { class: "note-thumb", src: url, alt: f.filename, loading: "lazy" })) : null, link);
}

async function ticketNotesCard(t) {
  const { notes } = await api("GET", "/api/tickets/" + enc(t.id) + "/notes");
  const card = h("section", { class: "card" }, h("h2", null, "Notes & attachments"));
  const err = h("div", { class: "error", role: "alert" });

  if (can("tickets.write")) {
    const text = h("textarea", { rows: "3", maxlength: 4000, placeholder: "Add a note. Paste a screenshot here, drop files, or use Attach." });
    const picker = h("input", { type: "file", multiple: true, accept: NOTE_ACCEPT, hidden: true });
    const chips = h("div", { class: "pending-files" });
    const addBtn = h("button", { class: "primary", type: "button" }, "Add note");
    let pending = [], busy = 0;

    const drawChips = () => chips.replaceChildren(...pending.map((f) => h("span", { class: "file-chip" }, "📎 " + f.filename + " (" + fmtBytes(f.size) + ") ",
      h("button", { type: "button", "aria-label": "Remove " + f.filename, onclick: async () => {
        try { await api("DELETE", "/api/tickets/" + enc(t.id) + "/files/" + enc(f.id)); } catch (e) { /* already gone */ }
        pending = pending.filter((p) => p.id !== f.id); drawChips();
      } }, "×"))));

    async function addFiles(files) {
      err.textContent = "";
      for (const file of files) {
        if (pending.length + busy >= NOTE_FILES_MAX) { err.textContent = "At most " + NOTE_FILES_MAX + " files per note."; break; }
        if (file.size > NOTE_FILE_MAX) { err.textContent = file.name + " is larger than 10 MB."; continue; }
        busy++; addBtn.disabled = true;
        try { pending.push(await uploadTicketFile(t.id, file)); drawChips(); }
        catch (e) { err.textContent = file.name + ": " + e.message; }
        finally { busy--; addBtn.disabled = busy > 0; }
      }
    }

    // A pasted screenshot arrives as an unnamed "image.png": give it a readable name.
    text.addEventListener("paste", (e) => {
      const files = [...((e.clipboardData && e.clipboardData.files) || [])];
      if (!files.length) return;                             // plain text paste: leave it alone
      e.preventDefault();
      const stamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "");
      addFiles(files.map((f, i) => (f.name && f.name !== "image.png" ? f
        : new File([f], "screenshot-" + stamp + (i ? "-" + (i + 1) : "") + "." + ((f.type.split("/")[1] || "png").replace("jpeg", "jpg")), { type: f.type }))));
    });
    card.addEventListener("dragover", (e) => { if (e.dataTransfer && [...e.dataTransfer.types].includes("Files")) { e.preventDefault(); card.classList.add("dropping"); } });
    card.addEventListener("dragleave", () => card.classList.remove("dropping"));
    card.addEventListener("drop", (e) => { e.preventDefault(); card.classList.remove("dropping"); addFiles([...e.dataTransfer.files]); });
    picker.addEventListener("change", () => { addFiles([...picker.files]); picker.value = ""; });

    addBtn.addEventListener("click", async () => {
      err.textContent = "";
      if (!text.value.trim() && !pending.length) { err.textContent = "Write a note or attach a file."; return; }
      addBtn.disabled = true;
      try {
        await api("POST", "/api/tickets/" + enc(t.id) + "/notes", { body: text.value, file_ids: pending.map((f) => f.id) });
        route();
      } catch (e) { err.textContent = e.message; addBtn.disabled = false; }
    });
    card.append(text, picker, chips,
      h("div", { class: "form-actions" }, addBtn, " ", h("button", { type: "button", onclick: () => picker.click() }, "Attach files"),
        h("span", { class: "muted small" }, "  Images, PDF, text, Office files, zip · up to 10 MB each")), err);
  }

  card.append(...(notes.length ? notes.map((n) => h("div", { class: "note" },
    h("div", { class: "muted small" }, n.created_at.replace("T", " ").replace("Z", "") + " · " + (n.user_name || "unknown")),
    n.body ? h("p", { class: "note-body" }, n.body) : null,
    n.files.length ? h("div", null, n.files.map((f) => noteFile(t.id, f))) : null))
    : [h("p", { class: "muted" }, "No notes yet.")]));
  return card;
}
