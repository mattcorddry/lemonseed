(() => {
  const disksEl = document.getElementById("disks");
  const warnEl = document.getElementById("warn");
  const startBtn = document.getElementById("start");
  const diskPanel = document.getElementById("disk-panel");
  const preserveRow = document.getElementById("preserve-row");
  const preserveRadio = document.getElementById("preserve-radio");
  const eraseRow = document.getElementById("erase-row");
  const eraseRadio = document.getElementById("erase-radio");
  const resetAdminRow = document.getElementById("reset-admin-row");
  const resetAdminCheck = document.getElementById("reset-admin");
  const resetAdminCopy = document.getElementById("reset-admin-copy");
  const encryptRow = document.getElementById("encrypt-row");
  const encryptCheck = document.getElementById("encrypt-user-data");
  const encryptWarning = document.getElementById("encrypt-warning");
  const channelSelect = document.getElementById("install-channel");
  const devDialog = document.getElementById("dev-dialog");
  const devUrlInput = document.getElementById("dev-url");
  const devError = document.getElementById("dev-error");
  const devCancelBtn = document.getElementById("dev-cancel");
  const devOkBtn = document.getElementById("dev-ok");
  const hwDialog = document.getElementById("hw-dialog");
  const hwValue = document.getElementById("hw-value");
  const hwInput = document.getElementById("hw-input");
  const hwCopyBtn = document.getElementById("hw-copy");
  const hwRegenBtn = document.getElementById("hw-regen");
  const hwContinueBtn = document.getElementById("hw-continue");
  const hwError = document.getElementById("hw-error");
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
  const errActions = document.getElementById("err-actions");
  const errDetail = document.getElementById("err-detail");
  const errDetailBody = document.getElementById("err-detail-body");
  const retryBtn = document.getElementById("retry");
  const errRebootBtn = document.getElementById("err-reboot");
  const barEl = document.querySelector(".bar");

  let selected = null;
  let selectedDisk = null;
  let hostPreflight = { ok: true, reasons: [] };
  let pollTimer = null;
  let waitTimer = null;
  let encryptionAvailable = false;
  let hwPassword = "";
  let hwMode = null; // "generate" | "unlock", while the dialog is open
  let devUrl = ""; // the Development channel's source, once confirmed
  let channelBeforeDev = "stable";

  function fmtSize(bytes) {
    const g = bytes / (1024 ** 3);
    return g >= 10 ? `${g.toFixed(0)} GB` : `${g.toFixed(1)} GB`;
  }

  // The D-E1 offer rides /api/disks: ticked when a usable TPM is present
  // (the user may still untick it), greyed out with the server's exact
  // warning copy otherwise. The server owns the copy because only it has
  // run the TPM trial.
  function applyEncryption(payload) {
    const enc = payload.encryption || {};
    encryptionAvailable = Boolean(enc.available);
    encryptCheck.disabled = !encryptionAvailable;
    encryptCheck.checked = Boolean(enc.checked);
    if (!encryptionAvailable && enc.warning) {
      encryptWarning.textContent = enc.warning;
      encryptWarning.classList.remove("hidden");
    } else {
      encryptWarning.classList.add("hidden");
      encryptWarning.textContent = "";
    }
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
    applyEncryption(payload);
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

  // The first choice is preserve-vs-erase, so the radio availability rides
  // the selected disk: preserve is the default when the disk allows it and
  // greyed out otherwise (erase is then the only path).
  function updatePreserve(disk) {
    const available = Boolean(disk && disk.preservable_user_data);
    preserveRadio.disabled = !available;
    // Default preserve only when the volume was verified (unlocked, identity
    // read). A locked volume cannot be examined without the Hardware password,
    // so erase is the default and preserve remains an opt-in that the
    // unlock dialog validates before anything is written.
    const verified = available && !disk.preservable_user_data.locked;
    preserveRadio.checked = verified;
    eraseRadio.checked = !verified;
    // Name the accounts a reset would remove admin access from (D-28). The
    // server computed the sentence because only it can read the roster; this
    // is per-disk, so switching the selected disk has to re-paint it.
    if (resetAdminCopy) {
      resetAdminCopy.textContent =
        (disk && disk.reset_admin_copy) || resetAdminDefault;
    }
    syncResetAdmin();
    syncEncrypt();
  }

  function syncResetAdmin() {
    const enabled = preserveRadio.checked;
    resetAdminRow.classList.toggle("off", !enabled);
    if (!enabled) resetAdminCheck.checked = false;
  }

  preserveRadio.addEventListener("change", syncResetAdmin);
  eraseRadio.addEventListener("change", () => {
    syncResetAdmin();
    syncEncrypt();
  });

  // Encryption is a sub-choice of erase-and-format only: a preserve keeps
  // whatever volume the box already has (encrypted stays encrypted, plain
  // stays plain — there is no in-place conversion). Greyed when erase is not
  // the selected mode; greyed AND unchecked when no usable TPM was found.
  function syncEncrypt() {
    const enabled = eraseRadio.checked && encryptionAvailable;
    encryptRow.classList.toggle("off", !enabled);
    if (!encryptionAvailable) encryptCheck.checked = false;
  }
  eraseRadio.addEventListener("change", syncEncrypt);

  // ---- Release channel (settings-page grammar) --------------------------
  // The default source is GitHub discovery per channel. Development is an
  // explicit, opt-in URL — the popup fires the moment the channel is chosen,
  // so no private host name ever rides a published ISO by accident.
  function devUrlProblem(value) {
    try {
      const parsed = new URL(value);
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
        return "Use an http:// or https:// URL.";
      }
      if (!parsed.hostname) {
        return "That URL has no host name. Example: http://host:port/release-20261031.01/";
      }
      return "";
    } catch (_) {
      return "That does not look like a URL. Example: http://host:port/release-20261031.01/";
    }
  }

  function openDevDialog() {
    devUrlInput.value = devUrl;
    devError.classList.add("hidden");
    devDialog.classList.remove("hidden");
    devUrlInput.focus();
  }

  function closeDevDialog() {
    devDialog.classList.add("hidden");
  }

  channelSelect.addEventListener("change", () => {
    if (channelSelect.value === "dev") {
      openDevDialog();
    } else {
      channelBeforeDev = channelSelect.value;
    }
  });

  devCancelBtn.addEventListener("click", () => {
    channelSelect.value = channelBeforeDev;
    closeDevDialog();
  });

  devOkBtn.addEventListener("click", () => {
    const problem = devUrlProblem(devUrlInput.value.trim());
    if (problem) {
      devError.textContent = problem;
      devError.classList.remove("hidden");
      return;
    }
    devUrl = devUrlInput.value.trim();
    channelBeforeDev = "dev";
    closeDevDialog();
  });

  // ---- Hardware password dialog (D27 presentation, §4.2) ----------------
  // Plain HTTP means navigator.clipboard does not exist here: select-and-
  // execCommand is the working path, not the edge case.
  async function copySecret(value) {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(value);
        return true;
      } catch (_) {
        /* fall through to the selection copy */
      }
    }
    const field = hwMode === "generate" ? hwValue : hwInput;
    field.focus();
    const range = document.createRange();
    range.selectNodeContents(field);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    return document.execCommand("copy");
  }

  async function drawPassword() {
    hwValue.textContent = "…";
    try {
      const res = await fetch("/api/password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      hwPassword = (await res.json()).hardware_password || "";
    } catch (_) {
      hwPassword = "";
    }
    hwValue.textContent = hwPassword || "Could not generate a password. Try again.";
  }

  function openGenerateDialog() {
    hwMode = "generate";
    hwValue.classList.remove("hidden");
    hwInput.classList.add("hidden");
    hwCopyBtn.classList.remove("hidden");
    hwRegenBtn.classList.remove("hidden");
    hwError.classList.add("hidden");
    hwDialog.classList.remove("hidden");
    drawPassword();
  }

  function openUnlockDialog() {
    hwMode = "unlock";
    hwValue.classList.add("hidden");
    hwInput.classList.remove("hidden");
    hwInput.value = "";
    hwCopyBtn.classList.add("hidden");
    hwRegenBtn.classList.add("hidden");
    hwError.classList.add("hidden");
    hwDialog.classList.remove("hidden");
    hwInput.focus();
  }

  function closeDialog() {
    hwMode = null;
    hwDialog.classList.add("hidden");
  }

  hwCopyBtn.addEventListener("click", async () => {
    const value = hwMode === "generate" ? hwPassword : hwInput.value;
    await copySecret(value);
    hwCopyBtn.textContent = "Copied";
    setTimeout(() => {
      hwCopyBtn.textContent = "Copy";
    }, 1500);
  });

  hwRegenBtn.addEventListener("click", drawPassword);

  hwContinueBtn.addEventListener("click", async () => {
    if (hwMode === "unlock") {
      const password = hwInput.value;
      if (!password) return;
      hwContinueBtn.disabled = true;
      let ok = false;
      let message = "";
      try {
        const res = await fetch("/api/luks-verify", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ disk: selected, password }),
        });
        const body = await res.json();
        ok = Boolean(body.ok);
        message = body.error || res.statusText;
      } catch (_) {
        message = "Could not verify the password.";
      }
      hwContinueBtn.disabled = false;
      if (!ok) {
        hwError.textContent = message;
        hwError.classList.remove("hidden");
        return;
      }
      closeDialog();
      beginInstall(password);
      return;
    }
    if (!hwPassword) {
      await drawPassword();
      return;
    }
    closeDialog();
    beginInstall(hwPassword);
  });

  function showProgress() {
    diskPanel.classList.add("hidden");
    progressPanel.classList.remove("hidden");
  }

  function applyStatus(st) {
    phaseTitle.textContent =
      st.phase === "await_usb" ? "Almost done" : "Installing";
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
    // Error actions ride the product sentence: the technical reason (DNS,
    // HTTP status, which channel was searched) stays behind the disclosure.
    const failed = st.phase === "error";
    errActions.classList.toggle("hidden", !failed);
    if (failed && st.detail) {
      errDetail.classList.remove("hidden");
      errDetailBody.textContent = st.detail;
    } else {
      errDetail.classList.add("hidden");
      errDetailBody.textContent = "";
    }
    if (st.can_reboot && st.phase === "await_usb") rebootBox.classList.remove("hidden");
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
    if (channelSelect.value === "dev" && !devUrl) {
      openDevDialog();
      return;
    }
    const preserving = Boolean(
      selectedDisk && selectedDisk.preservable_user_data && preserveRadio.checked
    );
    // §4.3: a preserved LUKS2 volume whose TPM token does not unlock asks for
    // the Hardware password and validates it before the install starts. The
    // state was decided at discovery time (disks.py ran the token trial).
    const lockedLuks = Boolean(
      preserving &&
        selectedDisk.preservable_user_data.luks &&
        selectedDisk.preservable_user_data.locked
    );
    if (!preserving) {
      // The Hardware password is always generated and presented (§5): on an
      // unencrypted install it still guards GRUB editing and the root console.
      openGenerateDialog();
    } else if (lockedLuks) {
      openUnlockDialog();
    } else {
      beginInstall("");
    }
  });

  async function beginInstall(hardwarePassword) {
    const preserving = Boolean(
      selectedDisk && selectedDisk.preservable_user_data && preserveRadio.checked
    );
    const channel = channelSelect.value || "stable";
    startBtn.disabled = true;
    showProgress();
    applyStatus({ phase: "starting", progress: 1, message: "Starting…" });
    const res = await fetch("/api/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        disk: selected,
        channel,
        source_url: channel === "dev" ? devUrl : "",
        preserve_user_data: preserving,
        reset_admin_credentials: Boolean(
          selectedDisk &&
            selectedDisk.preservable_user_data &&
            preserveRadio.checked &&
            resetAdminCheck.checked
        ),
        encrypt_user_data: Boolean(eraseRadio.checked && encryptionAvailable && encryptCheck.checked),
        hardware_password: hardwarePassword,
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
  }

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

  // A failed install that changed nothing (release files not found) goes back
  // to the disk form: the selections are still there, and Install re-runs the
  // preflight. Rebooting is equally safe — the box boots the installer again.
  retryBtn.addEventListener("click", async () => {
    progressPanel.classList.add("hidden");
    diskPanel.classList.remove("hidden");
    try {
      renderDisks(await fetch("/api/disks", { cache: "no-store" }).then((r) => r.json()));
    } catch (_) {
      /* keep the selections the form already has */
    }
  });

  errRebootBtn.addEventListener("click", async () => {
    errRebootBtn.disabled = true;
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
  });

  fetch("/api/disks")
    .then((r) => r.json())
    .then(renderDisks)
    .catch(() => {
      disksEl.innerHTML = '<p class="err">Could not list disks.</p>';
    });
  refreshLatest();
})();
