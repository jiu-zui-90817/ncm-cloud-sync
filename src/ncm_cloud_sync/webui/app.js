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
    problemKind: "all",
    problemCounts: { failed: 0, permission: 0 },
    notifyTypes: [],
    notifications: [],
    updateLoaded: false,
    updateLoading: false,
    updateInfo: null,
    capacityLoaded: false,
    capacityLoading: false,
    signinBusy: false,
    runDetail: { runId: null, kind: "all", items: [], total: 0, offset: 0, limit: 200, loading: false, run: null, counts: {} },
    fileDetail: { kind: null, items: [], total: 0, loading: false, source: null, finishedAt: null, counts: {} }
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

  function formatClock(value) {
    var text = String(value || "");
    var match = text.match(/[T ](\d{2}:\d{2})/);
    if (match) {
      return match[1];
    }
    var date = new Date(value);
    if (isNaN(date.getTime())) {
      return value || "-";
    }
    return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  }

  function formatRunTime(value) {
    var date = new Date(value);
    if (isNaN(date.getTime())) {
      return value || "-";
    }
    return date.toLocaleString("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false
    });
  }

  function formatRunDuration(started, finished) {
    var start = new Date(started).getTime();
    var end = new Date(finished).getTime();
    if (isNaN(start) || isNaN(end) || end < start) {
      return "-";
    }
    var seconds = Math.round((end - start) / 1000);
    if (seconds < 60) {
      return seconds + " 秒";
    }
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) {
      return minutes + " 分 " + (seconds % 60) + " 秒";
    }
    return Math.floor(minutes / 60) + " 小时 " + (minutes % 60) + " 分";
  }

  function renderRuns(runs) {
    var list = el("runHistory");
    if (!list) {
      return;
    }
    list.textContent = "";
    if (!runs || !runs.length) {
      var empty = document.createElement("li");
      empty.className = "run-empty";
      empty.textContent = "暂无执行记录";
      list.appendChild(empty);
      return;
    }
    runs.forEach(function (run) {
      var item = document.createElement("li");
      item.className = "run-row clickable";
      item.tabIndex = 0;
      item.setAttribute("role", "button");
      item.title = "查看逐文件详情";
      var head = document.createElement("div");
      head.className = "run-head";
      var left = document.createElement("div");
      left.className = "run-head-left";
      var badge = document.createElement("span");
      badge.className = "run-badge" + (run.mode === "scan" ? " scan" : "");
      badge.textContent = run.mode === "scan" ? "仅扫描" : (run.mode === "dry_run" ? "演练" : "同步");
      left.appendChild(badge);
      var when = document.createElement("strong");
      when.textContent = formatRunTime(run.started_at) + " → " + formatRunTime(run.finished_at);
      left.appendChild(when);
      var duration = document.createElement("span");
      duration.className = "run-duration";
      duration.textContent = "耗时 " + formatRunDuration(run.started_at, run.finished_at);
      head.appendChild(left);
      head.appendChild(duration);
      var stats = document.createElement("div");
      stats.className = "run-stats";
      var statTexts;
      if (run.mode === "scan") {
        var data = run.stats || {};
        statTexts = [
          "扫描 " + (run.scanned || 0),
          "待上传 " + (data.to_upload || 0),
          "云端独有 " + (data.cloud_only || 0),
          "已匹配 " + (data.matched || 0),
          "本地重复 " + (data.duplicate_local || 0),
          "失败 " + (run.failed || 0)
        ];
      } else {
        statTexts = [
          "扫描 " + (run.scanned || 0),
          "上传 " + (run.uploaded || 0),
          "跳过 " + (run.skipped || 0),
          "失败 " + (run.failed || 0)
        ];
      }
      statTexts.forEach(function (text) {
        var span = document.createElement("span");
        span.textContent = text;
        stats.appendChild(span);
      });
      item.appendChild(head);
      item.appendChild(stats);
      if (run.error) {
        var error = document.createElement("p");
        error.className = "run-error";
        error.textContent = run.error;
        item.appendChild(error);
      }
      item.addEventListener("click", function () { openRunDetail(run.id); });
      item.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openRunDetail(run.id);
        }
      });
      list.appendChild(item);
    });
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

  function renderCapacity(data) {
    var capacity = data && data.capacity ? data.capacity : data;
    var text = el("capacityText");
    var hint = el("capacityHint");
    var fill = el("capacityFill");
    if (!capacity || !capacity.supported || Number(capacity.total_bytes || 0) <= 0) {
      text.textContent = "暂不可用";
      hint.textContent = "网易云没有返回容量数据，可能是账号不支持或接口调整。";
      fill.className = "capacity-fill";
      fill.style.width = "0%";
      fill.parentNode.setAttribute("aria-valuenow", "0");
      return;
    }
    var total = Number(capacity.total_bytes) || 0;
    var used = Number(capacity.used_bytes) || 0;
    var free = Number(capacity.free_bytes) || 0;
    var percent = Number(capacity.percent);
    if (isNaN(percent)) {
      percent = total > 0 ? Math.min(used * 100 / total, 100) : 0;
    }
    percent = Math.max(0, Math.min(percent, 100));
    text.textContent = formatBytes(used) + " / " + formatBytes(total);
    hint.textContent = "剩余 " + formatBytes(free) + " · 已用 " + percent.toFixed(1) + "%";
    fill.className = "capacity-fill" + (percent >= 90 ? " danger" : percent >= 75 ? " warn" : "");
    fill.style.width = percent.toFixed(1) + "%";
    fill.parentNode.setAttribute("aria-valuenow", percent.toFixed(1));
  }

  function loadCapacity(silent) {
    if (state.capacityLoading) {
      return;
    }
    state.capacityLoading = true;
    api("/api/capacity").then(function (data) {
      state.capacityLoaded = true;
      renderCapacity(data);
    }).catch(function (error) {
      state.capacityLoaded = true;
      el("capacityText").textContent = "读取失败";
      el("capacityHint").textContent = error.message;
      el("capacityFill").className = "capacity-fill";
      el("capacityFill").style.width = "0%";
      if (!silent) {
        showToast(error.message, true);
      }
    }).finally(function () {
      state.capacityLoading = false;
    });
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
      scan_local: "扫描本地",
      scan: "检查文件",
      cloud_loaded: "读取云盘",
      done: "任务完成",
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
      if (item.event === "done" && item.mode === "scan") {
        text.textContent = "本地候选 " + (item.scanned || 0) + "，待上传 " + (item.to_upload || 0) + "，云端独有 " + (item.cloud_only || 0) + "，已匹配 " + (item.matched || 0);
      } else if (item.event === "done") {
        text.textContent = "上传 " + (item.uploaded || 0) + "，跳过 " + (item.skipped || 0) + "，失败 " + (item.failed || 0);
      } else if (item.event === "scan_local") {
        text.textContent = "候选 " + (item.total || 0) + " 个";
      } else if (item.event === "cloud_loaded") {
        text.textContent = "云盘 " + (item.count || 0) + " 首";
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

  function signinDetailText(details) {
    return (details || []).map(function (item) {
      var label = item.label || "签到";
      var message = item.message || item.status || "";
      var suffix = item.points ? " +" + item.points : "";
      return label + "：" + message + suffix;
    }).join("；");
  }

  function renderSignin(signin) {
    var data = signin || {};
    var signed = Boolean(data.today && (data.status === "success" || data.status === "already"));
    var failed = Boolean(data.today && data.status === "failed");
    var text = "今日未签到";
    if (!data.enabled) {
      text = "未启用";
    } else if (signed) {
      text = "今日已签到";
      if (data.points) {
        text += " · 云贝 +" + data.points;
      }
    } else if (failed) {
      text = "今日签到失败";
    } else if (data.target) {
      text = "今日随机签到 · " + formatClock(data.target);
    }
    el("signinStatusText").textContent = text;
    var windowText = (data.window_start || "08:00") + "–" + (data.window_end || "23:00");
    var hint = "签到只获得云贝，不是云盘空间；自动签到在 " + windowText +
      " 之间随机进行，仍可能触发平台风控，请自行评估。";
    var detailText = signinDetailText(data.details);
    if (data.today && detailText) {
      hint = detailText + " · " + hint;
    } else if (data.today && data.message) {
      hint = data.message + " · " + hint;
    }
    el("signinHint").textContent = hint;
    var button = el("headerSignin");
    button.textContent = state.signinBusy ? "签到中…" : (signed ? "今日已签到" : (failed ? "重试签到" : "立即签到"));
    button.disabled = !data.enabled || signed || state.signinBusy;
    button.title = data.enabled ? "" : "请先在设置中启用每日签到";
  }

  function renderSettings(status) {
    renderSignin(status.signin);
    if (state.settingsDirty) {
      return;
    }
    var settings = status.settings || {};
    el("musicDirs").value = (settings.music_dirs || []).join("\n");
    el("excludeDirs").value = (settings.exclude_dirs || []).join("\n");
    el("interval").value = settings.interval || 3600;
    el("rateLimit").value = settings.rate_limit != null ? settings.rate_limit : 1;
    el("autoSync").checked = settings.auto_sync !== false;
    var action = settings.post_sync_action || "none";
    el("postSyncAction").value = action;
    el("moveTarget").value = settings.move_target || "";
    el("checkMissing").checked = settings.check_missing !== false;
    el("dailySignin").checked = settings.daily_signin === true;
    el("signinWindowStart").value = settings.signin_window_start || "08:00";
    el("signinWindowEnd").value = settings.signin_window_end || "23:00";
    el("moveTargetField").classList.toggle("hidden", action !== "move");
    el("logLevel").value = settings.log_level || "INFO";
    el("notifyOn").value = settings.notify_on || "always";
    el("notifyEmpty").checked = settings.notify_empty === true;
    el("intervalLabel").textContent = formatInterval(settings.interval || status.config.interval);
  }

  function updatePostSyncFields() {
    var action = el("postSyncAction").value;
    el("moveTargetField").classList.toggle("hidden", action !== "move");
    if (action !== "none") {
      el("checkMissing").checked = false;
    }
    state.settingsDirty = true;
    el("settingsStatus").textContent = "有未保存的修改";
  }

  function renderLastRun(sync) {
    var last = sync.last_run;
    if (!last) {
      el("lastRun").textContent = "尚未运行";
    } else {
      var stats = last.stats || {};
      var text = formatDate(last.at);
      if (stats.scanned != null) {
        text += " · 扫描 " + (stats.scanned || 0);
      }
      if (stats.processed != null && stats.processed !== stats.scanned) {
        text += " · 处理 " + stats.processed;
      }
      text += " · 上传 " + (stats.uploaded || 0) +
        " · 跳过 " + (stats.skipped || 0) + " · 失败 " + (stats.failed || 0);
      if (stats.permission_denied) {
        text += "（无权限 " + stats.permission_denied + "）";
      }
      if (stats.cloud_songs) {
        text += " · 云盘 " + stats.cloud_songs + " 首";
      }
      var filtered = stats.filtered || ((stats.scan_unsupported || 0) +
        (stats.scan_excluded || 0) + (stats.scan_unreadable || 0));
      if (filtered) {
        var details = [];
        if (stats.scan_unsupported) {
          var suffixes = stats.unsupported_extensions || {};
          var labels = Object.keys(suffixes).slice(0, 3).map(function (ext) {
            return (ext || "无扩展名") + " " + suffixes[ext];
          });
          details.push("扩展名 " + stats.scan_unsupported +
            (labels.length ? "（" + labels.join("、") + "）" : ""));
        }
        if (stats.scan_excluded) {
          details.push("排除 " + stats.scan_excluded);
        }
        if (stats.scan_unreadable) {
          details.push("不可读 " + stats.scan_unreadable);
        }
        if (stats.scan_missing_roots) {
          details.push("目录缺失 " + stats.scan_missing_roots);
        }
        text += " · 本地过滤 " + filtered +
          (details.length ? "（" + details.join(" / ") + "）" : "");
      }
      if (stats.deferred) {
        text += " · 待续 " + stats.deferred;
      }
      if (stats.stopped) {
        text += " · 已终止";
      } else if (stats.paused) {
        text += " · 已暂停";
      }
      if (stats.mode === "scan") {
        text = formatDate(last.at) + " · 仅扫描：本地候选 " + (stats.scanned || 0) +
          " · 待上传 " + (stats.to_upload || 0) +
          " · 云端独有 " + (stats.cloud_only || 0) +
          " · 已匹配 " + (stats.matched || 0) +
          " · 本地重复 " + (stats.duplicate_local || 0) +
          " · 失败 " + (stats.failed || 0) +
          (stats.cloud_songs ? " · 云盘 " + stats.cloud_songs + " 首" : "");
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
    el("scanOnlyButton").disabled = Boolean(sync.running);
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

  function renderPending(stats, scan) {
    var hint = el("statPendingHint");
    if (scan && typeof scan.to_upload === "number") {
      el("statPending").textContent = scan.to_upload;
      hint.textContent = scan.incomplete ? "上次仅扫描未完成" : "来自最近一次仅扫描";
      hint.title = scan.finished_at ? "仅扫描完成于 " + formatRunTime(scan.finished_at) : "";
      hint.classList.remove("hidden");
    } else {
      el("statPending").textContent = stats.pending || 0;
      hint.textContent = "";
      hint.title = "";
      hint.classList.add("hidden");
    }
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
    el("headerSignin").classList.toggle("hidden", !data.logged_in);
    if (!data.logged_in) {
      state.capacityLoaded = false;
      return;
    }
    if (!state.capacityLoaded) {
      loadCapacity(true);
    }
    el("nickname").textContent = (data.account && data.account.nickname) || "网易云用户";
    el("userId").textContent = (data.account && data.account.userId)
      ? "用户 ID " + data.account.userId
      : "账号信息将在下一次刷新时更新";
    var stats = data.stats || {};
    el("statUploaded").textContent = stats.uploaded || 0;
    renderPending(stats, data.pending_scan);
    var failed = stats.failed || 0;
    var permission = stats.permission || 0;
    el("statProblems").textContent = failed + permission;
    el("statProblemBreakdown").textContent = "失败 " + failed + " · 无权限 " + permission;
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
    renderRuns(data.runs || []);
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
    el("problemAllCount").textContent =
      (state.problemCounts.failed || 0) + (state.problemCounts.permission || 0);
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
      meta.appendChild(make("span", "problem-kind " + (item.kind || "failed"),
        item.kind === "permission" ? "无权限" : "失败"));
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

  function renderPermission(data) {
    var box = el("permissionResult");
    box.textContent = "";
    var ok = Boolean(data.ok);
    var summary = make("div", "permission-summary " + (ok ? "ok" : "warn"));
    summary.appendChild(make("strong", "", ok ? "权限正常" : "发现权限问题"));
    var parts = ["应用账号 " + (data.user || "-")];
    parts.push("检查 " + (data.files || 0) + " 个音频文件");
    if (data.dirs) {
      parts.push("子目录 " + data.dirs);
    }
    summary.appendChild(make("span", "", parts.join(" · ")));
    box.appendChild(summary);

    if (data.error) {
      box.appendChild(make("p", "permission-error", data.error));
    }

    var dirs = data.directories || [];
    if (dirs.length) {
      var dirBox = make("div", "permission-dirs");
      dirs.forEach(function (dir) {
        var bad = !dir.exists || !dir.readable || dir.unreadable_files || dir.unreadable_dirs;
        var row = make("div", "permission-dir" + (bad ? " warn" : ""));
        row.appendChild(make("span", "permission-dir-path", dir.path));
        var meta;
        if (!dir.exists) {
          meta = "目录不存在";
        } else if (!dir.readable) {
          meta = "应用账号无法进入";
        } else {
          var metaParts = ["音频 " + (dir.files || 0)];
          if (dir.unreadable_files) {
            metaParts.push("无权限 " + dir.unreadable_files);
          }
          if (dir.unreadable_dirs) {
            metaParts.push("无法进入目录 " + dir.unreadable_dirs);
          }
          if (dir.truncated) {
            metaParts.push("未检查完");
          }
          meta = metaParts.join(" · ");
        }
        row.appendChild(make("span", "permission-dir-meta", meta));
        dirBox.appendChild(row);
      });
      box.appendChild(dirBox);
    }

    var samples = data.samples || [];
    if (samples.length) {
      box.appendChild(make("p", "muted", "无权限示例（最多显示 20 个）："));
      var sampleBox = make("div", "permission-samples");
      samples.forEach(function (sample) {
        var row = make("div", "permission-sample");
        row.appendChild(make("div", "permission-sample-path",
          "[" + (sample.kind === "dir" ? "目录" : "文件") + "] " + sample.path));
        row.appendChild(make("div", "permission-sample-error", sample.error || "无法访问"));
        sampleBox.appendChild(row);
      });
      box.appendChild(sampleBox);
    }

    if (data.truncated) {
      box.appendChild(make("p", "muted", "检查达到上限，结果可能不完整；可以缩短音乐目录后重试。"));
    }

    var guidance = data.guidance || [];
    if (guidance.length) {
      var guideBox = make("div", "permission-guidance");
      guideBox.appendChild(make("p", "permission-guidance-title", "请在飞牛文件管理里授权："));
      var guideList = make("ul", "permission-guidance-list");
      guidance.forEach(function (line) {
        guideList.appendChild(make("li", "", line));
      });
      guideBox.appendChild(guideList);
      box.appendChild(guideBox);
    } else if (ok) {
      box.appendChild(make("p", "muted", "应用账号可以读取当前配置的音乐目录，无需处理。"));
    }

    if (data.checked_at) {
      box.appendChild(make("p", "muted", "检查时间 " + formatDate(data.checked_at)));
    }
  }

  function checkPermissions() {
    var button = el("permissionCheckButton");
    var box = el("permissionResult");
    button.disabled = true;
    box.textContent = "";
    box.appendChild(make("p", "muted", "正在检查音乐目录权限…"));
    api("/api/permissions/check").then(function (data) {
      renderPermission(data);
    }).catch(function (error) {
      box.textContent = "";
      box.appendChild(make("p", "permission-error", error.message));
    }).finally(function () {
      button.disabled = false;
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
      var files = (data.log_files || []).map(function (file) {
        var name = String(file.path || "").split(/[\\/]/).pop();
        return name + " " + (file.exists ? formatBytes(file.size) : "不存在");
      });
      var limit = data.log_limit_bytes ? " · 总上限 " + formatBytes(data.log_limit_bytes) : "";
      el("logInfo").textContent = (files.join(" · ") || "暂无日志文件") + limit + " · 单个文件 2 MB 自动轮转";
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
      config: type === "dingtalk" ? { keyword: "NCM" } : {}
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

  function syncNow(scanOnly) {
    api("/api/sync", {
      method: "POST",
      body: {
        scan_only: Boolean(scanOnly),
        force: el("forceRecheck").checked
      }
    }).then(function (data) {
      var message = scanOnly ? "仅扫描任务已启动" : "同步任务已启动";
      showToast(data.started ? message : "已有同步任务在运行");
      loadStatus(false);
    }).catch(function (error) {
      showToast(error.message, true);
    });
  }

  var RUN_ITEM_LABELS = {
    all: "全部",
    uploaded: "上传成功",
    linked: "补建云盘",
    skipped_remote: "云端已有",
    skipped_local: "本地重复",
    planned: "演练待上传",
    failed: "失败",
    permission: "无权限",
    ignored: "已忽略",
    missing: "本地缺失",
    moved: "已移动",
    deleted: "已删除本地",
    moved_after_sync: "已移动本地",
    post_action_failed: "同步后处理失败",
    deferred: "待续",
    to_upload: "待上传",
    cloud_only: "云端独有",
    duplicate_local: "本地重复",
    pending: "待处理"
  };

  function runKindLabel(kind) {
    return RUN_ITEM_LABELS[kind] || kind;
  }

  function openRunDetail(runId) {
    state.runDetail = {
      runId: runId,
      kind: "all",
      items: [],
      total: 0,
      offset: 0,
      limit: 200,
      loading: false,
      run: null,
      counts: {}
    };
    el("runDetailModal").classList.remove("hidden");
    document.body.classList.add("modal-open");
    el("runDetailSummary").textContent = "正在读取逐文件详情…";
    el("runDetailList").textContent = "";
    el("runDetailChips").textContent = "";
    el("runDetailMore").classList.add("hidden");
    loadRunDetail(true);
  }

  function closeRunDetail() {
    el("runDetailModal").classList.add("hidden");
    document.body.classList.remove("modal-open");
    state.runDetail.runId = null;
  }

  function runSummaryText(run, stats) {
    var mode = (run && run.mode) || (stats && stats.mode) || "sync";
    if (mode === "scan") {
      return "仅扫描 · 本地候选 " + (stats.scanned || 0) +
        " · 待上传 " + (stats.to_upload || 0) +
        " · 云端独有 " + (stats.cloud_only || 0) +
        " · 已匹配 " + (stats.matched || 0) +
        " · 本地重复 " + (stats.duplicate_local || 0) +
        " · 失败 " + (stats.failed || 0) +
        (stats.cloud_songs ? " · 云盘 " + stats.cloud_songs + " 首" : "");
    }
    return (mode === "dry_run" ? "演练" : "同步") +
      " · 扫描 " + (stats.scanned || 0) +
      " · 上传 " + (stats.uploaded || 0) +
      " · 跳过 " + (stats.skipped || 0) +
      " · 失败 " + (stats.failed || 0);
  }

  function loadRunDetail(reset) {
    var detail = state.runDetail;
    if (detail.loading || !detail.runId) {
      return;
    }
    if (reset) {
      detail.kind = "all";
      detail.offset = 0;
      detail.items = [];
      detail.total = 0;
    }
    detail.loading = true;
    var url = "/api/runs/" + encodeURIComponent(detail.runId) +
      "?kind=" + encodeURIComponent(detail.kind) +
      "&limit=" + detail.limit + "&offset=" + detail.offset;
    api(url).then(function (data) {
      detail.run = data.run || detail.run;
      detail.counts = data.counts || {};
      detail.items = reset ? (data.items || []) : detail.items.concat(data.items || []);
      detail.total = data.total || 0;
      detail.offset = (data.offset || 0) + (data.items || []).length;
      detail.loading = false;
      renderRunDetail();
    }).catch(function (error) {
      detail.loading = false;
      showToast(error.message, true);
      el("runDetailSummary").textContent = error.message;
    });
  }

  function renderRunDetail() {
    var detail = state.runDetail;
    var run = detail.run || {};
    var stats = run.stats || {};
    el("runDetailTitle").textContent = "运行详情";
    el("runDetailSummary").textContent = (run.started_at ? formatRunTime(run.started_at) + " · " : "") + runSummaryText(run, stats);
    var chips = el("runDetailChips");
    chips.textContent = "";
    var order = ["all", "uploaded", "linked", "skipped_remote", "skipped_local", "planned", "failed", "permission", "ignored", "missing", "moved", "deleted", "moved_after_sync", "post_action_failed", "deferred", "to_upload", "cloud_only", "duplicate_local"];
    var counts = detail.counts || {};
    var allCount = Object.keys(counts).reduce(function (sum, key) {
      return sum + (counts[key] || 0);
    }, 0);
    order.forEach(function (kind) {
      var count = kind === "all" ? allCount : (counts[kind] || 0);
      if (kind !== "all" && !count) {
        return;
      }
      var chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip" + (detail.kind === kind ? " active" : "");
      chip.textContent = runKindLabel(kind) + " " + count;
      chip.addEventListener("click", function () {
        if (detail.loading || detail.kind === kind) {
          return;
        }
        detail.kind = kind;
        detail.offset = 0;
        detail.items = [];
        detail.total = 0;
        renderRunDetail();
        loadRunDetail(false);
      });
      chips.appendChild(chip);
    });
    var list = el("runDetailList");
    list.textContent = "";
    if (!detail.items.length) {
      var empty = document.createElement("li");
      empty.className = "run-empty";
      empty.textContent = detail.loading ? "加载中…" : "该分类下没有记录";
      list.appendChild(empty);
    }
    detail.items.forEach(function (item) {
      var row = document.createElement("li");
      row.className = "run-item";
      var head = document.createElement("div");
      head.className = "run-item-head";
      var name = document.createElement("strong");
      name.textContent = item.name || (item.path ? basename(item.path) : "云端歌曲");
      var kind = document.createElement("span");
      kind.className = "run-item-kind kind-" + (item.kind || "unknown");
      kind.textContent = runKindLabel(item.kind);
      head.appendChild(name);
      head.appendChild(kind);
      row.appendChild(head);
      if (item.detail) {
        var detailText = document.createElement("p");
        detailText.className = "run-item-detail";
        detailText.textContent = item.detail;
        row.appendChild(detailText);
      }
      var meta = [];
      if (item.size) {
        meta.push(formatBytes(item.size));
      }
      if (item.path) {
        meta.push(item.path);
      }
      if (meta.length) {
        var metaText = document.createElement("p");
        metaText.className = "run-item-path";
        metaText.textContent = meta.join(" · ");
        row.appendChild(metaText);
      }
      list.appendChild(row);
    });
    var more = el("runDetailMore");
    if (detail.items.length < detail.total) {
      more.classList.remove("hidden");
      more.textContent = "加载更多（已显示 " + detail.items.length + " / " + detail.total + "）";
    } else {
      more.classList.add("hidden");
    }
  }


  var FILE_KIND_LABELS = {
    uploaded: "已上传",
    pending: "待处理",
    missing: "本地缺失",
    skipped: "跳过/重复"
  };

  function fileKindLabel(kind) {
    return FILE_KIND_LABELS[kind] || "文件";
  }

  function fileDetailText(item) {
    if (item.status === "uploaded") {
      return "上传成功";
    }
    if (item.status === "pending") {
      return "等待下次同步上传";
    }
    if (item.status === "missing") {
      return "本地文件已不存在";
    }
    if (item.status === "skipped_local") {
      return "与本地其他文件内容重复";
    }
    if (item.status === "skipped_remote") {
      return "云端已有相同文件";
    }
    return item.last_error || "";
  }

  function openFileDetail(kind) {
    state.fileDetail = {
      kind: kind,
      items: [],
      total: 0,
      loading: false,
      source: null,
      finishedAt: null,
      counts: {}
    };
    el("fileDetailTitle").textContent = fileKindLabel(kind) + "明细";
    el("fileDetailSummary").textContent = "正在读取文件明细…";
    el("fileDetailList").textContent = "";
    el("fileDetailModal").classList.remove("hidden");
    document.body.classList.add("modal-open");
    loadFileDetail();
  }

  function closeFileDetail() {
    el("fileDetailModal").classList.add("hidden");
    document.body.classList.remove("modal-open");
    state.fileDetail.kind = null;
  }

  function loadFileDetail() {
    var detail = state.fileDetail;
    if (detail.loading || !detail.kind) {
      return;
    }
    detail.loading = true;
    api("/api/files?kind=" + encodeURIComponent(detail.kind) + "&limit=500").then(function (data) {
      detail.loading = false;
      detail.items = data.items || [];
      detail.total = data.total || 0;
      detail.source = data.source || null;
      detail.finishedAt = data.finished_at || null;
      detail.counts = data.counts || {};
      renderFileDetail();
    }).catch(function (error) {
      detail.loading = false;
      el("fileDetailSummary").textContent = error.message;
      showToast(error.message, true);
    });
  }

  function renderFileDetail() {
    var detail = state.fileDetail;
    var summary = "共 " + detail.total + " 个";
    if (detail.source === "scan" && detail.finishedAt) {
      summary += " · 来自 " + formatRunTime(detail.finishedAt) + " 的仅扫描";
    }
    if (detail.items.length < detail.total) {
      summary += " · 显示前 " + detail.items.length + " 个";
    }
    el("fileDetailSummary").textContent = summary;
    var list = el("fileDetailList");
    list.textContent = "";
    if (!detail.items.length) {
      var empty = document.createElement("li");
      empty.className = "run-empty";
      empty.textContent = "该分类下没有文件";
      list.appendChild(empty);
      return;
    }
    detail.items.forEach(function (item) {
      var row = document.createElement("li");
      row.className = "run-item";
      var head = document.createElement("div");
      head.className = "run-item-head";
      var name = document.createElement("strong");
      name.textContent = item.name || (item.path ? basename(item.path) : "未知文件");
      var kind = document.createElement("span");
      kind.className = "run-item-kind kind-" + (item.kind || "pending");
      kind.textContent = runKindLabel(item.kind);
      head.appendChild(name);
      head.appendChild(kind);
      row.appendChild(head);
      var detailText = fileDetailText(item);
      if (detailText) {
        var note = document.createElement("p");
        note.className = "run-item-detail";
        note.textContent = detailText;
        row.appendChild(note);
      }
      var meta = [];
      if (item.size) {
        meta.push(formatBytes(item.size));
      }
      if (item.path) {
        meta.push(item.path);
      }
      if (item.song_id) {
        meta.push("歌曲 ID " + item.song_id);
      }
      if (meta.length) {
        var metaText = document.createElement("p");
        metaText.className = "run-item-path";
        metaText.textContent = meta.join(" · ");
        row.appendChild(metaText);
      }
      list.appendChild(row);
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
      auto_sync: el("autoSync").checked,
      post_sync_action: el("postSyncAction").value,
      move_target: el("moveTarget").value.trim(),
      check_missing: el("checkMissing").checked,
      daily_signin: el("dailySignin").checked,
      signin_window_start: el("signinWindowStart").value || "08:00",
      signin_window_end: el("signinWindowEnd").value || "23:00",
      log_level: el("logLevel").value,
      notify_on: el("notifyOn").value,
      notify_empty: el("notifyEmpty").checked,
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

  function signinNow() {
    if (state.signinBusy) {
      return;
    }
    state.signinBusy = true;
    var button = el("headerSignin");
    button.disabled = true;
    button.textContent = "签到中…";
    function finish(signin, error) {
      state.signinBusy = false;
      if (signin) {
        if (state.status) {
          state.status.signin = signin;
        }
        renderSignin(signin);
      }
      if (error) {
        showToast(error.message, true);
      }
      loadStatus(false);
    }
    api("/api/signin", { method: "POST" }).then(function (result) {
      var signin = result.signin || {};
      showToast(signin.message || "签到完成");
      finish(signin, null);
    }).catch(function (error) {
      finish(null, error);
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
      loadCapacity(true);
    }).catch(function (error) {
      showToast(error.message, true);
    });
  }

  function bindFileEntry(node) {
    node.addEventListener("click", function () {
      openFileDetail(node.getAttribute("data-file-kind"));
    });
    node.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        node.click();
      }
    });
  }

  function bindProblemEntry(node) {
    node.addEventListener("click", function () {
      state.problemKind = node.getAttribute("data-problem-kind") || "all";
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
    el("scanOnlyButton").addEventListener("click", function () { syncNow(true); });
    el("runDetailClose").addEventListener("click", closeRunDetail);
    el("runDetailMore").addEventListener("click", function () { loadRunDetail(false); });
    el("runDetailModal").addEventListener("click", function (event) {
      if (event.target === el("runDetailModal")) {
        closeRunDetail();
      }
    });
    el("fileDetailClose").addEventListener("click", closeFileDetail);
    el("fileDetailRefresh").addEventListener("click", loadFileDetail);
    el("fileDetailModal").addEventListener("click", function (event) {
      if (event.target === el("fileDetailModal")) {
        closeFileDetail();
      }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !el("fileDetailModal").classList.contains("hidden")) {
        closeFileDetail();
        return;
      }
      if (event.key === "Escape" && !el("runDetailModal").classList.contains("hidden")) {
        closeRunDetail();
      }
    });
    el("pauseButton").addEventListener("click", pauseSync);
    el("stopButton").addEventListener("click", stopSync);
    el("resumeButton").addEventListener("click", resumeSync);
    el("refreshButton").addEventListener("click", function () { loadStatus(false); });
    el("logoutButton").addEventListener("click", logout);
    el("refreshAccount").addEventListener("click", refreshAccount);
    el("refreshLogs").addEventListener("click", loadLogs);
    el("settingsForm").addEventListener("submit", saveSettings);
    el("postSyncAction").addEventListener("change", updatePostSyncFields);
    el("headerSignin").addEventListener("click", signinNow);
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
    el("notifyEmpty").addEventListener("change", function () {
      state.notifyDirty = true;
      el("notifyStatus").textContent = "有未保存的修改";
    });
    el("refreshProblems").addEventListener("click", function () { loadProblems(state.problemKind, false); });
    el("permissionCheckButton").addEventListener("click", checkPermissions);
    el("retryProblems").addEventListener("click", retryProblems);
    el("checkUpdateButton").addEventListener("click", function () { loadUpdate(true); });
    Array.prototype.forEach.call(document.querySelectorAll(".nav-item"), function (item) {
      item.addEventListener("click", function () { showPage(item.getAttribute("data-page")); });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".problem-switch .chip"), function (item) {
      item.addEventListener("click", function () { loadProblems(item.getAttribute("data-problem-kind"), false); });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".stat.clickable"), function (item) {
      if (item.getAttribute("data-file-kind")) {
        bindFileEntry(item);
      } else {
        bindProblemEntry(item);
      }
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