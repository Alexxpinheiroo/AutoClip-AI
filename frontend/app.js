/* AutoClip-AI · frontend controller */
(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));

  const state = {
    mode: "file",
    file: null,
    busy: false,
    jobId: null,
    ws: null,
    pollTimer: null,
    elapsedTimer: null,
    currentJob: null,
  };

  const STYLE_LABELS = {
    hormozi_yellow: "Hormozi (amarelo)",
    mrbeast_green: "MrBeast (verde)",
    cyber_cyan: "Cyber (ciano)",
    minimal_white: "Minimalista (branco)",
  };

  const WHISPER_LABELS = {
    tiny: "tiny · mais rápido",
    base: "base · equilibrado",
    small: "small · preciso",
    medium: "medium · alta precisão",
    "large-v3": "large-v3 · máxima precisão",
  };

  const GENRE_LABELS = {
    auto: "Automático",
    viral: "Viral geral",
    humor: "Humor",
    educacional: "Educacional",
    entrevista: "Entrevista / Podcast",
    financas: "Finanças / Negócios",
    fitness: "Fitness / Saúde",
    games: "Games",
    noticias: "Notícias / Polêmica",
    esportes: "Esportes",
  };

  const ASPECT_LABELS = {
    "9:16": "9:16 · TikTok / Reels / Shorts",
    "1:1": "1:1 · Feed / LinkedIn",
    "16:9": "16:9 · YouTube / horizontal",
  };

  const SCORE_LABELS = {
    hook: "Gancho",
    retention: "Retenção",
    emotion: "Emoção",
    audio: "Áudio",
    visual: "Cenas",
    prompt: "Pedido",
    keywords: "Palavras",
    pacing: "Ritmo",
  };

  // ---------------------------------------------------------------- helpers
  async function fetchJSON(url, options = {}) {
    const response = await fetch(url, options);
    let data = null;
    try {
      data = await response.json();
    } catch (_) {
      data = null;
    }
    if (!response.ok) {
      const detail = data && (data.detail || data.error);
      throw new Error(detail || `Erro HTTP ${response.status}`);
    }
    return data;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function formatTime(totalSeconds) {
    const seconds = Math.max(0, Math.round(Number(totalSeconds) || 0));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    if (hours > 0) {
      return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
    }
    return `${minutes}:${String(secs).padStart(2, "0")}`;
  }

  function formatBytes(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(0)} KB`;
    if (value < 1024 * 1024 * 1024) return `${(value / (1024 * 1024)).toFixed(1)} MB`;
    return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  }

  let toastTimer = null;
  function toast(message, ok = false) {
    const el = $("#toast");
    el.textContent = message;
    el.classList.toggle("ok", ok);
    el.classList.remove("hidden");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 5200);
  }

  // ------------------------------------------------------------------ init
  async function init() {
    bindTabs();
    bindDropzone();
    bindSettings();
    bindActions();

    await Promise.all([loadHardware(), loadOptions()]);
    await loadHistory();
    await resumeRunningJob();
  }

  function resetStartLabel() {
    const button = $("#startBtn");
    button.querySelector(".btn-icon").textContent = "✦";
    button.querySelector(".btn-label").textContent = "Gerar cortes virais";
  }

  function bindTabs() {
    $$(".tab").forEach((tab) => {
      tab.addEventListener("click", () => setTab(tab.dataset.tab));
    });
  }

  function setTab(mode) {
    state.mode = mode;
    $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === mode));
    $("#pane-file").classList.toggle("hidden", mode !== "file");
    $("#pane-url").classList.toggle("hidden", mode !== "url");
  }

  function bindDropzone() {
    const dropzone = $("#dropzone");
    const input = $("#fileInput");

    input.addEventListener("change", () => {
      if (input.files && input.files[0]) setFile(input.files[0]);
    });

    ["dragenter", "dragover"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.add("dragging");
      });
    });

    ["dragleave", "drop"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.remove("dragging");
      });
    });

    dropzone.addEventListener("drop", (event) => {
      const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
      if (file) setFile(file);
    });
  }

  function setFile(file) {
    if (!file.type.startsWith("video/") && !/\.(mp4|mkv|mov|webm|avi|m4v)$/i.test(file.name)) {
      toast("Escolha um arquivo de vídeo válido.");
      return;
    }
    state.file = file;
    $("#fileName").textContent = file.name;
    $("#fileSize").textContent = formatBytes(file.size);
    $("#fileChip").classList.remove("hidden");
  }

  function bindSettings() {
    const minDur = $("#minDur");
    const maxDur = $("#maxDur");
    const wpg = $("#wordsPerGroup");
    const sil = $("#silenceThreshold");

    const syncLabels = () => {
      let min = Number(minDur.value);
      let max = Number(maxDur.value);
      if (max < min + 5) {
        max = Math.min(Number(maxDur.max), min + 5);
        maxDur.value = String(max);
      }
      $("#minLabel").textContent = `${min}s`;
      $("#maxLabel").textContent = `${max}s`;
    };

    minDur.addEventListener("input", syncLabels);
    maxDur.addEventListener("input", syncLabels);
    syncLabels();

    if (wpg) {
      wpg.addEventListener("input", () => {
        $("#wpgLabel").textContent = wpg.value;
      });
    }
    if (sil) {
      sil.addEventListener("input", () => {
        $("#silLabel").textContent = `${parseFloat(sil.value).toFixed(2)}s`;
      });
    }
  }

  function bindActions() {
    $("#startBtn").addEventListener("click", startProcessing);
    $("#cancelBtn").addEventListener("click", cancelJob);
    $("#refreshHistory").addEventListener("click", loadHistory);
    $("#openFolderBtn").addEventListener("click", openFolder);

    $("#urlInput").addEventListener("keydown", (event) => {
      if (event.key === "Enter") startProcessing();
    });
  }

  // ------------------------------------------------------------------- data
  async function loadHardware() {
    try {
      const hw = await fetchJSON("/api/hardware");
      $("#gpuName").textContent = hw.cuda ? `${hw.gpu_name} · CUDA` : "CPU";
      $("#footerHw").textContent = hw.nvenc ? "NVENC disponível" : "encoder por CPU";
      $("#useNvenc").checked = Boolean(hw.nvenc);
      const badge = $("#gpuBadge");
      badge.title = `${hw.device} · ${hw.gpu_name} · ffmpeg: ${hw.ffmpeg || "não encontrado"}`;
    } catch (_) {
      $("#gpuName").textContent = "Hardware indisponível";
    }
  }

  async function loadOptions() {
    try {
      const data = await fetchJSON("/api/options");
      const whisperSelect = $("#whisperModel");
      whisperSelect.innerHTML = (data.whisper_models || [])
        .map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(WHISPER_LABELS[model] || model)}</option>`)
        .join("");
      whisperSelect.value = data.defaults?.whisper_model || "base";

      const styleSelect = $("#subtitleStyle");
      styleSelect.innerHTML = (data.subtitle_styles || [])
        .map((style) => `<option value="${escapeHtml(style)}">${escapeHtml(STYLE_LABELS[style] || style)}</option>`)
        .join("");
      styleSelect.value = data.defaults?.subtitle_style || "hormozi_yellow";

      const genreSelect = $("#genre");
      genreSelect.innerHTML = (data.genres || [])
        .map((genre) => `<option value="${escapeHtml(genre.id)}">${escapeHtml(genre.label || GENRE_LABELS[genre.id] || genre.id)}</option>`)
        .join("");
      genreSelect.value = data.defaults?.genre || "auto";

      const aspectSelect = $("#aspectRatio");
      aspectSelect.innerHTML = (data.aspect_ratios || [])
        .map((aspect) => `<option value="${escapeHtml(aspect.id)}">${escapeHtml(ASPECT_LABELS[aspect.id] || aspect.label || aspect.id)}</option>`)
        .join("");
      aspectSelect.value = data.defaults?.aspect_ratio || "9:16";

      renderPromptChips(data.prompt_suggestions || []);

      if (data.defaults) {
        $("#numClips").value = data.defaults.num_clips ?? 3;
        $("#minDur").value = data.defaults.min_duration ?? 20;
        $("#maxDur").value = data.defaults.max_duration ?? 60;
        $("#minLabel").textContent = `${$("#minDur").value}s`;
        $("#maxLabel").textContent = `${$("#maxDur").value}s`;
        $("#ctaText").value = data.defaults.cta_text || "Siga para mais cortes!";
        if (typeof data.defaults.use_multimodal === "boolean") $("#useMultimodal").checked = data.defaults.use_multimodal;
        if (typeof data.defaults.remove_silences === "boolean") $("#removeSilences").checked = data.defaults.remove_silences;
        if (typeof data.defaults.show_hook === "boolean") $("#showHook").checked = data.defaults.show_hook;
        if (typeof data.defaults.show_cta === "boolean") $("#showCta").checked = data.defaults.show_cta;
        if (typeof data.defaults.use_emojis === "boolean") $("#useEmojis").checked = data.defaults.use_emojis;
      }
    } catch (error) {
      toast(`Não foi possível carregar as opções: ${error.message}`);
    }
  }

  function renderPromptChips(suggestions) {
    const container = $("#promptChips");
    if (!container) return;
    const chips = (suggestions || []).slice(0, 6);
    container.innerHTML = chips
      .map((text) => `<button type="button" class="chip">${escapeHtml(text)}</button>`)
      .join("");
    Array.from(container.querySelectorAll(".chip")).forEach((chip) => {
      chip.addEventListener("click", () => {
        const input = $("#promptInput");
        input.value = chip.textContent || "";
        container.querySelectorAll(".chip").forEach((item) => item.classList.toggle("active", item === chip));
        input.focus();
      });
    });
    $("#promptInput").addEventListener("input", () => {
      container.querySelectorAll(".chip").forEach((item) => {
        item.classList.toggle("active", item.textContent === $("#promptInput").value.trim());
      });
    });
  }

  function collectOptions() {
    return {
      num_clips: Number($("#numClips").value) || 3,
      min_duration: Number($("#minDur").value) || 20,
      max_duration: Number($("#maxDur").value) || 60,
      whisper_model: $("#whisperModel").value || "base",
      language: $("#language").value || null,
      subtitle_style: $("#subtitleStyle").value || "hormozi_yellow",
      prompt: $("#promptInput").value.trim(),
      genre: $("#genre").value || "auto",
      aspect_ratio: $("#aspectRatio").value || "9:16",
      cta_text: $("#ctaText").value.trim() || "Siga para mais cortes!",
      words_per_group: Number($("#wordsPerGroup")?.value) || 2,
      silence_threshold: parseFloat($("#silenceThreshold")?.value) || 0.55,
      use_face_tracking: $("#faceTracking").checked,
      use_nvenc: $("#useNvenc").checked,
      use_ollama: $("#useOllama").checked,
      use_multimodal: $("#useMultimodal").checked,
      remove_silences: $("#removeSilences").checked,
      show_hook: $("#showHook").checked,
      show_cta: $("#showCta").checked,
      use_emojis: $("#useEmojis").checked,
    };
  }

  // -------------------------------------------------------------- processing
  async function startProcessing() {
    if (state.busy) return;

    const options = collectOptions();
    setBusy(true);
    $("#resultsSection").classList.add("hidden");
    showProgress(0, "Preparando…");

    try {
      let jobId = null;

      if (state.mode === "url") {
        const url = $("#urlInput").value.trim();
        if (!/^https?:\/\//i.test(url)) {
          throw new Error("Cole uma URL válida começando com http:// ou https://");
        }
        showProgress(1, "Criando job e iniciando download…");
        const data = await fetchJSON("/api/process/url", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url, options }),
        });
        jobId = data.job_id;
      } else {
        if (!state.file) {
          throw new Error("Escolha um vídeo para enviar.");
        }
        showProgress(0, "Enviando vídeo para o servidor…");
        const meta = await uploadFile(state.file);
        showProgress(2, "Vídeo recebido. Iniciando processamento…");
        const data = await fetchJSON("/api/process/video", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ video_id: meta.video_id, options }),
        });
        jobId = data.job_id;
      }

      if (!jobId) throw new Error("O servidor não retornou um job válido.");
      watchJob(jobId, true);
      loadHistory();
    } catch (error) {
      toast(error.message);
      setBusy(false);
      $("#progressPanel").classList.add("hidden");
    }
  }

  function uploadFile(file) {
    return new Promise((resolve, reject) => {
      const form = new FormData();
      form.append("file", file, file.name);

      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload");
      xhr.responseType = "json";

      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) {
          const percent = (event.loaded / event.total) * 100;
          showProgress(percent * 0.06, `Enviando vídeo… ${percent.toFixed(0)}% (${formatBytes(event.loaded)} de ${formatBytes(event.total)})`);
        }
      };

      xhr.onload = () => {
        const data = xhr.response;
        if (xhr.status >= 200 && xhr.status < 300 && data) {
          resolve(data);
        } else {
          const detail = data && (data.detail || data.error);
          reject(new Error(detail || `Falha no upload (HTTP ${xhr.status}).`));
        }
      };

      xhr.onerror = () => reject(new Error("Falha de rede durante o upload."));
      xhr.send(form);
    });
  }

  function setBusy(busy) {
    state.busy = busy;
    const button = $("#startBtn");
    button.disabled = busy;
    if (busy) {
      button.querySelector(".btn-label").textContent = "Processando…";
    } else {
      resetStartLabel();
    }
  }

  function showProgress(percent, message) {
    $("#progressPanel").classList.remove("hidden");
    const safePercent = Math.max(0, Math.min(100, Number(percent) || 0));
    $("#progressBar").style.width = `${safePercent}%`;
    $("#progressPercent").textContent = `${safePercent.toFixed(0)}%`;
    $("#progressMessage").textContent = message || "Processando…";
  }

  function watchJob(jobId, newJob = false) {
    stopWatching();
    state.jobId = jobId;
    showProgress(0, "Conectando ao processamento…");

    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    try {
      const ws = new WebSocket(`${protocol}//${location.host}/ws/jobs/${jobId}`);
      state.ws = ws;
      ws.onmessage = (event) => {
        try {
          const job = JSON.parse(event.data);
          if (job && !job.error) applyJob(job);
        } catch (_) { /* ignore malformed frames */ }
      };
      ws.onerror = () => startPolling(jobId);
      ws.onclose = () => {
        if (state.jobId === jobId && !isTerminal(state.currentJob)) startPolling(jobId);
      };
    } catch (_) {
      startPolling(jobId);
    }

    if (newJob) {
      // Fallback polling in parallel; harmless if the socket already works.
      startPolling(jobId, 2000);
    }
  }

  function startPolling(jobId, interval = 1500) {
    if (state.pollTimer) return;
    state.pollTimer = setInterval(async () => {
      try {
        const job = await fetchJSON(`/api/jobs/${jobId}`);
        applyJob(job);
      } catch (_) { /* keep trying */ }
    }, interval);
  }

  function stopWatching() {
    if (state.ws) {
      try { state.ws.close(); } catch (_) { /* ignore */ }
      state.ws = null;
    }
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    if (state.elapsedTimer) {
      clearInterval(state.elapsedTimer);
      state.elapsedTimer = null;
    }
  }

  function isTerminal(job) {
    return Boolean(job && ["completed", "error", "cancelled"].includes(job.status));
  }

  function applyJob(job) {
    state.currentJob = job;
    const statusLabels = {
      queued: "Na fila",
      running: "Processando",
      completed: "Concluído",
      error: "Erro",
      cancelled: "Cancelado",
    };

    $("#progressPanel").classList.remove("hidden");
    $("#progressStatus").textContent = statusLabels[job.status] || job.status;
    showProgress(job.progress, job.message);
    $("#cancelBtn").classList.toggle("hidden", isTerminal(job));
    $("#startBtn").disabled = !isTerminal(job);
    if (isTerminal(job)) {
      resetStartLabel();
    } else {
      $("#startBtn").querySelector(".btn-label").textContent = "Processando…";
    }
    state.busy = !isTerminal(job);

    startElapsedClock(job);

    if (job.status === "completed") {
      stopWatching();
      if (job.clips && job.clips.length) {
        renderClips(job);
      } else {
        $("#resultsSection").classList.add("hidden");
        toast(job.message || "Nenhum resultado foi gerado.", true);
      }
      loadHistory();
    } else if (job.status === "error") {
      stopWatching();
      toast(job.error || job.message || "O processamento falhou.");
      loadHistory();
    } else if (job.status === "cancelled") {
      stopWatching();
      toast("Processamento cancelado.");
      loadHistory();
    }
  }

  function startElapsedClock(job) {
    if (state.elapsedTimer) {
      clearInterval(state.elapsedTimer);
      state.elapsedTimer = null;
    }

    const update = () => {
      const elapsed = isTerminal(job)
        ? Number(job.elapsed_seconds || 0)
        : (Date.now() / 1000) - Number(job.created_at || Date.now() / 1000);
      $("#progressElapsed").textContent = formatTime(elapsed);
    };

    update();
    if (!isTerminal(job)) {
      state.elapsedTimer = setInterval(update, 1000);
    }
  }

  async function cancelJob() {
    if (!state.jobId) return;
    try {
      await fetchJSON(`/api/jobs/${state.jobId}/cancel`, { method: "POST" });
      $("#cancelBtn").disabled = true;
    } catch (error) {
      toast(error.message);
    }
  }

  async function openFolder() {
    const job = state.currentJob;
    if (!job) return;
    try {
      await fetchJSON(`/api/jobs/${job.id}/open-folder`, { method: "POST" });
    } catch (error) {
      toast(error.message);
    }
  }

  // ----------------------------------------------------------------- results
  function aspectClass(ratio) {
    if (ratio === "1:1") return "aspect-1-1";
    if (ratio === "16:9") return "aspect-16-9";
    return "aspect-9-16";
  }

  function clipCopyText(clip) {
    const parts = [clip.title || ""];
    if (clip.description) parts.push(clip.description);
    if (Array.isArray(clip.hashtags) && clip.hashtags.length) parts.push(clip.hashtags.join(" "));
    return parts.filter(Boolean).join("\n\n");
  }

  async function copyClip(clip) {
    const text = clipCopyText(clip);
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      toast("Legenda copiada para a área de transferência.", true);
    } catch (_) {
      const area = document.createElement("textarea");
      area.value = text;
      document.body.appendChild(area);
      area.select();
      try {
        document.execCommand("copy");
        toast("Legenda copiada para a área de transferência.", true);
      } catch (error) {
        toast("Não foi possível copiar a legenda.");
      }
      area.remove();
    }
  }

  function renderClips(job) {
    const grid = $("#clipsGrid");
    const clips = job.clips || [];
    $("#clipCount").textContent = String(clips.length);
    $("#resultsSection").classList.remove("hidden");
    $("#openFolderBtn").dataset.jobId = job.id;

    if (!clips.length) {
      grid.innerHTML = `<p class="empty">Nenhum corte gerado neste vídeo.</p>`;
      return;
    }

    grid.innerHTML = clips.map((clip, index) => {
      const poster = clip.thumbnail_url ? ` poster="${escapeHtml(clip.thumbnail_url)}"` : "";
      const score = clip.virality_score ? `🔥 ${escapeHtml(clip.virality_score)}` : "Corte";
      const encoder = clip.encoder === "h264_nvenc" ? "GPU NVENC" : "CPU";
      const reason = clip.reason ? `<p class="clip-reason">${escapeHtml(clip.reason)}</p>` : "";
      const description = clip.description
        ? `<p class="clip-desc">${escapeHtml(clip.description)}</p>`
        : "";
      const hashtags = Array.isArray(clip.hashtags) && clip.hashtags.length
        ? `<p class="clip-hashtags">${escapeHtml(clip.hashtags.join(" "))}</p>`
        : "";
      const breakdown = clip.score_breakdown || {};
      const scoreTags = Object.keys(SCORE_LABELS)
        .filter((key) => breakdown[key] !== undefined && breakdown[key] !== null)
        .map((key) => `<span title="${escapeHtml(SCORE_LABELS[key])}">${escapeHtml(SCORE_LABELS[key])} ${escapeHtml(breakdown[key])}</span>`)
        .join("");
      const flags = [
        clip.aspect_ratio || "9:16",
        clip.auto_edited ? "auto-edit" : "corte direto",
        clip.source === "ollama" ? "IA local" : null,
        encoder,
      ].filter(Boolean).join(" · ");
      return `
        <article class="clip-card">
          <div class="clip-video ${aspectClass(clip.aspect_ratio)}">
            <video src="${escapeHtml(clip.url)}"${poster} controls playsinline preload="metadata"></video>
            <span class="score">${score}</span>
            <span class="dur">${formatTime(clip.duration)}</span>
          </div>
          <div class="clip-body">
            <h3>${escapeHtml(clip.title || "Corte")}</h3>
            <p class="clip-meta">${escapeHtml(flags)} · ${formatTime(clip.start_time)} → ${formatTime(clip.end_time)}</p>
            ${description}
            ${hashtags}
            ${scoreTags ? `<div class="score-tags">${scoreTags}</div>` : ""}
            ${reason}
            <div class="clip-actions">
              <a class="btn small" href="${escapeHtml(clip.url)}" download>⬇ Baixar MP4</a>
              <a class="btn small ghost" href="${escapeHtml(clip.url)}" target="_blank" rel="noopener">▶ Abrir</a>
              <button type="button" class="btn small ghost copy-btn" data-clip-index="${index}">📋 Legenda</button>
            </div>
          </div>
        </article>`;
    }).join("");

    grid.querySelectorAll(".copy-btn").forEach((button) => {
      button.addEventListener("click", () => copyClip(clips[Number(button.dataset.clipIndex)]));
    });
  }

  async function loadHistory() {
    try {
      const data = await fetchJSON("/api/jobs");
      const jobs = data.jobs || [];
      const list = $("#historyList");

      if (!jobs.length) {
        list.innerHTML = `<p class="empty">Nenhum processamento ainda.</p>`;
        return;
      }

      list.innerHTML = jobs.map((job) => {
        const title = (job.video && job.video.title) || job.source_url || "Processamento";
        const date = new Date((job.created_at || 0) * 1000).toLocaleDateString("pt-BR", {
          day: "2-digit", month: "2-digit",
        });
        const clips = job.clip_count ? `${job.clip_count} corte(s)` : "sem cortes";
        return `
          <button class="history-item" data-job-id="${escapeHtml(job.id)}">
            <span class="history-title">${escapeHtml(title)}</span>
            <span class="history-meta">
              <span>${date} · ${escapeHtml(clips)}</span>
              <span class="status-pill status-${escapeHtml(job.status)}">${escapeHtml(job.status)}</span>
            </span>
          </button>`;
      }).join("");

      $$(".history-item").forEach((item) => {
        item.addEventListener("click", () => openJob(item.dataset.jobId));
      });
    } catch (_) {
      $("#historyList").innerHTML = `<p class="empty">Não foi possível carregar o histórico.</p>`;
    }
  }

  async function openJob(jobId) {
    try {
      const job = await fetchJSON(`/api/jobs/${jobId}`);
      applyJob(job);
      if (!isTerminal(job)) {
        watchJob(jobId);
      } else {
        stopWatching();
      }
      $("#resultsSection").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      toast(error.message);
    }
  }

  async function resumeRunningJob() {
    try {
      const data = await fetchJSON("/api/jobs");
      const running = (data.jobs || []).find((job) => ["running", "queued"].includes(job.status));
      if (running) {
        watchJob(running.id);
        toast("Retomando o processamento em andamento…", true);
      }
    } catch (_) { /* ignore */ }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
