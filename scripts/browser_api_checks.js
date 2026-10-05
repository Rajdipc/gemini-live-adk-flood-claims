/*
 * browser_api_checks.js - API test cases you run FROM THE SIGNED-IN BROWSER.
 * =============================================================================
 * WHY THE BROWSER?
 *   The app sits behind Identity-Aware Proxy (IAP). Your browser tab already
 *   carries the IAP sign-in cookie, so fetch() calls from the DevTools console
 *   of that tab are treated exactly like the app's own calls. From a terminal,
 *   curl would be stopped by IAP (which is the point of test L0-07).
 *
 * HOW TO RUN
 *   1. Open the Cloud Run URL and sign in as yourself (the app loads).
 *   2. Press F12 (or Cmd+Option+I), open the "Console" tab.
 *   3. Chrome may ask you to type "allow pasting" first. Do that.
 *   4. Paste this whole file and press Enter.
 *   5. A PASS/FAIL table is printed. The whole run takes ~15-30 s.
 *
 * WHAT IT COSTS / CREATES
 *   * One test intake (deleted again at the end, test A-13).
 *   * One small generated JPEG is checked by gemini-3.8-flash (a fraction of
 *     a cent).
 *   * One packet ZIP is archived to your bucket, and one row is added to
 *     BigQuery `intake_packets` (test A-11). That's how you verify the archive.
 *   Nothing else is changed. It never opens a voice call.
 *
 * The IDs (A-01...) match docs/post_deployment_tests.md, section L1-API.
 * =============================================================================
 */
(async () => {
  const results = [];
  const record = (id, name, ok, detail = "") => results.push({ id, test: name, result: ok ? "PASS" : "FAIL", detail });
  const json = async (res) => { try { return await res.json(); } catch { return {}; } };

  // A tiny real JPEG drawn on a canvas: a "wall" with a blue line and a label.
  // Real image bytes, so the server-side JPEG check and the Gemini photo check
  // both run for real.
  const makeJpeg = (label) => new Promise((resolve) => {
    const c = document.createElement("canvas"); c.width = 320; c.height = 240;
    const g = c.getContext("2d");
    g.fillStyle = "#d8cfc0"; g.fillRect(0, 0, 320, 240);
    g.fillStyle = "#4a6fa5"; g.fillRect(0, 150, 320, 6);
    g.fillStyle = "#222"; g.font = "16px sans-serif"; g.fillText(label, 10, 30);
    c.toBlob(resolve, "image/jpeg", 0.85);
  });
  const upload = (id, blob, name, description = "") => {
    const form = new FormData();
    form.append("photo", blob, name);
    form.append("description", description);
    return fetch(`/api/intakes/${id}/photos`, { method: "POST", body: form });
  };

  let intakeId = "";
  try {
    // A-01 health
    let r = await fetch("/api/health");
    record("A-01", "GET /api/health returns 200", r.status === 200, `HTTP ${r.status}`);

    // A-02 config + identity
    r = await fetch("/api/config"); const cfg = await json(r);
    record("A-02", "GET /api/config has brand + your IAP email", r.status === 200 && !!cfg.brand_name && String(cfg.user || "").includes("@"),
      `brand=${cfg.brand_name} user=${cfg.user}`);
    record("A-03", "Supported states are CO,TX,FL,LA,NC", JSON.stringify(cfg.supported_states) === JSON.stringify(["CO", "TX", "FL", "LA", "NC"]),
      JSON.stringify(cfg.supported_states));

    // A-04 create intake
    r = await fetch("/api/intakes", { method: "POST" }); const created = await json(r);
    intakeId = created.intake_id || "";
    record("A-04", "POST /api/intakes creates an intake", r.status === 200 && !!intakeId, `intake_id=${intakeId} live_model=${created.live_model}`);
    record("A-05", "Live model is gemini-3.8-live (unchanged)", created.live_model === "gemini-3.8-live", String(created.live_model));

    // A-06 read it back
    r = await fetch(`/api/intakes/${intakeId}`);
    record("A-06", "GET /api/intakes/{id} returns your intake", r.status === 200, `HTTP ${r.status}`);

    // A-07 unknown id -> 404 with a safe message (same answer as "not yours")
    r = await fetch("/api/intakes/does-not-exist-123"); const nf = await json(r);
    record("A-07", "Unknown intake -> 404, safe message", r.status === 404 && /not found/i.test(nf.detail || ""), `HTTP ${r.status} "${nf.detail}"`);

    // A-08 PNG rejected
    const png = new Blob([new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])], { type: "image/png" });
    r = await upload(intakeId, png, "not-a-jpeg.png");
    record("A-08", "Non-JPEG upload -> 415", r.status === 415, `HTTP ${r.status}`);

    // A-09 >5 MB rejected (JPEG magic bytes + 6 MB of zeros)
    const big = new Uint8Array(6_000_000); big.set([0xff, 0xd8, 0xff], 0);
    r = await upload(intakeId, new Blob([big], { type: "image/jpeg" }), "too-big.jpg"); const tb = await json(r);
    record("A-09", "Upload over 5 MB -> 413", r.status === 413, `HTTP ${r.status} "${tb.detail}"`);

    // A-10 a real JPEG is accepted, checked by Gemini, and streamed back through the app
    const jpeg = await makeJpeg("post-deploy test photo");
    r = await upload(intakeId, jpeg, "test.jpg", "A wall with a line on it"); const up = await json(r);
    const photo = up.photo || {};
    record("A-10", "Valid JPEG accepted and stored", r.status === 200 && !!photo.id, `caption="${String(photo.caption || "").slice(0, 60)}"`);
    // verified=false means the Gemini call failed (permissions / model location); the app degraded gracefully.
    record("A-10c", "gemini-3.8-flash photo check ran (verified=true)", photo.verified === true, `verified=${photo.verified}`);
    if (photo.id) {
      r = await fetch(`/api/intakes/${intakeId}/evidence/${photo.id}`);
      record("A-10b", "Photo bytes served by the app (no signed URL)", r.status === 200 && (r.headers.get("content-type") || "").includes("image/jpeg"), `HTTP ${r.status}`);
    }

    // A-11 packet JSON + ZIP (archives to GCS and writes a BigQuery row)
    r = await fetch(`/api/intakes/${intakeId}/packet`); const pk = await json(r);
    record("A-11", "Packet JSON has markdown + routing", r.status === 200 && !!pk.markdown && !!(pk.packet || {}).routing_decision,
      `routing=${(pk.packet || {}).routing_decision}`);
    const promise = /(you are|you're) covered|will be (fully )?paid|claim (is )?approved|guarantee/i;
    record("A-11b", "Packet never promises coverage", !promise.test(pk.markdown || ""), "no 'you are covered / will be paid / approved' wording");
    r = await fetch(`/api/intakes/${intakeId}/packet.zip`);
    const zipBytes = new Uint8Array(await r.arrayBuffer());
    record("A-12", "Packet ZIP downloads (starts with PK)", r.status === 200 && zipBytes[0] === 0x50 && zipBytes[1] === 0x4b, `HTTP ${r.status}, ${zipBytes.length} bytes`);

    // A-13 delete, then it is gone
    r = await fetch(`/api/intakes/${intakeId}`, { method: "DELETE" });
    const gone = await fetch(`/api/intakes/${intakeId}`);
    record("A-13", "DELETE removes the intake (then 404)", r.status === 200 && gone.status === 404, `delete=${r.status} then=${gone.status}`);
  } catch (err) {
    record("A-XX", "Unexpected error while testing", false, String(err));
  }

  console.table(results);
  const failed = results.filter((x) => x.result === "FAIL").length;
  console.log(failed ? `%c${failed} test(s) FAILED` : "%cAll API tests passed", `font-weight:bold;color:${failed ? "#c62828" : "#2e7d32"}`);
  console.log(`Test intake id (for log / BigQuery checks): ${intakeId}`);
})();
