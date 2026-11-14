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
    el("interval").value = settings.interval || 3600;
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
      el("lastRun").textContent = formatDate(last.at) + " · 上传 " + (stats.uploaded || 0) +
        " · 跳过 " + (stats.skipped || 0) + " · 失败 " + (stats.failed || 0);
    }
    el("nextRun").textContent = sync.next_run_at ? formatDate(sync.next_run_at) : "-";
    el("syncState").textContent = sync.running ? "同步中" : "空闲";
    el("syncState").className = "tag" + (sync.running ? " active" : "");
    el("syncButton").disabled = Boolean(sync.running);
    el("dryRunButton").disabled = Boolean(sync.running);
    if (sync.error) {
      el("syncError").textContent = sync.error;
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
    el("statUploaded").textContent = stats.uploaded || 0;
    el("statPending").textContent = stats.pending || 0;
    el("statFailed").textContent = stats.failed || 0;
    el("statMissing").textContent = stats.missing || 0;
    el("statSkipped").textContent = stats.skipped || 0;
    el("syncHint").textContent = data.sync.running
      ? "正在同步，请保持应用运行。"
      : "同步任务在后台运行，可随时关闭页面。";
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
      view.textContent = data.logs || "暂无日志";
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

  function syncNow(dryRun) {
    api("/api/sync", { method: "POST", body: { dry_run: dryRun } }).then(function (data) {
      showToast(data.started ? "同步任务已启动" : "已有同步任务在运行");
      loadStatus(false);
    }).catch(function (error) {
      showToast(error.message, true);
    });
  }

  function saveSettings(event) {
    event.preventDefault();
    var dirs = el("musicDirs").value.split(/\r?\n/).map(function (line) {
      return line.trim();
    }).filter(Boolean);
    var payload = {
      music_dirs: dirs,
      interval: Number(el("interval").value),
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
    el("syncButton").addEventListener("click", function () { syncNow(false); });
    el("dryRunButton").addEventListener("click", function () { syncNow(true); });
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
