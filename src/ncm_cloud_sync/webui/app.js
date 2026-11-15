(function () {
  "use strict";

  var BASE = location.pathname.replace(/\/+$/, "");
  var state = {
    status: null,
    settingsDirty: false,
    notifyDirty: false,
    loading: false,
    qrKey: "",
    qrTimer: null,
    toastTimer: null,
    page: "overview",
    problemKind: "failed",
    problemCounts: { failed: 0, permission: 0 },
    notifyTypes: [],
    notifications: [],
    updateLoaded: false,
    updateLoading: false,
    updateInfo: null
  };

  var FIELD_LABELS = {
    bot_token: "Bot Token",
    chat_id: "Chat ID",
    api_base: "API 地址（可选）",
    url: "推送地址",
    group: "分组（可选）",
    server: "服务器地址",
    topic: "主题 / Topic",
    token: "Token",
    secret: "加签密钥（可选）",
    keyword: "自定义关键词（可选，逗号分隔）",
    at_mobiles: "被 @ 手机号（可选，逗号分隔）",
    priority: "优先级（可选）",
    webhook_url: "Webhook 地址",
    sendkey: "SendKey",
    user: "User Key",
    pushkey: "PushKey",
    host: "SMTP 服务器",
    port: "端口（可选）",
    username: "用户名（可选）",
    password: "密码（可选）",
    mail_from: "发件人",
    mail_to: "收件人（逗号分隔）",
    security: "加密方式（ssl/starttls/none）"
  };

  function el(id) {
    return document.getElementById(id);
  }

  function api(path, options) {
    options = options || {};
    var headers = Object.assign({ "Accept": "application/json" }, options.headers || {});
    var method = (options.method || "GET").toUpperCase();
    if (method !== "GET") {
      headers["X-NCM-Cloud-Sync"] = "web";
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

  function formatBytes(value) {
    var size = Number(value || 0);
    if (size < 1024) {
      return size + " B";
    }
    if (size < 1024 * 1024) {
      return (size / 1024).toFixed(1) + " KB";
    }
    if (size < 1024 * 1024 * 1024) {
      return (size / 1024 / 1024).toFixed(1) + " MB";
    }
    return (size / 1024 / 1024 / 1024).toFixed(2) + " GB";
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

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = text;
    }
    return node;
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
    el("autoSync").checked = settings.auto_sync !== false;
    el("logLevel").value = settings.log_level || "INFO";
    el("notifyOn").value = settings.notify_on || "always";
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
    if (sync.auto_sync === false) {
      el("nextRun").textContent = "自动同步已关闭";
    } else {
      el("nextRun").textContent = sync.next_run_at ? formatDate(sync.next_run_at) : "-";
    }
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
    el("statUploaded").textContent = stats.uploaded || 0;
    el("statPending").textContent = stats.pending || 0;
    el("statFailed").textContent = stats.failed || 0;
    el("statPermission").textContent = stats.permission || 0;
    el("statMissing").textContent = stats.missing || 0;
    el("statSkipped").textContent = stats.skipped || 0;
    if (!data.sync.auto_sync) {
      el("syncHint").textContent = "自动同步已关闭，仍可手动开始同步。";
    } else if (data.sync.running) {
      el("syncHint").textContent = "正在同步，请保持应用运行。";
    } else if (data.sync.state === "paused") {
      el("syncHint").textContent = "同步已暂停，点击继续会重新开始一轮。";
    } else if (data.sync.state === "stopped") {
      el("syncHint").textContent = "已终止本次同步，自动同步仍会按计划运行。";
    } else {
      el("syncHint").textContent = "同步任务在后台运行，可随时关闭页面。";
    }
    renderLastRun(data.sync || {});
    renderSettings(data);
    if (!state.notifyDirty) {
      state.notifications = JSON.parse(JSON.stringify((data.settings && data.settings.notifications) || []));
      renderNotifications();
    }
    renderWarnings(data.music_dirs || []);
    if (state.page === "problems") {
      loadProblems(state.problemKind, true);
    }
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

  function showPage(name) {
    state.page = name;
    Array.prototype.forEach.call(document.querySelectorAll(".nav-item"), function (item) {
      item.classList.toggle("active", item.getAttribute("data-page") === name);
    });
    ["overview", "problems", "settings", "notifications", "about", "logs"].forEach(function (page) {
      var node = el("page-" + page);
      if (node) {
        node.classList.toggle("hidden", page !== name);
      }
    });
    if (name === "problems") {
      loadProblems(state.problemKind, false);
    } else if (name === "logs") {
      loadLogs();
    } else if (name === "notifications") {
      renderNotifications();
    } else if (name === "about") {
      loadUpdate(false);
    }
  }

  function updateProblemSwitches() {
    Array.prototype.forEach.call(document.querySelectorAll(".problem-switch .chip"), function (item) {
      item.classList.toggle("active", item.getAttribute("data-problem-kind") === state.problemKind);
    });
  }

  function renderProblems(data) {
    state.problemCounts = data.counts || { failed: 0, permission: 0 };
    el("problemFailedCount").textContent = state.problemCounts.failed || 0;
    el("problemPermissionCount").textContent = state.problemCounts.permission || 0;
    var list = el("problemList");
    list.textContent = "";
    var items = data.items || [];
    el("problemEmpty").classList.toggle("hidden", items.length > 0);
    items.forEach(function (item) {
      var row = make("div", "problem-row");
      var head = make("div", "problem-head");
      head.appendChild(make("span", "problem-path", item.path));
      var copy = make("button", "btn tiny ghost", "复制路径");
      copy.type = "button";
      copy.addEventListener("click", function () {
        copyText(item.path).then(function () {
          showToast("路径已复制");
        }).catch(function (error) {
          showToast(error.message, true);
        });
      });
      head.appendChild(copy);
      row.appendChild(head);
      var meta = make("div", "problem-meta");
      meta.appendChild(make("span", "", "最后更新 " + formatDate(item.updated_at)));
      meta.appendChild(make("span", "", "大小 " + formatBytes(item.size)));
      meta.appendChild(make("span", "", "尝试 " + (item.attempts || 0) + " 次"));
      row.appendChild(meta);
      if (item.last_error) {
        row.appendChild(make("p", "problem-error", item.last_error));
      }
      list.appendChild(row);
    });
  }

  function loadProblems(kind, silent) {
    if (kind) {
      state.problemKind = kind;
    }
    updateProblemSwitches();
    return api("/api/problems?kind=" + encodeURIComponent(state.problemKind) + "&limit=200").then(function (data) {
      renderProblems(data);
    }).catch(function (error) {
      if (!silent) {
        showToast(error.message, true);
      }
      el("problemStatus").textContent = error.message;
    });
  }

  function retryProblems() {
    api("/api/problems/retry", { method: "POST", body: { kind: state.problemKind } }).then(function (data) {
      el("problemStatus").textContent = "已重置 " + (data.reset || 0) + " 个文件，下次同步会重新尝试。";
      showToast("已重置 " + (data.reset || 0) + " 个文件");
      loadProblems(state.problemKind, false);
      loadStatus(true);
    }).catch(function (error) {
      el("problemStatus").textContent = error.message;
      showToast(error.message, true);
    });
  }

  function renderUpdate(data) {
    state.updateInfo = data;
    var current = data.current || "";
    var latest = data.latest || "";
    el("aboutVersion").textContent = current ? "v" + current : "v-";
    el("aboutCurrentVersion").textContent = current ? "v" + current : "-";
    el("updateLatest").textContent = latest ? "v" + latest : "-";
    el("updatePublished").textContent = data.published_at
      ? String(data.published_at).slice(0, 10)
      : "-";
    var link = el("updateLink");
    link.href = data.release_url || data.releases_url || link.href;
    var notes = el("updateNotes");
    if (data.notes) {
      notes.textContent = data.notes;
      notes.classList.remove("hidden");
    } else {
      notes.textContent = "";
      notes.classList.add("hidden");
    }
    if (data.error) {
      el("updateStatus").textContent = data.error;
    } else if (data.update_available) {
      el("updateStatus").textContent = "发现新版本 v" + latest + "，可前往 Release 下载。";
    } else {
      el("updateStatus").textContent = "已是最新版本（v" + current + "）。";
    }
  }

  function loadUpdate(force) {
    if (state.updateLoading || (!force && state.updateLoaded)) {
      return;
    }
    state.updateLoading = true;
    var button = el("checkUpdateButton");
    button.disabled = true;
    el("updateStatus").textContent = "正在检查更新…";
    api("/api/update" + (force ? "?force=1" : "")).then(function (data) {
      state.updateLoaded = true;
      renderUpdate(data);
    }).catch(function (error) {
      el("updateStatus").textContent = error.message;
    }).finally(function () {
      state.updateLoading = false;
      button.disabled = false;
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

  function notifyMeta(type) {
    for (var index = 0; index < state.notifyTypes.length; index += 1) {
      if (state.notifyTypes[index].type === type) {
        return state.notifyTypes[index];
      }
    }
    return { type: type, label: type, fields: [], required: [], secret: [] };
  }

  function loadNotifyTypes() {
    return api("/api/notify/types").then(function (data) {
      state.notifyTypes = data.types || [];
      var select = el("notifyType");
      select.textContent = "";
      state.notifyTypes.forEach(function (item) {
        var option = document.createElement("option");
        option.value = item.type;
        option.textContent = item.label;
        select.appendChild(option);
      });
      renderNotifications();
    }).catch(function (error) {
      showToast(error.message, true);
    });
  }

  function renderNotifications() {
    var list = el("notifyList");
    if (!list) {
      return;
    }
    list.textContent = "";
    if (!state.notifications.length) {
      list.appendChild(make("div", "empty-state", "还没有通知渠道。选择上面的类型后点“添加”。"));
      return;
    }
    state.notifications.forEach(function (channel, index) {
      var meta = notifyMeta(channel.type);
      var card = make("div", "notify-card");
      card.setAttribute("data-index", String(index));
      var head = make("div", "notify-head");
      var title = make("div", "notify-title");
      title.appendChild(make("strong", "", meta.label || channel.type));
      var nameInput = document.createElement("input");
      nameInput.className = "notify-name";
      nameInput.type = "text";
      nameInput.placeholder = "备注名（可选）";
      nameInput.value = channel.name || "";
      nameInput.setAttribute("data-field", "__name");
      title.appendChild(nameInput);
      head.appendChild(title);
      var actions = make("div", "notify-actions");
      var enabledLabel = make("label", "check-field compact");
      var enabled = document.createElement("input");
      enabled.type = "checkbox";
      enabled.checked = channel.enabled !== false;
      enabled.setAttribute("data-field", "__enabled");
      enabledLabel.appendChild(enabled);
      enabledLabel.appendChild(make("span", "", "启用"));
      actions.appendChild(enabledLabel);
      var test = make("button", "btn tiny ghost", "测试");
      test.type = "button";
      test.setAttribute("data-action", "test");
      actions.appendChild(test);
      var remove = make("button", "btn tiny danger-ghost", "删除");
      remove.type = "button";
      remove.setAttribute("data-action", "delete");
      actions.appendChild(remove);
      head.appendChild(actions);
      card.appendChild(head);

      var grid = make("div", "notify-grid");
      (meta.fields || []).forEach(function (field) {
        var label = make("label", "field");
        var required = (meta.required || []).indexOf(field) >= 0;
        label.appendChild(make("span", "", (FIELD_LABELS[field] || field) + (required ? " *" : "")));
        var input = document.createElement("input");
        input.type = (meta.secret || []).indexOf(field) >= 0 ? "password" : "text";
        input.value = (channel.config && channel.config[field]) || "";
        input.setAttribute("data-field", field);
        input.autocomplete = "off";
        label.appendChild(input);
        grid.appendChild(label);
      });
      card.appendChild(grid);
      list.appendChild(card);
    });
  }

  function onNotifyInput(event) {
    var target = event.target;
    var card = target.closest ? target.closest(".notify-card") : null;
    if (!card) {
      return;
    }
    var index = Number(card.getAttribute("data-index"));
    var channel = state.notifications[index];
    if (!channel) {
      return;
    }
    var field = target.getAttribute("data-field");
    if (field === "__enabled") {
      channel.enabled = Boolean(target.checked);
    } else if (field === "__name") {
      channel.name = target.value;
    } else if (field) {
      channel.config = channel.config || {};
      channel.config[field] = target.value;
    }
    state.notifyDirty = true;
    el("notifyStatus").textContent = "有未保存的修改";
  }

  function onNotifyClick(event) {
    var target = event.target;
    var action = target.getAttribute ? target.getAttribute("data-action") : "";
    if (!action) {
      return;
    }
    var card = target.closest ? target.closest(".notify-card") : null;
    if (!card) {
      return;
    }
    var index = Number(card.getAttribute("data-index"));
    var channel = state.notifications[index];
    if (!channel) {
      return;
    }
    if (action === "delete") {
      state.notifications.splice(index, 1);
      state.notifyDirty = true;
      renderNotifications();
      el("notifyStatus").textContent = "已删除，保存后生效";
      return;
    }
    if (action === "test") {
      target.disabled = true;
      el("notifyStatus").textContent = "正在发送测试通知…";
      api("/api/notify/test", { method: "POST", body: { channel: channel } }).then(function () {
        el("notifyStatus").textContent = "测试通知已发送";
        showToast("测试通知已发送");
      }).catch(function (error) {
        el("notifyStatus").textContent = error.message;
        showToast(error.message, true);
      }).finally(function () {
        target.disabled = false;
      });
    }
  }

  function addNotifyChannel() {
    var type = el("notifyType").value;
    if (!type) {
      return;
    }
    var meta = notifyMeta(type);
    state.notifications.push({
      id: "",
      type: type,
      name: meta.label || type,
      enabled: true,
      config: {}
    });
    state.notifyDirty = true;
    renderNotifications();
    el("notifyStatus").textContent = "已添加，填写参数后保存";
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
    ["TRIM_APPDEST", "TRIM_PKGVAR", "NCM_CLOUD_SYNC_DATA_DIR", "NCM_CLOUD_SYNC_SETTINGS", "NCM_CLOUD_SYNC_LOG_FILE"].forEach(function (key) {
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
    if (!window.confirm("终止本次同步？当前文件会处理完，自动同步不会关闭。")) {
      return;
    }
    syncAction("/api/sync/stop", "正在终止本次同步");
  }

  function resumeSync() {
    syncAction("/api/sync/resume", "同步任务已继续");
  }

  function saveSettings(event) {
    if (event) {
      event.preventDefault();
    }
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
      auto_sync: el("autoSync").checked,
      log_level: el("logLevel").value,
      notify_on: el("notifyOn").value,
      notifications: state.notifications
    };
    api("/api/settings", { method: "PUT", body: payload }).then(function () {
      state.settingsDirty = false;
      state.notifyDirty = false;
      el("settingsStatus").textContent = "已保存";
      el("notifyStatus").textContent = "已保存";
      showToast("设置已保存");
      loadStatus(false);
    }).catch(function (error) {
      el("settingsStatus").textContent = error.message;
      el("notifyStatus").textContent = error.message;
      showToast(error.message, true);
    });
  }

  function saveNotifications() {
    saveSettings();
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

  function bindProblemEntry(node) {
    node.addEventListener("click", function () {
      state.problemKind = node.getAttribute("data-problem-kind") || "failed";
      updateProblemSwitches();
      showPage("problems");
    });
    node.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        node.click();
      }
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
    el("saveNotifications").addEventListener("click", saveNotifications);
    el("addNotifyChannel").addEventListener("click", addNotifyChannel);
    el("notifyList").addEventListener("input", onNotifyInput);
    el("notifyList").addEventListener("change", onNotifyInput);
    el("notifyList").addEventListener("click", onNotifyClick);
    el("notifyOn").addEventListener("change", function () {
      state.notifyDirty = true;
      el("notifyStatus").textContent = "有未保存的修改";
    });
    el("refreshProblems").addEventListener("click", function () { loadProblems(state.problemKind, false); });
    el("retryProblems").addEventListener("click", retryProblems);
    el("checkUpdateButton").addEventListener("click", function () { loadUpdate(true); });
    Array.prototype.forEach.call(document.querySelectorAll(".nav-item"), function (item) {
      item.addEventListener("click", function () { showPage(item.getAttribute("data-page")); });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".problem-switch .chip"), function (item) {
      item.addEventListener("click", function () { loadProblems(item.getAttribute("data-problem-kind"), false); });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".stat.clickable"), function (item) {
      bindProblemEntry(item);
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
    loadNotifyTypes();
    loadStatus(false);
    loadLogs();
  });
})();
