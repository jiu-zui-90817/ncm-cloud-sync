(function () {
  "use strict";

  var BASE = location.pathname.replace(/\/+$/, "");
  var state = {
    status: null,
    settingsDirty: false,
    loading: false,
    qrKey: "",
    qrTimer: null,
    toastTimer: null
  };

  function el(id) {
    return document.getElementById(id);
  }

  function api(path, options) {
    options = options || {};
    var headers = Object.assign({ "Accept": "application/json" }, options.headers || {});
    var method = (options.method || "GET").toUpperCase();
    if (method !== "GET") {
      headers["X-WY-Music"] = "web";
    }
    var init = {
      method: method,
      credentials: "same-origin",
      headers: headers
    };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    return fetch(BASE + path, init).then(function (response) {
      return response.json().catch(function () {
        return {};
      }).then(function (data) {
        if (!response.ok) {
          var message = data && data.error ? data.error : "请求失败（HTTP " + response.status + "）";
          var error = new Error(message);
          error.status = response.status;
          throw error;
        }
        return data;
      });
    });
  }

  function showToast(message, isError) {
    var node = el("toast");
    node.textContent = message;
    node.className = "toast" + (isError ? " error" : "");
    window.clearTimeout(state.toastTimer);
    state.toastTimer = window.setTimeout(function () {
      node.className = "toast hidden";
    }, 4200);
  }

  function formatDate(value) {
    if (!value) {
      return "-";
    }
    var date = new Date(value);
    if (isNaN(date.getTime())) {
      return value;
    }
    return date.toLocaleString("zh-CN", { hour12: false });
  }

  function formatInterval(seconds) {
    var value = Number(seconds || 0);
    if (value <= 0) {
      return "手动";
    }
    if (value % 3600 === 0) {
      return (value / 3600) + " 小时";
    }
    if (value % 60 === 0) {
      return (value / 60) + " 分钟";
    }
    return value + " 秒";
  }

  function syncStateLabel(value) {
    var labels = {
      idle: "空闲",
      running: "同步中",
      pausing: "正在暂停",
      paused: "已暂停",
      stopping: "正在终止",
      stopped: "已终止"
    };
    return labels[value] || "空闲";
  }

  function basename(path) {
    var parts = String(path || "").split(/[\\/]/);
    return parts[parts.length - 1] || path;
  }

  function renderProgress(items) {
    var box = el("progressBox");
    var list = el("progressList");
    if (!items || !items.length) {
      box.classList.add("hidden");
      list.textContent = "";
      return;
    }
    var labels = {
      uploaded: "已上传",
      skip_local: "本地重复，跳过",
      skip_remote: "云端已有，跳过",
      planned: "演练：待上传",
      done: "同步完成",
      finished: "任务结束"
    };
    box.classList.remove("hidden");
    list.textContent = "";
    items.slice().reverse().forEach(function (item) {
      var row = document.createElement("li");
      var tag = document.createElement("span");
      tag.className = "progress-tag";
      tag.textContent = labels[item.event] || item.event;
      var text = document.createElement("span");
      text.className = "progress-path";
      if (item.event === "done") {
        text.textContent = "上传 " + (item.uploaded || 0) + "，跳过 " + (item.skipped || 0) + "，失败 " + (item.failed || 0);
      } else if (item.event === "finished") {
        text.textContent = "耗时 " + (item.duration || 0) + " 秒";
      } else {
        text.textContent = basename(item.path);
      }
      row.appendChild(tag);
      row.appendChild(text);
      list.appendChild(row);
    });
    el("progressCount").textContent = items.length + " 条";
  }

  function renderWarnings(dirs) {
    var box = el("dirWarnings");
    box.textContent = "";
    (dirs || []).forEach(function (item) {
      if (item.exists && item.readable) {
        return;
      }
      var line = document.createElement("p");
      line.className = "warning-line";
      line.textContent = (item.exists ? "目录不可读：" : "目录不存在：") + item.path;
      box.appendChild(line);
    });
  }

  function renderSettings(status) {
    if (state.settingsDirty) {
      return;
    }
    var settings = status.settings || {};
    el("musicDirs").value = (settings.music_dirs || []).join("\n");
    el("excludeDirs").value = (settings.exclude_dirs || []).join("\n");
    el("interval").value = settings.interval || 3600;
    el("rateLimit").value = settings.rate_limit != null ? settings.rate_limit : 1;
    el("dryRun").checked = Boolean(settings.dry_run);
    el("logLevel").value = settings.log_level || "INFO";
    el("intervalLabel").textContent = formatInterval(settings.interval || status.config.interval);
  }

  function renderLastRun(sync) {
    var last = sync.last_run;
    if (!last) {
      el("lastRun").textContent = "尚未运行";
    } else {
      var stats = last.stats || {};
      var text = formatDate(last.at) + " · 上传 " + (stats.uploaded || 0) +
        " · 跳过 " + (stats.skipped || 0) + " · 失败 " + (stats.failed || 0);
      if (stats.permission_denied) {
        text += "（无权限 " + stats.permission_denied + "）";
      }
      if (stats.deferred) {
        text += " · 待续 " + stats.deferred;
      }
      if (stats.stopped) {
        text += " · 已终止";
      } else if (stats.paused) {
        text += " · 已暂停";
      }
      el("lastRun").textContent = text;
    }
    el("nextRun").textContent = sync.next_run_at ? formatDate(sync.next_run_at) : "-";
    var syncState = sync.state || (sync.running ? "running" : "idle");
    el("syncState").textContent = syncStateLabel(syncState);
    el("syncState").className = "tag" + (sync.running ? " active" : "");
    el("syncButton").disabled = Boolean(sync.running);
    el("dryRunButton").disabled = Boolean(sync.running);
    el("pauseButton").disabled = !sync.can_pause;
    el("stopButton").disabled = !sync.can_stop;
    el("resumeButton").disabled = !sync.can_resume;
    var lastStats = (sync.last_run && sync.last_run.stats) || {};
    var syncError = sync.error || lastStats.error || (sync.last_run && sync.last_run.error);
    if (syncError) {
      el("syncError").textContent = syncError;
      el("syncError").classList.remove("hidden");
    } else {
      el("syncError").classList.add("hidden");
    }
    renderProgress(sync.progress);
  }

  function renderStatus(data) {
    state.status = data;
    el("version").textContent = "v" + data.version;
    el("accountBadge").textContent = data.logged_in
      ? (data.account && data.account.nickname ? data.account.nickname : "已登录")
      : "未登录";
    el("accountBadge").className = "pill" + (data.logged_in ? " ok" : " muted");
    el("loginCard").classList.toggle("hidden", Boolean(data.logged_in));
    el("dashboard").classList.toggle("hidden", !data.logged_in);
    if (!data.logged_in) {
      return;
    }
    el("nickname").textContent = (data.account && data.account.nickname) || "网易云用户";
    el("userId").textContent = (data.account && data.account.userId)
      ? "用户 ID " + data.account.userId
      : "账号信息将在下一次刷新时更新";
    var stats = data.stats || {};
    var lastStats = (data.sync && data.sync.last_run && data.sync.last_run.stats) || {};
    el("statUploaded").textContent = stats.uploaded || 0;
    el("statPending").textContent = stats.pending || 0;
    el("statFailed").textContent = stats.failed || 0;
    el("statPermission").textContent = lastStats.permission_denied || 0;
    el("statMissing").textContent = stats.missing || 0;
    el("statSkipped").textContent = stats.skipped || 0;
    if (data.sync.running) {
      el("syncHint").textContent = "正在同步，请保持应用运行。";
    } else if (data.sync.state === "paused") {
      el("syncHint").textContent = "同步已暂停，点击继续会重新开始一轮。";
    } else if (data.sync.state === "stopped") {
      el("syncHint").textContent = "同步已终止，点击继续会重新开始一轮。";
    } else {
      el("syncHint").textContent = "同步任务在后台运行，可随时关闭页面。";
    }
    renderLastRun(data.sync || {});
    renderSettings(data);
    renderWarnings(data.music_dirs || []);
  }

  function loadStatus(silent) {
    if (state.loading) {
      return;
    }
    state.loading = true;
    api("/api/status").then(function (data) {
      renderStatus(data);
    }).catch(function (error) {
      if (!silent) {
        showToast(error.message, true);
      }
    }).finally(function () {
      state.loading = false;
    });
  }

  function loadLogs() {
    api("/api/logs?lines=200").then(function (data) {
      var view = el("logView");
      var paths = (data.log_paths || []).join("\n");
      view.textContent = (paths ? "# 日志文件：" + paths + "\n\n" : "") + (data.logs || "暂无日志");
      view.scrollTop = view.scrollHeight;
    }).catch(function (error) {
      el("logView").textContent = error.message;
    });
  }

  function stopQrPolling() {
    if (state.qrTimer) {
      window.clearInterval(state.qrTimer);
      state.qrTimer = null;
    }
  }

  function pollQr() {
    if (!state.qrKey) {
      return;
    }
    api("/api/qr/status?key=" + encodeURIComponent(state.qrKey)).then(function (data) {
      el("qrStatus").textContent = data.message || "等待扫码";
      if (data.code === 803 || data.logged_in) {
        stopQrPolling();
        el("qrStatus").textContent = "登录成功，正在刷新状态…";
        state.qrKey = "";
        showToast("登录成功");
        loadStatus(false);
      } else if (data.code === 800 || data.code === 404) {
        stopQrPolling();
        state.qrKey = "";
        el("qrImage").textContent = "";
        el("qrPlaceholder").classList.remove("hidden");
      }
    }).catch(function (error) {
      el("qrStatus").textContent = error.message;
      if (error.status === 401) {
        stopQrPolling();
        state.qrKey = "";
        el("qrImage").textContent = "";
        el("qrPlaceholder").classList.remove("hidden");
      }
    });
  }

  function startQr() {
    var button = el("qrButton");
    button.disabled = true;
    el("qrStatus").textContent = "正在生成二维码…";
    api("/api/qr", { method: "POST" }).then(function (data) {
      state.qrKey = data.key;
      el("qrImage").innerHTML = data.qr_svg;
      el("qrPlaceholder").classList.add("hidden");
      el("qrStatus").textContent = "请用网易云音乐 App 扫码";
      stopQrPolling();
      state.qrTimer = window.setInterval(pollQr, 2000);
      pollQr();
    }).catch(function (error) {
      el("qrStatus").textContent = error.message;
      showToast(error.message, true);
    }).finally(function () {
      button.disabled = false;
    });
  }

  function cookieLogin() {
    var button = el("cookieButton");
    var value = el("cookieInput").value.trim();
    if (!value) {
      el("cookieStatus").textContent = "请先粘贴 Cookie";
      return;
    }
    button.disabled = true;
    el("cookieStatus").textContent = "正在验证 Cookie…";
    api("/api/login/cookie", { method: "POST", body: { cookie: value } }).then(function () {
      el("cookieStatus").textContent = "登录成功，正在刷新…";
      el("cookieInput").value = "";
      showToast("登录成功");
      loadStatus(false);
    }).catch(function (error) {
      el("cookieStatus").textContent = error.message;
      showToast(error.message, true);
    }).finally(function () {
      button.disabled = false;
    });
  }

  function formatDiagnostics(data) {
    var lines = [];
    lines.push("版本: " + (data.version || "-"));
    lines.push("数据目录: " + (data.data_dir || "-"));
    lines.push("设置文件: " + (data.settings_path || "-"));
    lines.push("Python: " + (data.python || "-") + "  " + (data.platform || "-"));
    lines.push("进程 PID: " + (data.pid || "-"));
    lines.push("Unix Socket: " + (data.socket || "(未启用)"));
    lines.push("工作目录: " + (data.cwd || "-"));
    ["TRIM_APPDEST", "TRIM_PKGVAR", "WY_MUSIC_DATA_DIR", "WY_MUSIC_SETTINGS", "WY_MUSIC_LOG_FILE"].forEach(function (key) {
      lines.push(key + ": " + ((data.env && data.env[key]) || "(空)"));
    });
    lines.push("");
    lines.push("日志文件:");
    (data.log_paths || []).forEach(function (item) {
      lines.push("  " + item.path + "  " + (item.exists ? ("存在，" + (item.size || 0) + " 字节") : "不存在"));
    });
    lines.push("");
    lines.push("最近日志:");
    lines.push(data.logs || "暂无日志");
    return lines.join("\n");
  }

  function collectDiagnostics() {
    return api("/api/diagnostics").then(formatDiagnostics);
  }

  function loadDiagnostics(viewId) {
    var view = el(viewId || "diagnosticsView");
    view.classList.remove("hidden");
    view.textContent = "加载中…";
    return collectDiagnostics().then(function (text) {
      view.textContent = text;
      return text;
    }).catch(function (error) {
      view.textContent = error.message;
      return "";
    });
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      try {
        if (document.execCommand("copy")) {
          resolve();
        } else {
          reject(new Error("复制失败，请手动选择文本"));
        }
      } catch (error) {
        reject(error);
      } finally {
        document.body.removeChild(area);
      }
    });
  }

  function copyDiagnostics() {
    el("diagnosticsStatus").textContent = "正在收集…";
    collectDiagnostics().then(function (text) {
      return copyText(text).then(function () {
        el("diagnosticsStatus").textContent = "已复制";
        showToast("诊断信息已复制");
      });
    }).catch(function (error) {
      el("diagnosticsStatus").textContent = error.message;
      showToast(error.message, true);
    });
  }

  function syncNow(dryRun) {
    api("/api/sync", { method: "POST", body: { dry_run: dryRun, force: el("forceRecheck").checked } }).then(function (data) {
      showToast(data.started ? "同步任务已启动" : "已有同步任务在运行");
      loadStatus(false);
    }).catch(function (error) {
      showToast(error.message, true);
    });
  }

  function syncAction(path, message) {
    api(path, { method: "POST" }).then(function () {
      showToast(message);
      loadStatus(false);
    }).catch(function (error) {
      showToast(error.message, true);
      loadStatus(false);
    });
  }

  function pauseSync() {
    syncAction("/api/sync/pause", "正在暂停，当前文件处理完后停止");
  }

  function stopSync() {
    if (!window.confirm("终止本次同步？当前文件会处理完，未处理的文件下一轮继续。")) {
      return;
    }
    syncAction("/api/sync/stop", "正在终止同步");
  }

  function resumeSync() {
    syncAction("/api/sync/resume", "同步任务已继续");
  }

  function saveSettings(event) {
    event.preventDefault();
    var dirs = el("musicDirs").value.split(/\r?\n/).map(function (line) {
      return line.trim();
    }).filter(Boolean);
    var excludes = el("excludeDirs").value.split(/\r?\n/).map(function (line) {
      return line.trim();
    }).filter(Boolean);
    var payload = {
      music_dirs: dirs,
      exclude_dirs: excludes,
      interval: Number(el("interval").value),
      rate_limit: Number(el("rateLimit").value),
      dry_run: el("dryRun").checked,
      log_level: el("logLevel").value
    };
    api("/api/settings", { method: "PUT", body: payload }).then(function () {
      state.settingsDirty = false;
      el("settingsStatus").textContent = "已保存";
      showToast("设置已保存");
      loadStatus(false);
    }).catch(function (error) {
      el("settingsStatus").textContent = error.message;
      showToast(error.message, true);
    });
  }

  function logout() {
    if (!window.confirm("退出后需要重新扫码登录，确定退出吗？")) {
      return;
    }
    api("/api/logout", { method: "POST" }).then(function () {
      showToast("已退出登录");
      loadStatus(false);
    }).catch(function (error) {
      showToast(error.message, true);
    });
  }

  function refreshAccount() {
    api("/api/account/refresh", { method: "POST" }).then(function () {
      showToast("账号信息已刷新");
      loadStatus(false);
    }).catch(function (error) {
      showToast(error.message, true);
    });
  }

  function bind() {
    el("qrButton").addEventListener("click", startQr);
    el("cookieButton").addEventListener("click", cookieLogin);
    el("diagnosticsButton").addEventListener("click", function () { loadDiagnostics("diagnosticsView"); });
    el("loginDiagnosticsButton").addEventListener("click", function () { loadDiagnostics("loginDiagnosticsView"); });
    el("copyDiagnosticsButton").addEventListener("click", copyDiagnostics);
    el("copyDiagnosticsDashboard").addEventListener("click", copyDiagnostics);
    el("logCopyDiagnostics").addEventListener("click", copyDiagnostics);
    el("loginDiagnostics").addEventListener("toggle", function () {
      if (el("loginDiagnostics").open && el("loginDiagnosticsView").classList.contains("hidden")) {
        loadDiagnostics("loginDiagnosticsView");
      }
    });
    el("syncButton").addEventListener("click", function () { syncNow(false); });
    el("dryRunButton").addEventListener("click", function () { syncNow(true); });
    el("pauseButton").addEventListener("click", pauseSync);
    el("stopButton").addEventListener("click", stopSync);
    el("resumeButton").addEventListener("click", resumeSync);
    el("refreshButton").addEventListener("click", function () { loadStatus(false); });
    el("logoutButton").addEventListener("click", logout);
    el("refreshAccount").addEventListener("click", refreshAccount);
    el("refreshLogs").addEventListener("click", loadLogs);
    el("settingsForm").addEventListener("submit", saveSettings);
    el("settingsForm").addEventListener("input", function () {
      state.settingsDirty = true;
      el("settingsStatus").textContent = "有未保存的修改";
    });
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) {
        loadStatus(true);
      }
    });
    window.setInterval(function () {
      if (!document.hidden) {
        loadStatus(true);
      }
    }, 5000);
  }

  document.addEventListener("DOMContentLoaded", function () {
    bind();
    loadStatus(false);
    loadLogs();
  });
})();
