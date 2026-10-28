/* AutoClip-AI — frontend controller */
(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));

  const state = {
    workflowMode: "clips", // "clips" or "full_video"
    mode: "file",          // "file" or "url"
    file: null,
    busy: false,
    jobId: null,
    ws: null,
    pollTimer: null,
    elapsedTimer: null,
    currentJob: null,
    enabledStickers: ["subscribe", "like", "comment", "share"],
  };

  const STYLE_LABELS = {
    youtube_clean: "YouTube Clean (Elegante & Confortável)",
    hormozi_yellow: "Hormozi (amarelo)",
    mrbeast_green: "MrBeast (verde)",
    cyber_cyan: "Cyber (ciano)",
    minimal_white: "Minimalista (branco)",
    bold_cinema: "Bold Cinema (Documentário)",
  };

  const WHISPER_LABELS = {
    tiny: "tiny • ultrarrápido (baixa precisão)",
    base: "base • rápido (precisão limitada)",
    small: "small • equilibrado",
    medium: "medium • muito preciso",
    "large-v3-turbo": "large-v3-turbo • rápido + preciso (bom para vídeos longos)",
    "large-v3": "large-v3 • máxima precisão (recomendado)",
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
    original: "Original • Manter resolução do vídeo",
    "16:9": "16:9 • YouTube / TV (horizontal)",
    "9:16": "9:16 • TikTok / Reels / Shorts (vertical)",
    "1:1": "1:1 • Feed / Instagram (quadrado)",
  };

  const SCORE_LABELS = {
    hook: "Gancho",
    retention: "Retenção",
    emotion: "Emoção",
    audio: "Áudio",
    visual: "Cenas",
    prompt: "Pedido",
    keywords: "Palavras",
  };

  // ----------------------------------------------------------------- helpers
  function escapeHtml(text) {
    return String(text ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function formatBytes(bytes) {
    if (!bytes || bytes <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(1024));
    return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`;
  }

  function formatTime(totalSeconds) {
    const s = Math.max(0, Math.floor(Number(totalSeconds) || 0));
    const mins = Math.floor(s / 60);
    const secs = s % 60;
    return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }

  function toast(message, isSuccess = false) {
    const el = $("#toast");
    if (!el) return;
    el.textContent = message;
    el.className = `toast ${isSuccess ? "toast-success" : ""}`;
    el.classList.remove("hidden");
    clearTimeout(el._timer);
    el._timer = setTimeout(() => el.classList.add("hidden"), 4500);
  }

  async function fetchJSON(url, options = {}) {
    const response = await fetch(url, options);
    if (!response.ok) {
      let detail = `Erro na requisição (HTTP ${response.status})`;
      try {
        const body = await response.json();
        detail = body.detail || body.error || detail;
      } catch (_) { /* ignore */ }
      throw new Error(detail);
    }
    return response.json();
  }

  // ------------------------------------------------------------------ init
  async function init() {
    bindWorkflowModes();
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
    if (state.workflowMode === "full_video") {
      button.querySelector(".btn-icon").textContent = "🎬";
      $("#startBtnLabel").textContent = "Processar Vídeo Completo (Legendas + Adesivos)";
    } else {
      button.querySelector(".btn-icon").textContent = "⚡";
      $("#startBtnLabel").textContent = "Gerar cortes virais";
    }
  }

  function bindWorkflowModes() {
    const clipsBtn = $("#modeClipsBtn");
    const fullBtn = $("#modeFullBtn");
    if (!clipsBtn || !fullBtn) return;

    clipsBtn.addEventListener("click", () => setWorkflowMode("clips"));
    fullBtn.addEventListener("click", () => setWorkflowMode("full_video"));
  }

  function setWorkflowMode(mode) {
    state.workflowMode = mode;
    $("#modeClipsBtn")?.classList.toggle("active", mode === "clips");
    $("#modeFullBtn")?.classList.toggle("active", mode === "full_video");

    $("#clipsModeWrapper")?.classList.toggle("hidden", mode !== "clips");
    $("#fullVideoModeWrapper")?.classList.toggle("hidden", mode !== "full_video");

    resetStartLabel();
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
    // Clips mode settings
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

    if (minDur && maxDur) {
      minDur.addEventListener("input", syncLabels);
      maxDur.addEventListener("input", syncLabels);
      syncLabels();
    }

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

    // Full video settings
    const fullWpg = $("#fullWordsPerGroup");
    if (fullWpg) {
      fullWpg.addEventListener("input", () => {
        $("#fullWpgLabel").textContent = fullWpg.value;
      });
    }

    const fullStickerDur = $("#fullStickerDur");
    if (fullStickerDur) {
      fullStickerDur.addEventListener("input", () => {
        $("#fullStickerDurLabel").textContent = `${parseFloat(fullStickerDur.value).toFixed(1)}s`;
      });
    }

    const fullInterval = $("#fullStickerInterval");
    if (fullInterval) {
      fullInterval.addEventListener("change", () => {
        $("#fullCustomIntervalField")?.classList.toggle("hidden", fullInterval.value !== "custom");
      });
    }

    const chapterMinDur = $("#chapterMinDur");
    if (chapterMinDur) {
      chapterMinDur.addEventListener("input", () => {
        $("#chapterMinDurLabel").textContent = `${chapterMinDur.value}s`;
      });
    }

    const fullExportChapters = $("#fullExportChapters");
    if (fullExportChapters) {
      fullExportChapters.addEventListener("change", () => {
        $("#chapterMinDurField")?.classList.toggle("hidden", !fullExportChapters.checked);
      });
    }
  }

  function bindActions() {
    $("#startBtn").addEventListener("click", startProcessing);
    $("#cancelBtn").addEventListener("click", cancelJob);
    $("#refreshHistory").addEventListener("click", loadHistory);

  $("#clearHistory").addEventListener("click", async () => {
    if (!confirm("Apagar TODO o histórico e os arquivos gerados?")) return;
    try {
      await fetchJSON("/api/jobs", { method: "DELETE" });
      state.jobId = null;
      $("#resultsSection")?.classList.add("hidden");
      toast("Histórico limpo.");
      loadHistory();
    } catch (error) {
      toast(error.message);
    }
  });
    $("#openFolderBtn").addEventListener("click", openFolder);

    $("#urlInput").addEventListener("keydown", (event) => {
      if (event.key === "Enter") startProcessing();
    });
  }

  // ----------------------------------------------------------- metadata/opts
  async function loadHardware() {
    try {
      const data = await fetchJSON("/api/hardware");
      const parts = [];
      if (data.gpu_name) parts.push(data.gpu_name);
      if (data.nvenc) parts.push("NVENC ativo");
      const label = parts.join(" • ") || "CPU";
      $("#gpuName").textContent = label;
      $("#footerHw").textContent = label;
    } catch (_) {
      $("#gpuName").textContent = "Hardware local";
    }
  }

  async function loadOptions() {
    try {
      const data = await fetchJSON("/api/options");

      // 1. Clips options
      const whisperSelect = $("#whisperModel");
      whisperSelect.innerHTML = (data.whisper_models || [])
        .map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(WHISPER_LABELS[model] || model)}</option>`)
        .join("");
      whisperSelect.value = data.defaults?.whisper_model || "base";

      const styleSelect = $("#subtitleStyle");
      styleSelect.innerHTML = (data.subtitle_styles || [])
        .map((style) => `<option value="${escapeHtml(style)}">${escapeHtml(STYLE_LABELS[style] || data.subtitle_style_labels?.[style] || style)}</option>`)
        .join("");
      styleSelect.value = data.defaults?.subtitle_style || "hormozi_yellow";

      const genreSelect = $("#genre");
      genreSelect.innerHTML = (data.genres || [])
        .map((genre) => `<option value="${escapeHtml(genre.id)}">${escapeHtml(genre.label || GENRE_LABELS[genre.id] || genre.id)}</option>`)
        .join("");
      genreSelect.value = data.defaults?.genre || "auto";

      const aspectSelect = $("#aspectRatio");
      aspectSelect.innerHTML = (data.aspect_ratios || [])
        .filter((a) => a.id !== "original")
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

      // 2. Full Video Options
      const fullStyleSelect = $("#fullSubtitleStyle");
      if (fullStyleSelect) {
        fullStyleSelect.innerHTML = (data.subtitle_styles || [])
          .map((style) => `<option value="${escapeHtml(style)}">${escapeHtml(data.subtitle_style_labels?.[style] || STYLE_LABELS[style] || style)}</option>`)
          .join("");
        fullStyleSelect.value = data.defaults?.full_subtitle_style || "youtube_clean";
      }

      const fullWhisperSelect = $("#fullWhisperModel");
      if (fullWhisperSelect) {
        fullWhisperSelect.innerHTML = (data.whisper_models || [])
          .map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(WHISPER_LABELS[model] || model)}</option>`)
          .join("");
        fullWhisperSelect.value = data.defaults?.whisper_model || "large-v3";
      }

      const fullIntervalSelect = $("#fullStickerInterval");
      if (fullIntervalSelect && data.stickers?.intervals) {
        fullIntervalSelect.innerHTML = data.stickers.intervals
          .map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label)}</option>`)
          .join("");
        fullIntervalSelect.value = "auto";
      }

      const fullPosSelect = $("#fullStickerPos");
      if (fullPosSelect && data.stickers?.positions) {
        fullPosSelect.innerHTML = data.stickers.positions
          .map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label)}</option>`)
          .join("");
        fullPosSelect.value = "bottom_right";
      }

      // 3. Render Sticker Cards Grid
      renderStickerCards(data.stickers?.available || []);

    } catch (error) {
      toast(`Não foi possível carregar as opções: ${error.message}`);
    }
  }

  function renderStickerCards(stickers) {
    const grid = $("#stickerCardsGrid");
    if (!grid) return;

    grid.innerHTML = stickers.map((stk) => {
      const active = state.enabledStickers.includes(stk.id);
      return `
        <div class="sticker-card ${active ? "active" : ""}" data-sticker-id="${escapeHtml(stk.id)}">
          <img class="sticker-card-thumb" src="${escapeHtml(stk.preview_url)}" alt="${escapeHtml(stk.label)}" loading="lazy">
          <div class="sticker-card-info">
            <strong>${escapeHtml(stk.label)}</strong>
            <small>${escapeHtml(stk.subtitle || stk.title)}</small>
          </div>
          <div class="sticker-card-check">✓</div>
        </div>
      `;
    }).join("");

    grid.querySelectorAll(".sticker-card").forEach((card) => {
      card.addEventListener("click", () => {
        const id = card.dataset.stickerId;
        if (state.enabledStickers.includes(id)) {
          state.enabledStickers = state.enabledStickers.filter((s) => s !== id);
          card.classList.remove("active");
        } else {
          state.enabledStickers.push(id);
          card.classList.add("active");
        }
      });
    });
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
    if (state.workflowMode === "full_video") {
      return {
        mode: "full_video",
        subtitle_style: $("#fullSubtitleStyle")?.value || "youtube_clean",
        subtitle_position: $("#fullSubtitlePos")?.value || "bottom",
        words_per_group: Number($("#fullWordsPerGroup")?.value) || 3,
        whisper_model: $("#fullWhisperModel")?.value || "large-v3",
        language: $("#fullLanguage")?.value || null,
        output_language: $("#fullOutputLanguage")?.value || "auto",
        aspect_ratio: $("#fullAspectRatio")?.value || "original",
        sticker_interval_mode: $("#fullStickerInterval")?.value || "auto",
        sticker_custom_interval: parseFloat($("#fullCustomInterval")?.value || 90),
        sticker_position: $("#fullStickerPos")?.value || "bottom_right",
        sticker_duration: parseFloat($("#fullStickerDur")?.value || 4.5),
        burn_subtitles: $("#fullBurnSubtitles")?.checked ?? true,
        export_srt: $("#fullExportSrt")?.checked ?? true,
        export_chapters: $("#fullExportChapters")?.checked ?? true,
        chapter_min_duration: parseFloat($("#chapterMinDur")?.value || 60),
        use_stickers: $("#fullUseStickers")?.checked ?? true,
        use_emojis: $("#fullUseEmojis")?.checked ?? false,
        use_nvenc: $("#fullUseNvenc")?.checked ?? true,
        enabled_stickers: state.enabledStickers,
      };
    }

    return {
      mode: "clips",
      num_clips: Number($("#numClips").value) || 3,
      min_duration: Number($("#minDur").value) || 20,
      max_duration: Number($("#maxDur").value) || 60,
      whisper_model: $("#whisperModel").value || "large-v3",
      language: $("#language").value || null,
      output_language: $("#outputLanguage")?.value || "auto",
      subtitle_style: $("#subtitleStyle").value || "hormozi_yellow",
      prompt: $("#promptInput").value.trim(),
      genre: $("#genre").value || "auto",
      aspect_ratio: $("#aspectRatio").value || "9:16",
      facecam_position: $("#facecamPosition")?.value || "off",
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
    showProgress(0, "Preparando processamento…");

    try {
      let jobId = null;

      if (state.mode === "url") {
        const url = $("#urlInput").value.trim();
        if (!/^https?:\/\//i.test(url)) {
          throw new Error("Cole uma URL válida começando com http:// ou https://");
        }
        showProgress(1, "Criando job e iniciando download do vídeo…");
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
      button.querySelector(".btn-label").textContent = "Processando no servidor…";
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
        } catch (_) { /* ignore */ }
      };
      ws.onerror = () => startPolling(jobId);
      ws.onclose = () => {
        if (state.jobId === jobId && !isTerminal(state.currentJob)) startPolling(jobId);
      };
    } catch (_) {
      startPolling(jobId);
    }

    if (newJob) {
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
      $("#startBtn").querySelector(".btn-label").textContent = "Processando no servidor…";
    }
    state.busy = !isTerminal(job);

    startElapsedClock(job);

    if (job.status === "completed") {
      stopWatching();
      if (job.mode === "full_video" && job.output_file) {
        renderFullVideoResult(job);
      } else if (job.clips && job.clips.length) {
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

  function renderClips(job) {
    const section = $("#resultsSection");
    const grid = $("#clipsGrid");
    const fullShowcase = $("#fullVideoShowcase");

    section.classList.remove("hidden");
    grid.classList.remove("hidden");
    fullShowcase?.classList.add("hidden");

    $("#resultsHeading").innerHTML = `Cortes prontos <span class="count-pill" id="clipCount">${job.clips.length}</span>`;

    const clips = job.clips || [];
    grid.innerHTML = clips.map((clip, idx) => {
      const score = Math.round(Number(clip.score || 0));
      const dur = formatTime(clip.duration);
      const aspect = aspectClass(job.options?.aspect_ratio);
      const copyText = escapeHtml(clipCopyText(clip));
      const breakdown = clip.score_breakdown || {};
      const breakdownEntries = Object.entries(breakdown).filter(([, val]) => typeof val === "number");

      return `
        <article class="clip-card" data-clip-id="${clip.id}">
          <div class="clip-media ${aspect}">
            <video src="${escapeHtml(clip.url)}" poster="${escapeHtml(clip.thumbnail_url || "")}" controls playsinline preload="metadata"></video>
            <div class="score-pill" title="Score de viralidade">${score}</div>
            <div class="clip-badge">${dur}</div>
          </div>
          <div class="clip-body">
            <h3 class="clip-title" title="${escapeHtml(clip.title || "")}">${escapeHtml(clip.title || `Corte #${clip.id}`)}</h3>
            ${clip.hook ? `<p class="clip-hook"><b>Gancho:</b> "${escapeHtml(clip.hook)}"</p>` : ""}
            ${breakdownEntries.length ? `
              <div class="score-breakdown">
                ${breakdownEntries.slice(0, 4).map(([key, val]) => `
                  <span class="subscore-pill" title="${escapeHtml(SCORE_LABELS[key] || key)}">
                    ${escapeHtml(SCORE_LABELS[key] || key)}: <b>${Math.round(val)}</b>
                  </span>`).join("")}
              </div>` : ""}
            ${Array.isArray(clip.hashtags) && clip.hashtags.length ? `
              <div class="clip-tags">
                ${clip.hashtags.slice(0, 4).map((tag) => `<span class="hashtag">${escapeHtml(tag)}</span>`).join("")}
              </div>` : ""}
            <div class="clip-actions">
              <a class="btn small" href="${escapeHtml(clip.url)}" download="${escapeHtml(clip.filename || `clip_${clip.id}.mp4`)}">
                ⬇ Baixar MP4
              </a>
              <button class="btn ghost small copy-btn" data-clip-index="${idx}" data-copy="${copyText}" title="Copiar título, descrição e hashtags">
                📋 Copiar post
              </button>
            </div>
          </div>
        </article>`;
    }).join("");

    grid.querySelectorAll(".copy-btn").forEach((button) => {
      button.addEventListener("click", () => copyClip(clips[Number(button.dataset.clipIndex)]));
    });
  }

  function renderFullVideoResult(job) {
    const section = $("#resultsSection");
    const grid = $("#clipsGrid");
    const fullShowcase = $("#fullVideoShowcase");
    const player = $("#fullVideoPlayer");

    section.classList.remove("hidden");
    grid.classList.add("hidden");
    fullShowcase.classList.remove("hidden");

    $("#resultsHeading").innerHTML = `Vídeo Legendado & Enriquecido <span class="count-pill">Concluído</span>`;

    const out = job.output_file || {};
    const vid = job.video || {};

    player.src = out.url || "";
    player.load();

    $("#fullVideoTitle").textContent = vid.title || "Vídeo Completo Legendado";

    // Download buttons
    const dlMp4 = $("#fullDownloadBtn");
    dlMp4.href = out.url || "#";
    dlMp4.download = out.filename || "video_legendado.mp4";

    const dlSrt = $("#fullDownloadSrtBtn");
    if (job.srt_file && job.srt_file.url) {
      dlSrt.classList.remove("hidden");
      dlSrt.href = job.srt_file.url;
      dlSrt.download = job.srt_file.filename || "legendas.srt";
    } else {
      dlSrt.classList.add("hidden");
    }

    // Video meta tags
    const tagsContainer = $("#fullVideoTags");
    const durStr = formatTime(out.duration || vid.duration);
    const sizeStr = formatBytes(out.size_bytes);
    const resStr = (vid.width && vid.height) ? `${vid.width}x${vid.height}` : "";
    const encStr = out.encoder === "h264_nvenc" ? "GPU (NVENC)" : "CPU (libx264)";
    const stkCount = job.stickers_count || 0;

    tagsContainer.innerHTML = `
      <span class="video-tag highlight">⏱ ${durStr}</span>
      <span class="video-tag">📦 ${sizeStr}</span>
      ${resStr ? `<span class="video-tag">📐 ${resStr}</span>` : ""}
      <span class="video-tag">⚡ ${encStr}</span>
      <span class="video-tag highlight">✨ ${stkCount} adesivos na timeline</span>
    `;

    // Timeline stickers navigator
    const timelineContainer = $("#timelineBadges");
    const timeline = job.stickers_timeline || [];

    if (timeline.length) {
      $("#timelineNavBox").classList.remove("hidden");
      timelineContainer.innerHTML = timeline.map((ev) => {
        let icon = "🔔";
        if (ev.type === "like") icon = "👍";
        else if (ev.type === "comment") icon = "💬";
        else if (ev.type === "share") icon = "🚀";

        return `
          <button type="button" class="timeline-badge" data-time="${ev.start_time}" title="Pular vídeo para ${ev.start_formatted}">
            <span class="t-icon">${icon}</span>
            <span class="t-time">${ev.start_formatted}</span>
            <span class="t-label">${escapeHtml(ev.label)}</span>
          </button>
        `;
      }).join("");

      timelineContainer.querySelectorAll(".timeline-badge").forEach((badge) => {
        badge.addEventListener("click", () => {
          const t = Math.max(0, parseFloat(badge.dataset.time) - 0.5);
          player.currentTime = t;
          player.play();
        });
      });
    } else {
      $("#timelineNavBox").classList.add("hidden");
    }

    // Chapters presentation
    const dlChapters = $("#fullDownloadChaptersBtn");
    const copyChaptersBtn = $("#fullCopyChaptersBtn");
    const chaptersPanel = $("#chaptersPanel");
    const chaptersList = $("#chaptersList");

    const chapters = Array.isArray(job.chapters) ? job.chapters : [];
    if (job.chapters_file?.url && chapters.length > 0) {
      if (dlChapters) {
        dlChapters.classList.remove("hidden");
        dlChapters.href = job.chapters_file.url;
        dlChapters.download = job.chapters_file.filename || "capitulos.txt";
      }

      const formatYtTime = (sec) => {
        const s = Math.max(0, Math.round(Number(sec || 0)));
        const h = Math.floor(s / 3600);
        const m = Math.floor((s % 3600) / 60);
        const rSec = s % 60;
        return h > 0
          ? `${h}:${String(m).padStart(2, "0")}:${String(rSec).padStart(2, "0")}`
          : `${m}:${String(rSec).padStart(2, "0")}`;
      };

      const chaptersYtText = chapters.map((ch) => `${formatYtTime(ch.start)} ${ch.title || "Capítulo"}`).join("\n");

      if (copyChaptersBtn) {
        copyChaptersBtn.classList.remove("hidden");
        copyChaptersBtn.onclick = async () => {
          try {
            await navigator.clipboard.writeText(chaptersYtText);
            toast("Capítulos copiados! Cole na descrição do YouTube.", true);
          } catch (_) {
            const ta = document.createElement("textarea");
            ta.value = chaptersYtText;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand("copy");
            document.body.removeChild(ta);
            toast("Capítulos copiados!", true);
          }
        };
      }

      if (chaptersPanel && chaptersList) {
        chaptersPanel.classList.remove("hidden");
        chaptersList.innerHTML = chapters.map((ch) => {
          const ts = formatYtTime(ch.start);
          return `
            <div class="chapter-item" data-time="${ch.start}">
              <span class="chapter-time">▶ ${ts}</span>
              <span class="chapter-title">${escapeHtml(ch.title || "Capítulo")}</span>
              <span class="chapter-jump">Pular ➔</span>
            </div>
          `;
        }).join("");

        chaptersList.querySelectorAll(".chapter-item").forEach((item) => {
          item.addEventListener("click", () => {
            const t = Math.max(0, parseFloat(item.dataset.time) || 0);
            player.currentTime = t;
            player.play();
          });
        });
      }
    } else {
      dlChapters?.classList.add("hidden");
      copyChaptersBtn?.classList.add("hidden");
      chaptersPanel?.classList.add("hidden");
    }

    // YouTube Publishing Package (titles + description + hashtags)
    const metaPanel = $("#metaPanel");
    const meta = job.youtube_metadata;

    if (metaPanel && meta && Array.isArray(meta.titles) && meta.titles.length) {
      metaPanel.classList.remove("hidden");

      // 1. Title suggestions - click the copy button to copy each one
      const titlesList = $("#metaTitles");
      titlesList.innerHTML = meta.titles.map((t, i) => `
        <div class="meta-title-item">
          <span class="meta-title-rank">${i === 0 ? "⭐" : i + 1}</span>
          <span class="meta-title-text" title="${escapeHtml(t)}">${escapeHtml(t)}</span>
          <button type="button" class="btn ghost small meta-copy-btn" data-title="${escapeHtml(t)}">📋</button>
        </div>
      `).join("");
      titlesList.querySelectorAll(".meta-copy-btn").forEach((btn) => {
        btn.addEventListener("click", async () => {
          const text = btn.dataset.title || "";
          try {
            await navigator.clipboard.writeText(text);
            toast("Título copiado!", true);
          } catch (_) {
            const ta = document.createElement("textarea");
            ta.value = text;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand("copy");
            document.body.removeChild(ta);
            toast("Título copiado!", true);
          }
        });
      });

      // 2. Description - textarea + copy button
      const descTa = $("#metaDescription");
      if (descTa) descTa.value = meta.description || "";
      const copyDescBtn = $("#metaCopyDescBtn");
      if (copyDescBtn) {
        copyDescBtn.onclick = async () => {
          const text = descTa ? descTa.value : "";
          try {
            await navigator.clipboard.writeText(text);
            toast("Descrição copiada! Cole no YouTube.", true);
          } catch (_) {
            const ta = document.createElement("textarea");
            ta.value = text;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand("copy");
            document.body.removeChild(ta);
            toast("Descrição copiada!", true);
          }
        };
      }

      // 3. Download the full .txt package
      const dlMeta = $("#fullDownloadMetaBtn");
      if (job.metadata_file?.url && dlMeta) {
        dlMeta.classList.remove("hidden");
        dlMeta.href = job.metadata_file.url;
        dlMeta.download = job.metadata_file.filename || "youtube_metadata.txt";
      }

      // 4. Hashtags + tags chips
      const tagsRow = $("#metaTagsRow");
      if (tagsRow) {
        const chips = [
          ...(meta.hashtags || []),
          ...(meta.tags || []).map((t) => `#${t.replace(/\s+/g, "")}`),
        ];
        const uniq = [...new Set(chips)];
        tagsRow.innerHTML = uniq.length
          ? uniq.map((h) => `<span class="video-tag">${escapeHtml(h)}</span>`).join("")
          : "";
      }
    } else if (metaPanel) {
      metaPanel.classList.add("hidden");
    }
  }

  async function copyClip(clip) {
    if (!clip) return;
    const text = clipCopyText(clip);
    try {
      await navigator.clipboard.writeText(text);
      toast("Post copiado para a área de transferência!", true);
    } catch (_) {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      document.body.removeChild(textarea);
      toast("Post copiado!", true);
    }
  }

  // ----------------------------------------------------------------- history
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

        let summary = "";
        let modeIcon = "⚡";
        if (job.mode === "full_video") {
          modeIcon = "🎬";
          summary = `${job.stickers_count || 0} adesivos • legendado`;
        } else {
          summary = job.clip_count ? `${job.clip_count} corte(s)` : "sem cortes";
        }

        return `
          <div class="history-row">
            <button class="history-item" data-job-id="${escapeHtml(job.id)}">
              <span class="history-title">${modeIcon} ${escapeHtml(title)}</span>
              <span class="history-meta">
                <span>${date} • ${escapeHtml(summary)}</span>
                <span class="status-pill status-${escapeHtml(job.status)}">${escapeHtml(job.status)}</span>
              </span>
            </button>
            <button class="history-delete" data-job-id="${escapeHtml(job.id)}" title="Apagar do histórico">🗑️</button>
          </div>`;
      }).join("");

      $$(".history-item").forEach((item) => {
        item.addEventListener("click", () => openJob(item.dataset.jobId));
      });
      $$(".history-delete").forEach((btn) => {
        btn.addEventListener("click", async (event) => {
          event.stopPropagation();
          if (!confirm("Apagar este processamento e seus arquivos?")) return;
          try {
            await fetchJSON(`/api/jobs/${btn.dataset.jobId}`, { method: "DELETE" });
            if (state.jobId === btn.dataset.jobId) {
              state.jobId = null;
              $("#resultsSection")?.classList.add("hidden");
            }
            toast("Processamento apagado.");
            loadHistory();
          } catch (error) {
            toast(error.message);
          }
        });
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
