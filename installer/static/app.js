(() => {
  const disksEl = document.getElementById("disks");
  const warnEl = document.getElementById("warn");
  const startBtn = document.getElementById("start");
  const diskPanel = document.getElementById("disk-panel");
  const preserveRow = document.getElementById("preserve-row");
  const preserveCheck = document.getElementById("preserve-user-data");
  const resetAdminRow = document.getElementById("reset-admin-row");
  const resetAdminCheck = document.getElementById("reset-admin");
  const resetAdminCopy = document.getElementById("reset-admin-copy");
  // The sentence as it is printed in the HTML. Kept as the fallback so a disk
  // with no roster to read shows real copy instead of an empty line.
  const resetAdminDefault = resetAdminCopy ? resetAdminCopy.textContent : "";
  const progressPanel = document.getElementById("progress-panel");
  const phaseTitle = document.getElementById("phase-title");
  const phaseMsg = document.getElementById("phase-msg");
  const phasePct = document.getElementById("phase-pct");
  const barFill = document.getElementById("bar-fill");
  const rebootBox = document.getElementById("reboot-box");
  const rebootBtn = document.getElementById("reboot");
  const waitBox = document.getElementById("wait-box");
  const errEl = document.getElementById("err");
  const barEl = document.querySelector(".bar");

  let selected = null;
  let selectedDisk = null;
  let hostPreflight = { ok: true, reasons: [] };
  let pollTimer = null;
  let waitTimer = null;

  function fmtSize(bytes) {
    const g = bytes / (1024 ** 3);
    return g >= 10 ? `${g.toFixed(0)} GB` : `${g.toFixed(1)} GB`;
  }

  function showHostPreflightWarn() {
    warnEl.classList.remove("hidden");
    const reasons = hostPreflight.reasons || [];
    const body = reasons.length
      ? reasons.map((r) => `<p>${r}</p>`).join("")
      : "<p>This computer does not meet Lemonseed's minimum requirements. Please check the system memory and processor, then try again.</p>";
    warnEl.innerHTML =
      "<strong>This computer isn't ready for Lemonseed yet</strong>" + body;
  }

  function updateWarn(disk) {
    if (!hostPreflight.ok) {
      showHostPreflightWarn();
      return;
    }
    if (!disk) {
      warnEl.classList.add("hidden");
      return;
    }
    if (disk.preservable_user_data) {
      warnEl.classList.remove("hidden");
      warnEl.textContent =
        "This disk has an existing Lemonseed install, which will be overwritten with a fresh install.";
    } else if (disk.wipe_warning || disk.existing_os) {
      warnEl.classList.remove("hidden");
      warnEl.textContent =
        "This disk already has data or an operating system. Installing Lemonseed will erase everything on it.";
    } else {
      warnEl.classList.add("hidden");
    }
  }

  function renderPreflight(payload) {
    hostPreflight = payload.preflight || { ok: true, reasons: [] };
    if (hostPreflight.ok) {
      return;
    }
    showHostPreflightWarn();
    startBtn.disabled = true;
  }

  function renderDisks(payload) {
    disksEl.innerHTML = "";
    renderPreflight(payload);
    const hostOk = hostPreflight.ok !== false;
    const eligible = (payload.disks || []).filter((d) => d.eligible);
    const blocked = (payload.disks || []).filter((d) => !d.eligible);

    if (!eligible.length) {
      disksEl.innerHTML =
        '<p class="lede">No suitable internal disk found. USB and eMMC devices cannot be used. Disks under 20 GB are too small.</p>';
      startBtn.disabled = true;
      return;
    }

    selected = (payload.default && payload.default.path) || eligible[0].path;
    selectedDisk = eligible.find((d) => d.path === selected) || eligible[0];

    for (const d of eligible) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "disk" + (d.path === selected ? " selected" : "");
      btn.innerHTML = `<div class="title">${d.model || d.name}</div>
        <div class="meta">${d.path} · ${fmtSize(d.size_bytes)}${d.serial ? " · " + d.serial : ""}</div>`;
      btn.addEventListener("click", () => {
        selected = d.path;
        selectedDisk = d;
        updateWarn(d);
        updatePreserve(d);
        [...disksEl.querySelectorAll(".disk")].forEach((el) =>
          el.classList.toggle("selected", el === btn)
        );
        startBtn.disabled = !hostOk;
      });
      disksEl.appendChild(btn);
      if (d.path === selected) {
        updateWarn(d);
        updatePreserve(d);
      }
    }

    if (blocked.length) {
      const note = document.createElement("p");
      note.className = "lede";
      note.style.marginTop = "0.75rem";
      note.textContent = `${blocked.length} other disk(s) hidden (USB, eMMC, under 20 GB, or installer medium).`;
      disksEl.appendChild(note);
    }

    startBtn.disabled = !selected || !hostOk;
  }

  function updatePreserve(disk) {
    const available = Boolean(disk && disk.preservable_user_data);
    preserveRow.classList.toggle("hidden", !available);
    preserveCheck.checked = available;
    // Reset-admin only makes sense alongside preserve (a fresh install
    // creates the first admin at /setup regardless). It is nested under
    // preserve and greyed out until preserve is checked.
    resetAdminRow.classList.toggle("hidden", !available);
    // Name the accounts a reset would remove admin access from (D-28). The
    // server computed the sentence because only it can read the roster; this is
    // per-disk, so switching the selected disk has to re-paint it.
    if (resetAdminCopy) {
      resetAdminCopy.textContent =
        (disk && disk.reset_admin_copy) || resetAdminDefault;
    }
    syncResetAdmin();
  }

  function syncResetAdmin() {
    const enabled = preserveCheck.checked;
    resetAdminRow.classList.toggle("off", !enabled);
    if (!enabled) resetAdminCheck.checked = false;
  }

  preserveCheck.addEventListener("change", syncResetAdmin);

  function showProgress() {
    diskPanel.classList.add("hidden");
    progressPanel.classList.remove("hidden");
  }

  function applyStatus(st) {
    phaseTitle.textContent =
      st.phase === "await_usb"
        ? "Almost done"
        : st.phase === "error"
          ? "Install failed"
          : "Installing";
    phaseMsg.textContent = st.message || "";
    const pct = Math.max(0, Math.min(100, st.progress || 0));
    barFill.style.width = pct + "%";
    phasePct.textContent = pct + "%";
    if (st.error) {
      errEl.classList.remove("hidden");
      errEl.textContent = st.error;
    } else {
      errEl.classList.add("hidden");
    }
    if (st.can_reboot) rebootBox.classList.remove("hidden");
    else rebootBox.classList.add("hidden");
  }

  function applianceOrigins() {
    const origins = new Set(["", window.location.origin, "http://lemonseed.local"]);
    if (window.location.protocol === "https:") {
      origins.add("https://lemonseed.local");
    }
    return [...origins];
  }

  async function probeAppliance(origin) {
    const base = origin || "";
    const res = await fetch(base + "/api/status", { cache: "no-store" });
    if (!res.ok) return null;
    const st = await res.json();
    if (st && st.role === "control-plane") return st;
    return null;
  }

  function startWaitingForAppliance() {
    if (waitTimer) return;
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    rebootBox.classList.add("hidden");
    waitBox.classList.remove("hidden");
    if (barEl) barEl.classList.add("hidden");
    phasePct.classList.add("hidden");
    errEl.classList.add("hidden");
    phaseTitle.textContent = "Waiting for the Lemon Seed appliance to boot";
    phaseMsg.textContent =
      "Leave this page open. Setup will appear automatically once the appliance is ready.";

    const tick = async () => {
      let sawInstaller = false;
      for (const origin of applianceOrigins()) {
        try {
          const st = await probeAppliance(origin);
          if (st) {
            let slug = (st.setup && st.setup.mdns_name) || "";
            if (!slug && typeof st.portal_url === "string") {
              const match = st.portal_url.match(/^https?:\/\/([a-z0-9-]+)\.local\/?$/i);
              if (match) slug = match[1];
            }
            if (slug && slug !== "lemonseed") {
              // Named appliances: leave lemonseed.local and open the real portal.
              window.location.replace("http://" + slug + ".local/");
            } else {
              const next = (origin || window.location.origin) + "/";
              window.location.replace(next);
            }
            return;
          }
        } catch (_) {
          /* down, still booting, or CORS/mDNS not ready */
        }
        if (!origin || origin === window.location.origin) {
          try {
            const res = await fetch("/api/status", { cache: "no-store" });
            if (res.ok) {
              const st = await res.json();
              if (st.role === "installer") sawInstaller = true;
            }
          } catch (_) {
            /* expected while the machine is restarting */
          }
        }
      }
      phaseMsg.textContent = sawInstaller
        ? "Still seeing the installer. Remove the USB, then wait — this page will keep checking."
        : "Waiting for the Lemon Seed appliance to boot. Setup will appear automatically.";
    };

    waitTimer = setInterval(tick, 2000);
    tick();
  }

  async function poll() {
    try {
      const st = await fetch("/api/status").then((r) => r.json());
      applyStatus(st);
      if (st.phase === "error" || st.can_reboot) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    } catch (_) {
      /* keep polling */
    }
  }

  startBtn.addEventListener("click", async () => {
    if (!selected) return;
    startBtn.disabled = true;
    showProgress();
    applyStatus({ phase: "starting", progress: 1, message: "Starting…" });
    const res = await fetch("/api/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        disk: selected,
        preserve_user_data: Boolean(
          selectedDisk && selectedDisk.preservable_user_data && preserveCheck.checked
        ),
        reset_admin_credentials: Boolean(
          selectedDisk &&
            selectedDisk.preservable_user_data &&
            preserveCheck.checked &&
            resetAdminCheck.checked
        ),
      }),
    });
    const body = await res.json();
    if (!res.ok) {
      applyStatus({
        phase: "error",
        progress: 0,
        message: "Could not start",
        error: body.error || res.statusText,
      });
      return;
    }
    pollTimer = setInterval(poll, 500);
    poll();
  });

  rebootBtn.addEventListener("click", async () => {
    rebootBtn.disabled = true;
    phaseMsg.textContent = "Rebooting…";
    try {
      await fetch("/api/reboot", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
    } catch (_) {
      /* host will drop mid-request once sysrq-b fires */
    }
    startWaitingForAppliance();
  });

  fetch("/api/disks")
    .then((r) => r.json())
    .then(renderDisks)
    .catch(() => {
      disksEl.innerHTML = '<p class="err">Could not list disks.</p>';
    });
})();
