(() => {
  const meta = document.getElementById("meta");
  const search = document.getElementById("search");
  const empty = document.getElementById("empty");
  const guideEl = document.getElementById("guide");
  const timeRuler = document.getElementById("time-ruler");
  const channelRail = document.getElementById("channel-rail");
  const gridScroll = document.getElementById("grid-scroll");
  const programGrid = document.getElementById("program-grid");
  const nowLine = document.getElementById("now-line");
  const nowBar = document.getElementById("now");
  const nowName = document.getElementById("now-name");
  const stopBtn = document.getElementById("stop");
  const infoCh = document.getElementById("info-ch");
  const infoTitle = document.getElementById("info-title");
  const infoTime = document.getElementById("info-time");
  const infoDesc = document.getElementById("info-desc");
  const tabs = [...document.querySelectorAll(".tab")];
  const taglineEl = document.querySelector(".tagline");

  let category = "local";
  let query = "";
  let playingUrl = null;
  let debounceTimer = null;
  let guideData = null;
  let focusRow = 0;
  let focusProg = 0;
  let syncingScroll = false;
  let timeTrack = null;
  let layoutKey = "";
  let resizeTimer = null;

  function pxPerMin() {
    return (
      parseFloat(
        getComputedStyle(document.documentElement).getPropertyValue("--px-per-min")
      ) || 2.4
    );
  }

  function rowHeight() {
    return (
      parseFloat(
        getComputedStyle(document.documentElement).getPropertyValue("--row-h")
      ) || 52
    );
  }

  function currentLayoutKey() {
    return `${pxPerMin()}|${rowHeight()}`;
  }

  function windowWidthPx(data) {
    return ((data.window_end - data.window_start) / 60) * pxPerMin();
  }

  function xForTime(ts, data) {
    return ((ts - data.window_start) / 60) * pxPerMin();
  }

  function formatClock(ts) {
    return new Date(ts * 1000).toLocaleTimeString([], {
      hour: "numeric",
      minute: "2-digit",
    });
  }

  function formatRange(start, stop) {
    return `${formatClock(start)} – ${formatClock(stop)}`;
  }

  function setPlaying(name, url) {
    playingUrl = url || null;
    if (url) {
      nowName.textContent = name || "Stream";
      nowBar.classList.remove("hidden");
    } else {
      nowBar.classList.add("hidden");
      nowName.textContent = "—";
    }
    channelRail.querySelectorAll(".channel-row").forEach((el) => {
      el.classList.toggle("playing", el.dataset.url === playingUrl);
    });
    programGrid.querySelectorAll(".prog-cell").forEach((el) => {
      el.classList.toggle("playing", el.dataset.url === playingUrl);
    });
  }

  function placeholder(name) {
    const d = document.createElement("div");
    d.className = "ch-logo placeholder";
    d.textContent = (name || "?").trim().charAt(0).toUpperCase();
    return d;
  }

  function logoEl(ch) {
    if (ch.logo) {
      const img = document.createElement("img");
      img.className = "ch-logo";
      img.src = ch.logo;
      img.alt = "";
      img.loading = "lazy";
      img.referrerPolicy = "no-referrer";
      img.onerror = () => {
        img.replaceWith(placeholder(ch.name));
      };
      return img;
    }
    return placeholder(ch.name);
  }

  function updateInfo(ch, prog) {
    if (!ch || !prog) {
      infoCh.textContent = "—";
      infoTitle.textContent = "Select a program";
      infoTime.textContent = "";
      infoDesc.textContent = "";
      return;
    }
    infoCh.textContent = `${ch.number ?? ""} ${ch.name}`.trim();
    infoTitle.textContent = prog.title || "Live";
    const start = prog.orig_start ?? prog.start;
    const stop = prog.orig_stop ?? prog.stop;
    infoTime.textContent = formatRange(start, stop);
    infoDesc.textContent =
      prog.desc ||
      (prog.live
        ? "No programme guide for this channel — stream still plays live"
        : "");
  }

  function nearestProgramIndex(ch, nowTs) {
    const progs = ch.programs || [];
    if (!progs.length) return 0;
    for (let i = 0; i < progs.length; i++) {
      if (progs[i].start <= nowTs && nowTs < progs[i].stop) return i;
    }
    return 0;
  }

  function ensureCellVisible(cell) {
    const cellLeft = cell.offsetLeft;
    const cellRight = cellLeft + cell.offsetWidth;
    const viewLeft = gridScroll.scrollLeft;
    const viewRight = viewLeft + gridScroll.clientWidth;
    if (cellLeft < viewLeft) {
      gridScroll.scrollLeft = Math.max(0, cellLeft - 24);
    } else if (cellRight > viewRight) {
      gridScroll.scrollLeft = cellRight - gridScroll.clientWidth + 24;
    }

    const rh = rowHeight();
    const rowTop = focusRow * rh;
    const viewTop = gridScroll.scrollTop;
    const viewBottom = viewTop + gridScroll.clientHeight;
    if (rowTop < viewTop) {
      gridScroll.scrollTop = rowTop;
      channelRail.scrollTop = rowTop;
    } else if (rowTop + rh > viewBottom) {
      const next = rowTop + rh - gridScroll.clientHeight;
      gridScroll.scrollTop = next;
      channelRail.scrollTop = next;
    }
  }

  function applyFocus() {
    if (!guideData || !guideData.channels.length) {
      updateInfo(null, null);
      return;
    }
    focusRow = Math.max(0, Math.min(focusRow, guideData.channels.length - 1));
    const ch = guideData.channels[focusRow];
    const progs = ch.programs || [];
    focusProg = Math.max(0, Math.min(focusProg, Math.max(progs.length - 1, 0)));

    channelRail.querySelectorAll(".channel-row.selected").forEach((el) => {
      el.classList.remove("selected");
    });
    programGrid.querySelectorAll(".prog-cell.selected").forEach((el) => {
      el.classList.remove("selected");
    });

    const rowBtn = channelRail.querySelector(`.channel-row[data-row="${focusRow}"]`);
    if (rowBtn) rowBtn.classList.add("selected");

    const cell = programGrid.querySelector(
      `.prog-cell[data-row="${focusRow}"][data-prog="${focusProg}"]`
    );
    if (cell) {
      cell.classList.add("selected");
      ensureCellVisible(cell);
    }
    updateInfo(ch, progs[focusProg] || null);
  }

  function syncTimeTrack() {
    if (timeTrack) {
      timeTrack.style.transform = `translateX(${-gridScroll.scrollLeft}px)`;
    }
  }

  function renderTimeRuler(data) {
    timeRuler.innerHTML = "";
    timeTrack = document.createElement("div");
    timeTrack.className = "time-track";
    timeTrack.style.position = "relative";
    timeTrack.style.height = "100%";
    timeTrack.style.width = `${windowWidthPx(data)}px`;
    timeTrack.style.willChange = "transform";

    for (let t = data.window_start; t < data.window_end; t += 1800) {
      const tick = document.createElement("div");
      tick.className = "time-tick";
      tick.style.left = `${xForTime(t, data)}px`;
      tick.style.width = `${30 * pxPerMin()}px`;
      tick.textContent = formatClock(t);
      timeTrack.appendChild(tick);
    }
    timeRuler.appendChild(timeTrack);
    syncTimeTrack();
  }

  function renderChannels(data) {
    channelRail.innerHTML = "";
    const frag = document.createDocumentFragment();
    data.channels.forEach((ch, i) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "channel-row" + (ch.url === playingUrl ? " playing" : "");
      btn.dataset.row = String(i);
      btn.dataset.url = ch.url;

      const num = document.createElement("span");
      num.className = "ch-num";
      num.textContent = ch.number ?? "";

      const name = document.createElement("span");
      name.className = "ch-name";
      name.textContent = ch.name;

      btn.append(num, logoEl(ch), name);
      btn.addEventListener("click", () => {
        focusRow = i;
        focusProg = nearestProgramIndex(ch, Date.now() / 1000);
        applyFocus();
        play(ch);
      });
      frag.appendChild(btn);
    });
    channelRail.appendChild(frag);
  }

  function renderPrograms(data) {
    programGrid.innerHTML = "";
    const width = windowWidthPx(data);
    const rh = rowHeight();
    programGrid.style.width = `${width}px`;
    programGrid.style.height = `${data.channels.length * rh}px`;

    const frag = document.createDocumentFragment();
    data.channels.forEach((ch, row) => {
      const rowEl = document.createElement("div");
      rowEl.className = "prog-row";
      rowEl.style.width = `${width}px`;

      (ch.programs || []).forEach((prog, pi) => {
        const left = xForTime(prog.start, data);
        const w = Math.max(((prog.stop - prog.start) / 60) * pxPerMin() - 2, 8);
        const cell = document.createElement("button");
        cell.type = "button";
        cell.className =
          "prog-cell" +
          (prog.live ? " live" : "") +
          (ch.url === playingUrl ? " playing" : "");
        cell.style.left = `${left}px`;
        cell.style.width = `${w}px`;
        cell.dataset.row = String(row);
        cell.dataset.prog = String(pi);
        cell.dataset.url = ch.url;
        cell.textContent = prog.title || "No listings";
        cell.title = prog.title || "No listings";
        cell.addEventListener("click", () => {
          focusRow = row;
          focusProg = pi;
          applyFocus();
          play(ch);
        });
        rowEl.appendChild(cell);
      });
      frag.appendChild(rowEl);
    });
    programGrid.appendChild(frag);
  }

  function updateNowLine(data) {
    const now = Date.now() / 1000;
    if (now < data.window_start || now > data.window_end) {
      nowLine.classList.add("hidden");
      return;
    }
    nowLine.classList.remove("hidden");
    nowLine.style.left = `${xForTime(now, data)}px`;
    nowLine.style.height = `${data.channels.length * rowHeight()}px`;
  }

  function scrollToNow(data) {
    const now = Date.now() / 1000;
    if (now < data.window_start || now > data.window_end) return;
    const x = xForTime(now, data);
    gridScroll.scrollLeft = Math.max(0, x - gridScroll.clientWidth * 0.25);
    syncTimeTrack();
  }

  function render(data, opts = {}) {
    const keepScroll = Boolean(opts.keepScroll);
    const prevLeft = keepScroll ? gridScroll.scrollLeft : 0;
    const prevTop = keepScroll ? gridScroll.scrollTop : 0;

    guideData = data;
    layoutKey = currentLayoutKey();
    if (!data.channels.length) {
      guideEl.classList.add("hidden");
      empty.classList.remove("hidden");
      updateInfo(null, null);
    } else {
      guideEl.classList.remove("hidden");
      empty.classList.add("hidden");
      renderTimeRuler(data);
      renderChannels(data);
      renderPrograms(data);
      updateNowLine(data);
      if (keepScroll) {
        gridScroll.scrollLeft = prevLeft;
        gridScroll.scrollTop = prevTop;
        channelRail.scrollTop = prevTop;
        syncTimeTrack();
      } else {
        scrollToNow(data);
      }
      focusRow = Math.min(focusRow, data.channels.length - 1);
      focusProg = nearestProgramIndex(
        data.channels[focusRow],
        Date.now() / 1000
      );
      applyFocus();
    }

    const when = data.epg_updated_at
      ? new Date(data.epg_updated_at * 1000).toLocaleString()
      : "—";
    meta.textContent = `${data.count} channel${data.count === 1 ? "" : "s"} · guide updated ${when}`;
  }

  function rerenderForLayout() {
    if (!guideData) return;
    const next = currentLayoutKey();
    if (next === layoutKey) return;
    render(guideData, { keepScroll: true });
  }

  async function load() {
    const params = new URLSearchParams();
    if (category && category !== "all") params.set("category", category);
    if (query) params.set("q", query);
    params.set("hours", "4");
    meta.textContent = "Loading guide…";
    try {
      const res = await fetch(`/api/guide?${params}`);
      const data = await res.json();
      render(data);
    } catch (err) {
      meta.textContent = "Failed to load guide";
      console.error(err);
    }
  }

  async function play(ch) {
    try {
      const res = await fetch("/api/play", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: ch.url, name: ch.name }),
      });
      const data = await res.json();
      if (!data.ok) {
        setPlaying(null, null);
        const msg = (data.error || "Could not start playback").split("\n")[0];
        alert(msg.length > 180 ? msg.slice(0, 177) + "…" : msg);
        return;
      }
      setPlaying(data.name || ch.name, data.url || ch.url);
    } catch (err) {
      setPlaying(null, null);
      alert("Play request failed");
      console.error(err);
    }
  }

  async function stop() {
    try {
      await fetch("/api/stop", { method: "POST" });
    } catch (err) {
      console.error(err);
    }
    setPlaying(null, null);
  }

  async function refreshStatus() {
    try {
      const res = await fetch("/api/status");
      const data = await res.json();
      if (data.tagline && taglineEl) taglineEl.textContent = data.tagline;
      if (data.running && data.url) {
        setPlaying(data.name, data.url);
      } else {
        setPlaying(null, null);
      }
    } catch (_) {
      /* ignore */
    }
  }

  function moveFocus(dRow, dProg) {
    if (!guideData || !guideData.channels.length) return;
    if (dRow !== 0) {
      focusRow = Math.max(
        0,
        Math.min(guideData.channels.length - 1, focusRow + dRow)
      );
      focusProg = nearestProgramIndex(
        guideData.channels[focusRow],
        Date.now() / 1000
      );
    }
    if (dProg !== 0) {
      const progs = guideData.channels[focusRow].programs || [];
      focusProg = Math.max(0, Math.min(progs.length - 1, focusProg + dProg));
    }
    applyFocus();
  }

  guideEl.addEventListener("keydown", (e) => {
    if (!guideData || !guideData.channels.length) return;
    switch (e.key) {
      case "ArrowUp":
        e.preventDefault();
        moveFocus(-1, 0);
        break;
      case "ArrowDown":
        e.preventDefault();
        moveFocus(1, 0);
        break;
      case "ArrowLeft":
        e.preventDefault();
        moveFocus(0, -1);
        break;
      case "ArrowRight":
        e.preventDefault();
        moveFocus(0, 1);
        break;
      case "Enter":
        e.preventDefault();
        play(guideData.channels[focusRow]);
        break;
      default:
        break;
    }
  });

  gridScroll.addEventListener("scroll", () => {
    if (syncingScroll) return;
    syncingScroll = true;
    channelRail.scrollTop = gridScroll.scrollTop;
    syncTimeTrack();
    syncingScroll = false;
  });

  channelRail.addEventListener("scroll", () => {
    if (syncingScroll) return;
    syncingScroll = true;
    gridScroll.scrollTop = channelRail.scrollTop;
    syncingScroll = false;
  });

  tabs.forEach((tab) => {
    tab.addEventListener("click", () => {
      category = tab.dataset.cat;
      tabs.forEach((t) => {
        const on = t === tab;
        t.classList.toggle("active", on);
        t.setAttribute("aria-selected", on ? "true" : "false");
      });
      focusRow = 0;
      focusProg = 0;
      load();
    });
  });

  search.addEventListener("input", () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      query = search.value.trim();
      focusRow = 0;
      focusProg = 0;
      load();
    }, 200);
  });

  stopBtn.addEventListener("click", stop);

  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(rerenderForLayout, 150);
  });
  window.addEventListener("orientationchange", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(rerenderForLayout, 250);
  });

  load();
  refreshStatus();
  setInterval(refreshStatus, 8000);
  setInterval(() => {
    if (guideData) updateNowLine(guideData);
  }, 30000);
  setInterval(load, 5 * 60 * 1000);
})();
