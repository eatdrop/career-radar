/* 智职引擎 Web client. Dynamic content is rendered with DOM APIs only. */

"use strict";

const API_BASE = "/api/v1";
const MAX_RESUME_FILE_BYTES = 4 * 1024 * 1024;
const RADAR_RUN_STORAGE_KEY = "career-radar.active-run.v1";
const SAVED_JOBS_STORAGE_KEY = "career-radar.saved-jobs.v1";
const RADAR_POLL_TIMEOUT_MS = 3 * 60 * 1000;
const RADAR_TERMINAL_STATUSES = new Set(["succeeded", "failed"]);
const RADAR_ACTIVE_STATUSES = new Set(["queued", "running", "retrying"]);
const MOBILE_NAV_QUERY = window.matchMedia("(max-width: 900px)");
const REDUCED_MOTION_QUERY = window.matchMedia("(prefers-reduced-motion: reduce)");
const PANEL_META = {
  radar: ["AUTOMATED JOB HUNTING", "今日求职雷达"],
  preferences: ["SET ONCE, RUN DAILY", "求职偏好"],
  resume: ["YOUR EVIDENCE BASE", "简历画像"],
  jobs: ["JOB INBOX", "岗位池"],
  workshop: ["TAILORED, NOT FABRICATED", "简历工坊"],
  applications: ["APPLICATION CRM", "投递看板"],
};
const STATUS_LABELS = {
  submitted: "已投递",
  viewed: "被查看",
  interviewing: "面试中",
  offered: "已录用",
  rejected: "已拒绝",
};
const state = {
  health: null,
  dashboard: null,
  radar: null,
  radarItems: [],
  savedJobKeys: new Set(),
  jobFeedback: new Map(),
  jobPool: [],
  applications: [],
  resumeMode: "text",
  resumeFile: null,
  resumeResult: null,
  radarRunning: false,
  radarRunStatus: null,
  radarActiveButtonId: null,
  currentPanel: "radar",
};

const byId = (id) => document.getElementById(id);

function make(tag, options = {}) {
  const node = document.createElement(tag);
  if (options.className) node.className = options.className;
  if (options.text !== undefined) node.textContent = String(options.text);
  if (options.title) node.title = options.title;
  if (options.type) node.type = options.type;
  if (options.href) node.href = options.href;
  if (options.target) node.target = options.target;
  if (options.rel) node.rel = options.rel;
  if (options.hidden) node.hidden = true;
  return node;
}

function append(parent, ...children) {
  for (const child of children.flat()) {
    if (child !== null && child !== undefined) parent.append(child);
  }
  return parent;
}

function readSavedJobKeys() {
  try {
    const value = JSON.parse(window.localStorage.getItem(SAVED_JOBS_STORAGE_KEY) || "[]");
    return new Set(Array.isArray(value) ? value.filter((item) => typeof item === "string") : []);
  } catch {
    return new Set();
  }
}

function storeSavedJobKeys() {
  try {
    window.localStorage.setItem(
      SAVED_JOBS_STORAGE_KEY,
      JSON.stringify([...state.savedJobKeys].slice(-500)),
    );
    return true;
  } catch {
    return false;
  }
}

function jobIdentity(job) {
  if (/^[a-f0-9]{32}$/.test(job.job_key || "")) return job.job_key;
  const canonicalUrl = /^https?:\/\//i.test(job.url || "") ? job.url.trim() : "";
  return canonicalUrl || [job.company, job.title, job.location]
    .map((value) => String(value || "").trim().toLocaleLowerCase("zh-CN"))
    .join("|");
}

function searchableJobText(job) {
  return [
    job.title,
    job.company,
    job.location,
    job.salary,
    job.source,
    job.why_fit,
    ...(job.matched_keywords || []),
    ...(job.missing_keywords || []),
  ].join(" ").toLocaleLowerCase("zh-CN");
}

function setSaveButtonState(button, saved) {
  button.setAttribute("aria-pressed", String(saved));
  button.setAttribute("aria-label", saved ? "取消收藏这个岗位" : "收藏这个岗位");
  button.title = saved ? "取消收藏" : "收藏岗位";
  button.querySelector("[aria-hidden]")?.replaceChildren(saved ? "★" : "☆");
}

function syncSavedCount() {
  byId("radarSavedCount").textContent = String(state.savedJobKeys.size);
}

async function setJobFeedback(job, action, reason = "") {
  const jobKey = jobIdentity(job);
  if (!/^[a-f0-9]{32}$/.test(jobKey)) {
    throw new Error("这个岗位缺少可持久化标识，请重新运行雷达后再试。");
  }
  const result = await apiRequest("/jobs/feedback", {
    method: "POST",
    body: { job_key: jobKey, action, reason, job },
  });
  if (result.action) state.jobFeedback.set(jobKey, result.action);
  else state.jobFeedback.delete(jobKey);
  return result;
}

function freshnessLabel(job) {
  if (job.user_action === "applied") return ["已建档", "is-applied"];
  if (job.freshness === "fresh") return ["7 天内发布", "is-fresh"];
  if (job.freshness === "aging") return ["发布时间较早", "is-aging"];
  if (job.freshness === "expired") return ["可能已截止", "is-expired"];
  return [job.posted_label || "发布时间待核验", "is-unknown"];
}

function resetRadarFilters() {
  byId("radarResultQuery").value = "";
  byId("radarResultSort").value = "score";
  byId("radarSavedOnly").setAttribute("aria-pressed", "false");
  renderRadarItems();
  byId("radarResultQuery").focus();
}

function renderRadarItems() {
  const container = byId("radarResults");
  const filterEmpty = byId("radarFilterEmpty");
  container.replaceChildren();
  const query = byId("radarResultQuery").value.trim().toLocaleLowerCase("zh-CN");
  const savedOnly = byId("radarSavedOnly").getAttribute("aria-pressed") === "true";
  const sort = byId("radarResultSort").value;
  const items = state.radarItems
    .filter((job) => !query || searchableJobText(job).includes(query))
    .filter((job) => !savedOnly || state.savedJobKeys.has(jobIdentity(job)))
    .sort((left, right) => {
      if (sort === "company") {
        return String(left.company || "").localeCompare(String(right.company || ""), "zh-CN");
      }
      if (sort === "salary") {
        const leftHasSalary = Boolean(left.salary && !/面议|未注明/.test(left.salary));
        const rightHasSalary = Boolean(right.salary && !/面议|未注明/.test(right.salary));
        if (leftHasSalary !== rightHasSalary) return rightHasSalary - leftHasSalary;
      }
      return Number(right.score || 0) - Number(left.score || 0);
    });
  items.forEach((job, index) => container.append(createRecommendationCard(job, index + 1)));
  filterEmpty.hidden = state.radarItems.length === 0 || items.length > 0;
  byId("radarVisibleCount").textContent = `${items.length} 个岗位`;
  const controlsEnabled = state.radarItems.length > 0;
  ["radarResultQuery", "radarResultSort", "radarSavedOnly", "radarFilterReset"].forEach((id) => {
    byId(id).disabled = !controlsEnabled;
  });
  syncSavedCount();
}

function showToast(message, type = "info", duration = 4500) {
  const toast = make("div", {
    className: `toast${type === "error" ? " is-error" : type === "warning" ? " is-warning" : ""}`,
  });
  toast.setAttribute("role", type === "error" ? "alert" : "status");
  const text = make("span", { text: message });
  const close = make("button", { type: "button", text: "×", title: "关闭通知" });
  close.setAttribute("aria-label", "关闭通知");
  close.addEventListener("click", () => toast.remove());
  append(toast, text, close);
  byId("toastRegion").append(toast);
  window.setTimeout(() => toast.remove(), duration);
}

function requestId() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  return `web-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function apiRequest(path, options = {}) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), options.timeout || 30000);
  const init = {
    method: options.method || "GET",
    headers: {
      Accept: "application/json",
      "X-Request-ID": requestId(),
    },
    signal: controller.signal,
  };
  if (options.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  if (options.headers) {
    Object.keys(options.headers).forEach((name) => {
      init.headers[name] = options.headers[name];
    });
  }
  try {
    const response = await fetch(`${API_BASE}${path}`, init);
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) {
      throw new Error(`服务返回了非 JSON 响应（HTTP ${response.status}）`);
    }
    const payload = await response.json();
    if (!response.ok || !payload.success) {
      const error = new Error(payload?.error?.message || `请求失败（HTTP ${response.status}）`);
      error.code = payload?.error?.code || "request_failed";
      error.status = response.status;
      error.requestId = payload?.meta?.request_id || "";
      error.details = payload?.error?.details || {};
      const retryAfter = Number(response.headers.get("Retry-After"));
      error.retryAfter = Number.isFinite(retryAfter)
        ? Math.max(1, Math.min(60, retryAfter))
        : 0;
      throw error;
    }
    return payload.data;
  } catch (error) {
    if (error.name === "AbortError") {
      const timeoutError = new Error("请求超时。外部岗位源可能正在验证，请稍后重试。");
      timeoutError.code = "timeout";
      throw timeoutError;
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

function setButtonLoading(button, loading) {
  if (!button) return;
  button.classList.toggle("is-loading", loading);
  button.disabled = loading;
  if (loading) button.setAttribute("aria-busy", "true");
  else button.removeAttribute("aria-busy");
}

function errorMessage(error) {
  const suffix = error.requestId ? `（请求 ${error.requestId.slice(0, 8)}）` : "";
  return `${error.message || "操作失败"}${suffix}`;
}

function warnIfRefreshFailed(results) {
  if (results.some((result) => result.status === "rejected")) {
    showToast("操作已成功，但部分概览数据刷新失败；重新加载页面即可同步。", "warning", 6500);
  }
}

function switchPanel(name, updateHash = true) {
  if (!PANEL_META[name]) name = "radar";
  state.currentPanel = name;
  document.querySelectorAll("[data-panel-content]").forEach((panel) => {
    panel.classList.toggle("is-active", panel.dataset.panelContent === name);
  });
  document.querySelectorAll("[data-panel]").forEach((item) => {
    const active = item.dataset.panel === name;
    item.classList.toggle("is-active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  const [kicker, title] = PANEL_META[name];
  byId("pageKicker").textContent = kicker;
  byId("pageTitle").textContent = title;
  document.title = `${title} · 智职引擎`;
  if (updateHash) history.replaceState(null, "", `#${name}`);
  closeMobileMenu({ restoreFocus: true });
  window.scrollTo({ top: 0, behavior: REDUCED_MOTION_QUERY.matches ? "auto" : "smooth" });
}

function openMobileMenu() {
  if (!MOBILE_NAV_QUERY.matches) return;
  const sidebar = byId("sidebar");
  sidebar.inert = false;
  sidebar.classList.add("is-open");
  byId("mobileOverlay").hidden = false;
  const menuButton = byId("mobileMenuButton");
  menuButton.setAttribute("aria-expanded", "true");
  menuButton.setAttribute("aria-label", "关闭导航");
  document.body.classList.add("menu-open");
  window.setTimeout(() => {
    (sidebar.querySelector(".nav-item.is-active") || sidebar.querySelector(".nav-item"))?.focus();
  }, 0);
}

function closeMobileMenu({ restoreFocus = false } = {}) {
  const sidebar = byId("sidebar");
  const wasOpen = sidebar.classList.contains("is-open");
  sidebar.classList.remove("is-open");
  sidebar.inert = MOBILE_NAV_QUERY.matches;
  byId("mobileOverlay").hidden = true;
  const menuButton = byId("mobileMenuButton");
  menuButton.setAttribute("aria-expanded", "false");
  menuButton.setAttribute("aria-label", "打开导航");
  document.body.classList.remove("menu-open");
  if (restoreFocus && wasOpen && MOBILE_NAV_QUERY.matches) menuButton.focus();
}

function syncMobileNavigation() {
  if (MOBILE_NAV_QUERY.matches) {
    closeMobileMenu();
    return;
  }
  const sidebar = byId("sidebar");
  sidebar.inert = false;
  sidebar.classList.remove("is-open");
  byId("mobileOverlay").hidden = true;
  byId("mobileMenuButton").setAttribute("aria-expanded", "false");
  byId("mobileMenuButton").setAttribute("aria-label", "打开导航");
  document.body.classList.remove("menu-open");
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 10);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function localDateInputValue(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function datetimeLocalInputValue(value) {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  const year = parsed.getFullYear();
  const month = String(parsed.getMonth() + 1).padStart(2, "0");
  const day = String(parsed.getDate()).padStart(2, "0");
  const hour = String(parsed.getHours()).padStart(2, "0");
  const minute = String(parsed.getMinutes()).padStart(2, "0");
  return `${year}-${month}-${day}T${hour}:${minute}`;
}

function renderOnboarding(onboarding = {}) {
  const steps = [
    ["onboardingResume", Boolean(onboarding.resume_ready)],
    ["onboardingPreferences", Boolean(onboarding.preferences_ready)],
    ["onboardingJobs", Boolean(onboarding.jobs_ready)],
  ];
  const completed = steps.filter(([, ready]) => ready).length;
  steps.forEach(([id, ready]) => {
    const button = byId(id);
    button.classList.toggle("is-complete", ready);
    button.querySelector("span").textContent = ready ? "✓" : button.id === "onboardingResume" ? "1" : button.id === "onboardingPreferences" ? "2" : "3";
  });
  byId("onboardingProgress").textContent = completed === 3
    ? "准备完成。现在运行雷达，获得第一批推荐。"
    : `已完成 ${completed}/3 步；系统会保存进度。`;
  byId("onboardingCard").classList.toggle("is-complete", completed === 3);
}

function splitList(value) {
  return String(value || "")
    .split(/[,，\n]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function renderCapabilityBadge(id, capability) {
  const node = byId(id);
  if (!node) return;
  const enabled = Boolean(capability?.enabled);
  node.textContent = enabled ? "已就绪" : "未配置";
  node.classList.toggle("is-ready", enabled);
  node.classList.toggle("is-unavailable", !enabled);
  node.title = capability?.reason || "";
}

function syncCapabilityControl(selector, capability) {
  const input = document.querySelector(selector);
  if (!input) return;
  const enabled = Boolean(capability?.enabled);
  input.disabled = !enabled;
  if (!enabled) input.checked = false;
  input.title = enabled ? "" : capability?.reason || "服务端能力未配置";
}

function renderHealth(health) {
  state.health = health;
  const up = health?.status === "ok";
  const dot = byId("sidebarStatusDot");
  dot.classList.toggle("is-up", up);
  dot.classList.toggle("is-down", !up);
  byId("sidebarStatusText").textContent = up ? "服务正常" : "服务降级";
  byId("sidebarStatusDetail").textContent = up
    ? `数据库 ${health.database === "ok" ? "已连接" : "异常"} · 核心能力可用`
    : "部分功能不可用，请查看服务日志";
  renderCapabilityBadge("serpapiSourceState", health.capabilities?.job_aggregator);
  const crawler = health.capabilities?.crawler;
  renderCapabilityBadge("crawlerSourceState", crawler);
  syncCapabilityControl(
    'input[name="radarSource"][value="serpapi"]',
    health.capabilities?.job_aggregator,
  );
  syncCapabilityControl(
    'input[name="radarSource"][value="51job"]',
    crawler,
  );
  byId("crawlCapabilityText").textContent = crawler?.enabled
    ? "实时采集组件已就绪；也可主动勾选演示模式做无网络验收。"
    : "实时采集组件未就绪，已切换为演示模式；演示岗位会被明确标注。";
  if (!crawler?.enabled) byId("crawlUseDemo").checked = true;
  const email = health.capabilities?.email;
  byId("emailCapabilityText").textContent = email?.enabled ? "SMTP 已就绪" : "需要服务端 SMTP 配置";
  syncCapabilityControl("#emailEnabled", email);

  const ai = health.capabilities?.ai;
  const aiEnabled = Boolean(ai?.enabled);
  byId("aiConsentWrap").hidden = !aiEnabled;
  byId("aiProcessingConsent").checked = false;
  byId("enhanceModeNotice").textContent = aiEnabled
    ? "AI 已配置：勾选授权后，本次简历与 JD 会发送至已配置模型；输出仍需人工核验。"
    : "AI 未配置：本次将使用本地规则处理，不会把简历发送给外部模型。";

  const loadedSettings = state.radar?.settings || state.dashboard?.radar?.settings;
  if (loadedSettings) renderSettings(loadedSettings);
}

function renderHealthFailure() {
  const dot = byId("sidebarStatusDot");
  dot.classList.remove("is-up");
  dot.classList.add("is-down");
  byId("sidebarStatusText").textContent = "服务未连接";
  byId("sidebarStatusDetail").textContent = "请确认 python web_server.py 正在运行";
  byId("crawlCapabilityText").textContent = "无法确认实时采集能力；服务恢复前请勿提交采集任务。";
  byId("enhanceModeNotice").textContent = "无法确认 AI 能力；如继续操作，将要求显式数据处理授权。";
}

function renderSettings(settings) {
  if (!settings) return;
  byId("preferenceKeywords").value = (settings.keywords || []).join(", ");
  byId("excludeKeywords").value = (settings.exclude_keywords || []).join(", ");
  byId("newGradOnly").checked = Boolean(settings.new_grad_only);
  byId("radarEnabled").checked = Boolean(settings.enabled);
  byId("scheduleTime").value = settings.schedule_time || "21:00";
  byId("minScore").value = String(settings.min_score ?? 55);
  byId("minScoreValue").textContent = String(settings.min_score ?? 55);
  byId("maxResults").value = String(settings.max_results ?? 20);
  const emailEnabled = !byId("emailEnabled").disabled && Boolean(settings.email_enabled);
  byId("emailEnabled").checked = emailEnabled;
  byId("emailTo").value = settings.email_to || "";
  const cities = new Set(settings.cities || []);
  document.querySelectorAll("#cityCheckboxes input").forEach((input) => {
    input.checked = cities.has(input.value);
  });
  const sources = new Set(settings.sources || []);
  document.querySelectorAll('input[name="radarSource"]').forEach((input) => {
    input.checked = !input.disabled && sources.has(input.value);
  });
  byId("preferenceReadiness").textContent = settings.enabled
    ? `每天 ${settings.schedule_time} 自动运行`
    : "自动运行未开启";
  byId("nextRunText").textContent = settings.enabled
    ? `计划：每天 ${settings.schedule_time} 运行 · ${emailEnabled ? "邮件摘要已开启" : "结果保存在站内"}`
    : "定时任务尚未开启，可随时手动运行";
}

function renderRadarOverview(overview) {
  state.radar = overview;
  renderSettings(overview.settings);
  renderRadarDigest(overview.latest);
  renderRunHistory(overview.runs || []);
}

function renderRadarDigest(digest) {
  const empty = byId("radarEmpty");
  const container = byId("radarResults");
  const panel = byId("panel-radar");
  container.replaceChildren();
  if (!digest || !digest.summary) {
    panel.classList.remove("has-results");
    state.radarItems = [];
    empty.hidden = false;
    byId("radarShortlisted").textContent = "0";
    byId("radarCollected").textContent = "0";
    byId("radarFiltered").textContent = "0";
    byId("radarRejected").textContent = "0";
    byId("radarRunState").textContent = "待运行";
    renderFilterLog([]);
    renderRadarItems();
    return;
  }

  const summary = digest.summary;
  byId("radarShortlisted").textContent = String(summary.shortlisted || 0);
  byId("radarCollected").textContent = String(summary.collected || 0);
  byId("radarFiltered").textContent = String(summary.after_filter || 0);
  byId("radarRejected").textContent = String(summary.filtered_out || 0);
  byId("radarRunState").textContent = digest.trigger === "scheduled" ? "自动完成" : "已完成";
  byId("radarResultCaption").textContent =
    `${formatDate(digest.generated_at)} · 从 ${summary.collected || 0} 个岗位中筛出 ${summary.shortlisted || 0} 个，按简历证据排序。`;
  renderFilterLog(digest.filter_breakdown || []);

  const items = Array.isArray(digest.items)
    ? digest.items.filter((job) => !["dismissed", "expired"].includes(job.user_action))
    : [];
  items.forEach((job) => {
    if (job.user_action) state.jobFeedback.set(jobIdentity(job), job.user_action);
    if (job.user_action === "saved") state.savedJobKeys.add(jobIdentity(job));
  });
  state.radarItems = items;
  panel.classList.toggle("has-results", items.length > 0);
  empty.hidden = items.length > 0;
  renderRadarItems();
  if (items.length === 0) {
    empty.hidden = false;
    empty.querySelector("h3").textContent = "今天没有岗位通过你的门槛";
    empty.querySelector("p").textContent = "这是有效结果，不是系统故障。可以检查最低匹配分、排除词或岗位来源。";
  }
}

function createRecommendationCard(job, rank) {
  const card = make("article", { className: "recommendation-card" });
  card.dataset.jobKey = jobIdentity(job);
  const score = make("div", { className: "match-score" });
  append(
    score,
    make("small", { className: "recommendation-rank", text: `#${rank}` }),
    make("strong", { text: job.score ?? 0 }),
    make("span", { text: "匹配度" }),
  );

  const main = make("div", { className: "recommendation-main" });
  const heading = make("div", { className: "job-title-row" });
  const titleGroup = make("div");
  append(
    titleGroup,
    make("h3", { text: job.title || "未命名岗位" }),
    make("strong", { className: "job-salary", text: job.salary || "薪资面议" }),
  );
  const save = make("button", { className: "save-job-button", type: "button" });
  save.append(make("span", { text: "☆" }));
  save.firstElementChild.setAttribute("aria-hidden", "true");
  setSaveButtonState(save, state.savedJobKeys.has(jobIdentity(job)));
  save.addEventListener("click", async () => {
    const key = jobIdentity(job);
    const willSave = !state.savedJobKeys.has(key);
    if (willSave) state.savedJobKeys.add(key);
    else state.savedJobKeys.delete(key);
    const stored = storeSavedJobKeys();
    setSaveButtonState(save, willSave);
    syncSavedCount();
    if (byId("radarSavedOnly").getAttribute("aria-pressed") === "true") {
      renderRadarItems();
    }
    try {
      await setJobFeedback(job, willSave ? "saved" : "clear");
      showToast(willSave ? "已收藏岗位，刷新后仍会保留。" : "已取消收藏。");
    } catch (error) {
      showToast(
        stored ? `${errorMessage(error)}；已暂存于当前浏览器。` : errorMessage(error),
        "warning",
      );
    }
  });
  append(heading, titleGroup, save);
  main.append(heading);
  const meta = make("div", { className: "job-meta" });
  [
    job.company || "未知公司",
    job.location || "地点未注明",
    job.source || "未知来源",
  ].forEach((value) => meta.append(make("span", { text: value })));
  const [freshnessText, freshnessClass] = freshnessLabel(job);
  meta.append(make("span", {
    className: `freshness-badge ${freshnessClass}`,
    text: freshnessText,
  }));
  main.append(meta);
  main.append(make("p", { className: "fit-reason", text: job.why_fit || "请结合岗位详情进一步核验。" }));
  const chips = make("div", { className: "chip-cloud" });
  (job.matched_keywords || []).slice(0, 7).forEach((keyword) => {
    chips.append(make("span", { className: "chip", text: `✓ ${keyword}` }));
  });
  (job.missing_keywords || []).slice(0, 3).forEach((keyword) => {
    chips.append(make("span", { className: "chip is-missing", text: `待核 ${keyword}` }));
  });
  main.append(chips);
  if (job.resume_evidence?.length) {
    const proof = make("details", { className: "evidence-proof" });
    proof.append(make("summary", { text: "查看这条匹配引用的简历证据" }));
    job.resume_evidence.slice(0, 2).forEach((item) => {
      proof.append(make("p", { text: item.text }));
    });
    main.append(proof);
  }

  const side = make("div", { className: "recommendation-side" });
  side.append(make("h4", { text: "投递前建议" }));
  const tips = make("ul");
  (job.resume_tips || []).forEach((tip) => tips.append(make("li", { text: tip })));
  if (!tips.childElementCount) tips.append(make("li", { text: "核对岗位发布时间与官方申请入口。" }));
  side.append(tips);
  const actions = make("div", { className: "card-actions" });
  if (job.url && /^https?:\/\//i.test(job.url)) {
    const link = make("a", {
      className: "button button-primary button-compact",
      text: "查看并投递 ↗",
      href: job.url,
      target: "_blank",
      rel: "noopener noreferrer",
    });
    actions.append(link);
  }
  const track = make("button", {
    className: "button button-secondary button-compact",
    type: "button",
    text: "记入投递看板",
  });
  track.addEventListener("click", () => openApplicationDialog(null, job));
  actions.append(track);
  const dismiss = make("button", {
    className: "text-button",
    type: "button",
    text: "不感兴趣",
  });
  dismiss.addEventListener("click", async () => {
    try {
      await setJobFeedback(job, "dismissed", "用户主动忽略");
      state.radarItems = state.radarItems.filter((item) => jobIdentity(item) !== jobIdentity(job));
      renderRadarItems();
      showToast("已忽略，后续雷达不会重复推荐这个岗位。");
    } catch (error) {
      showToast(errorMessage(error), "error");
    }
  });
  const expired = make("button", {
    className: "text-button danger",
    type: "button",
    text: "岗位失效",
  });
  expired.addEventListener("click", async () => {
    try {
      await setJobFeedback(job, "expired", "用户标记岗位已失效");
      state.radarItems = state.radarItems.filter((item) => jobIdentity(item) !== jobIdentity(job));
      renderRadarItems();
      showToast("已标记失效，不再参与后续推荐。");
    } catch (error) {
      showToast(errorMessage(error), "error");
    }
  });
  append(actions, dismiss, expired);
  side.append(actions);
  append(card, score, main, side);
  return card;
}

function renderFilterLog(items) {
  const container = byId("radarFilterLog");
  container.replaceChildren();
  if (!items.length) {
    container.append(make("p", { className: "muted", text: "运行后展示排除原因，方便你校准规则。" }));
    return;
  }
  items.forEach((item) => {
    const row = make("div", { className: "filter-log-item" });
    append(row, make("strong", { text: item.reason }), make("span", { text: `${item.count} 个岗位` }));
    container.append(row);
  });
}

function renderRunHistory(runs) {
  const container = byId("radarRunHistory");
  container.replaceChildren();
  if (!runs.length) {
    container.append(make("p", { className: "muted", text: "暂无运行记录。" }));
    return;
  }
  runs.slice(0, 6).forEach((run) => {
    const row = make("div", { className: "run-history-item" });
    let label = `失败：${run.error_message || "未知原因"}`;
    if (run.status === "completed") label = `${run.shortlisted_count} 个推荐`;
    else if (run.status === "running") label = "正在运行";
    append(
      row,
      make("strong", { text: label }),
      make("span", { text: `${run.trigger_type === "scheduled" ? "自动" : "手动"} · ${formatDate(run.started_at)}` }),
    );
    container.append(row);
  });
}

function createRadarIdempotencyKey() {
  let randomPart = "";
  if (window.crypto && typeof window.crypto.getRandomValues === "function") {
    const values = new Uint32Array(4);
    window.crypto.getRandomValues(values);
    randomPart = Array.from(values, (value) => value.toString(36)).join("");
  } else {
    randomPart = `${Math.random().toString(36).slice(2)}${Math.random().toString(36).slice(2)}`;
  }
  return `radar-${Date.now().toString(36)}-${randomPart}`.slice(0, 128);
}

function isValidIdempotencyKey(value) {
  return typeof value === "string" && value.length >= 8 && value.length <= 128
    && /^[A-Za-z0-9._:~/-]+$/.test(value);
}

function readStoredRadarRun() {
  try {
    const value = window.sessionStorage.getItem(RADAR_RUN_STORAGE_KEY);
    if (!value) return null;
    const record = JSON.parse(value);
    if (!record || !isValidIdempotencyKey(record.idempotency_key)) {
      window.sessionStorage.removeItem(RADAR_RUN_STORAGE_KEY);
      return null;
    }
    if (!record.body || typeof record.body !== "object" || Array.isArray(record.body)) record.body = {};
    record.submitted_at = Number(record.submitted_at) || Date.now();
    record.deadline_at = Number(record.deadline_at) || record.submitted_at + RADAR_POLL_TIMEOUT_MS;
    return record;
  } catch {
    return null;
  }
}

function storeRadarRun(record) {
  try {
    window.sessionStorage.setItem(RADAR_RUN_STORAGE_KEY, JSON.stringify(record));
    return true;
  } catch {
    return false;
  }
}

function clearStoredRadarRun(record = null) {
  try {
    if (record) {
      const current = readStoredRadarRun();
      if (current && current.idempotency_key !== record.idempotency_key) return;
    }
    window.sessionStorage.removeItem(RADAR_RUN_STORAGE_KEY);
  } catch {
    // The task can still finish when storage is disabled by the browser.
  }
}

function radarStatusPath(record) {
  const id = String(record.id || "");
  if (!id) throw new Error("任务缺少标识，无法查询运行状态。");
  const expectedPath = `${API_BASE}/radar/runs/${encodeURIComponent(id)}`;
  const raw = String(record.status_url || "");
  if (raw) {
    try {
      const url = new URL(raw, window.location.origin);
      if (url.origin === window.location.origin && url.pathname === expectedPath && !url.search) {
        return url.pathname.slice(API_BASE.length);
      }
    } catch {
      // Fall back to the canonical same-origin path below.
    }
  }
  return expectedPath.slice(API_BASE.length);
}

function radarTaskDigest(task) {
  if (task?.result?.summary) return task.result;
  if (task?.result?.digest?.summary) return task.result.digest;
  return null;
}

function radarTaskFailure(task) {
  const taskError = task?.error;
  const message = typeof taskError === "string"
    ? taskError
    : taskError?.message || "雷达任务执行失败，请检查岗位来源与简历状态后重试。";
  const error = new Error(message);
  error.code = typeof taskError === "object" && taskError?.code ? taskError.code : "radar_run_failed";
  return error;
}

function radarPollDelay(index) {
  if (index === 0) return 1000;
  if (index === 1) return 2000;
  return 5000;
}

function waitFor(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function setRadarTaskVisual(task) {
  const status = RADAR_ACTIVE_STATUSES.has(task?.status) ? task.status : "queued";
  state.radarRunStatus = status;
  const labels = {
    queued: "排队中",
    running: "运行中",
    retrying: task?.attempts && task?.max_attempts
      ? `重试中 ${task.attempts}/${task.max_attempts}`
      : "重试中",
  };
  byId("radarRunState").textContent = labels[status];
  syncRadarRunButtons();
}

function radarButtonLabelTarget(button) {
  return button.id === "runRadarButton" ? button.querySelector("span") || button : button;
}

function syncRadarRunButtons(activeButton = null) {
  if (activeButton) state.radarActiveButtonId = activeButton.id;
  const activeId = state.radarActiveButtonId || "runRadarButton";
  const runningLabel = {
    queued: "任务排队中…",
    running: "正在匹配…",
    retrying: "任务重试中…",
  }[state.radarRunStatus] || "处理中…";
  ["runRadarButton", "topRunRadarButton", "runFromPoolButton"].forEach((id) => {
    const button = byId(id);
    const label = radarButtonLabelTarget(button);
    if (!label.dataset.idleLabel) label.dataset.idleLabel = label.textContent;
    label.textContent = state.radarRunning ? runningLabel : label.dataset.idleLabel;
    const isActive = state.radarRunning && id === activeId;
    button.classList.toggle("is-loading", isActive);
    button.disabled = state.radarRunning || (id === "runFromPoolButton" && state.jobPool.length === 0);
    if (state.radarRunning) button.setAttribute("aria-busy", "true");
    else button.removeAttribute("aria-busy");
  });
}

async function submitRadarRun(record) {
  const task = await apiRequest("/radar/runs", {
    method: "POST",
    body: record.body,
    headers: { "Idempotency-Key": record.idempotency_key },
    timeout: 30000,
  });
  if (!task?.id || !RADAR_ACTIVE_STATUSES.has(task.status) && !RADAR_TERMINAL_STATUSES.has(task.status)) {
    const error = new Error("服务返回了无法识别的任务状态，请稍后重试。");
    error.code = "invalid_task_response";
    throw error;
  }
  record.id = task.id;
  record.status_url = task.status_url || `${API_BASE}/radar/runs/${encodeURIComponent(task.id)}`;
  record.status = task.status;
  storeRadarRun(record);
  return task;
}

async function pollRadarRun(record, initialTask = null) {
  let task = initialTask;
  let pollIndex = 0;
  while (true) {
    if (task) {
      if (String(task.id) !== String(record.id)) {
        const error = new Error("任务状态与提交记录不一致，已停止自动查询。");
        error.code = "task_identity_mismatch";
        throw error;
      }
      if (!RADAR_ACTIVE_STATUSES.has(task.status) && !RADAR_TERMINAL_STATUSES.has(task.status)) {
        const error = new Error("服务返回了未知任务状态，请稍后重试。");
        error.code = "invalid_task_status";
        throw error;
      }
      record.status = task.status;
      storeRadarRun(record);
      if (task.status === "succeeded") return task;
      if (task.status === "failed") throw radarTaskFailure(task);
      setRadarTaskVisual(task);
    }

    if (Date.now() >= record.deadline_at) {
      const error = new Error("任务仍在后台处理，页面已停止自动查询；再次点击运行可继续查看同一任务。");
      error.code = "radar_poll_timeout";
      error.keepRadarRecord = true;
      throw error;
    }

    const delay = task || pollIndex > 0 ? radarPollDelay(pollIndex) : 0;
    if (delay) await waitFor(Math.min(delay, Math.max(0, record.deadline_at - Date.now())));
    if (Date.now() >= record.deadline_at) continue;
    pollIndex += 1;
    try {
      task = await apiRequest(radarStatusPath(record), { timeout: 15000 });
    } catch (error) {
      if (error.status === 408 || error.status === 429) {
        state.radarRunStatus = "retrying";
        byId("radarRunState").textContent = error.status === 429 ? "限流等待中" : "连接重试中";
        syncRadarRunButtons();
        if (error.retryAfter) {
          await waitFor(
            Math.min(error.retryAfter * 1000, Math.max(0, record.deadline_at - Date.now())),
          );
        }
        task = null;
        continue;
      }
      if (error.status && error.status < 500) throw error;
      task = null;
      state.radarRunStatus = "retrying";
      byId("radarRunState").textContent = "连接重试中";
      syncRadarRunButtons();
    }
  }
}

async function completeRadarRun(task, record, resumed) {
  clearStoredRadarRun(record);
  const returnedDigest = radarTaskDigest(task);
  if (returnedDigest) renderRadarDigest(returnedDigest);
  const refreshResults = await Promise.allSettled([loadRadar(), loadDashboard()]);
  const digest = returnedDigest || state.radar?.latest;
  switchPanel("radar");
  if (digest?.summary) {
    const prefix = resumed ? "已恢复并完成" : "雷达完成";
    showToast(
      `${prefix}：从 ${digest.summary.collected} 个岗位中推荐 ${digest.summary.shortlisted} 个。`,
    );
  } else {
    showToast("雷达任务已完成，最新结果正在同步。");
  }
  warnIfRefreshFailed(refreshResults);
  window.requestAnimationFrame(() => {
    byId("radarResultQuery").scrollIntoView({
      behavior: REDUCED_MOTION_QUERY.matches ? "auto" : "smooth",
      block: "center",
    });
  });
}

async function continueRadarRun(record, options = {}) {
  state.radarRunning = true;
  state.radarRunStatus = record.status && RADAR_ACTIVE_STATUSES.has(record.status)
    ? record.status
    : "queued";
  state.radarActiveButtonId = options.button?.id || state.radarActiveButtonId || "runRadarButton";
  syncRadarRunButtons(options.button);
  setRadarTaskVisual({ status: state.radarRunStatus });
  try {
    const initialTask = record.id ? null : await submitRadarRun(record);
    const task = await pollRadarRun(record, initialTask);
    await completeRadarRun(task, record, Boolean(options.resumed));
  } catch (error) {
    const serviceUnavailableByConfiguration = error.code === "background_workers_disabled";
    const ambiguousSubmission = !record.id
      && !serviceUnavailableByConfiguration
      && (
        !error.status
        || error.status >= 500
        || error.status === 408
        || error.status === 429
        || error.code === "timeout"
      );
    const keepRecord = Boolean(error.keepRadarRecord || ambiguousSubmission);
    if (!keepRecord) clearStoredRadarRun(record);
    byId("radarRunState").textContent = keepRecord ? "后台处理中" : "运行失败";
    const message = ambiguousSubmission
      ? "提交结果暂时无法确认；幂等记录已保留，刷新页面会继续恢复同一任务。"
      : errorMessage(error);
    showToast(message, keepRecord ? "warning" : "error", 7000);
    if (error.code === "resume_unavailable") switchPanel("resume");
  } finally {
    state.radarRunning = false;
    state.radarRunStatus = null;
    state.radarActiveButtonId = null;
    syncRadarRunButtons();
  }
}

async function runRadar(options = {}) {
  if (state.radarRunning) return;
  const stored = readStoredRadarRun();
  if (stored) {
    stored.deadline_at = Date.now() + RADAR_POLL_TIMEOUT_MS;
    storeRadarRun(stored);
    await continueRadarRun(stored, { button: options.button, resumed: true });
    return;
  }
  const useDemo = options.useDemo ?? byId("radarUseDemo").checked;
  const body = options.sources
    ? { sources: options.sources }
    : useDemo
      ? { sources: ["demo"] }
      : {};
  const now = Date.now();
  const record = {
    idempotency_key: createRadarIdempotencyKey(),
    body,
    status: "queued",
    submitted_at: now,
    deadline_at: now + RADAR_POLL_TIMEOUT_MS,
  };
  if (!storeRadarRun(record)) {
    showToast("浏览器未开放会话存储；本次任务可运行，但刷新后无法自动恢复。", "warning", 6500);
  }
  await continueRadarRun(record, { button: options.button });
}

function resumeStoredRadarRun() {
  const record = readStoredRadarRun();
  if (!record || state.radarRunning) return;
  record.deadline_at = Date.now() + RADAR_POLL_TIMEOUT_MS;
  storeRadarRun(record);
  showToast("检测到未完成的雷达任务，正在恢复进度。");
  continueRadarRun(record, { resumed: true }).catch(() => {
    // continueRadarRun reports operational failures and always restores button state.
  });
}

async function saveRadarSettings(event) {
  event.preventDefault();
  const button = byId("saveSettingsButton");
  const keywords = splitList(byId("preferenceKeywords").value);
  const cities = [...document.querySelectorAll("#cityCheckboxes input:checked")].map((input) => input.value);
  const sources = [...document.querySelectorAll('input[name="radarSource"]:checked')].map((input) => input.value);
  if (keywords.length === 0 || keywords.length > 6) {
    showToast("请设置 1–6 个岗位关键词。", "warning");
    byId("preferenceKeywords").focus();
    return;
  }
  if (cities.length === 0 || cities.length > 5) {
    showToast("请选择 1–5 个目标城市。", "warning");
    document.querySelector("#cityCheckboxes input")?.focus();
    return;
  }
  if (sources.length === 0) {
    showToast("请至少选择一个可用的岗位来源。", "warning");
    document.querySelector('input[name="radarSource"]:not(:disabled)')?.focus();
    return;
  }
  if (byId("emailEnabled").checked && !byId("emailTo").value.trim()) {
    showToast("开启邮件摘要前，请填写收件邮箱。", "warning");
    byId("emailTo").focus();
    return;
  }

  setButtonLoading(button, true);
  const payload = {
    enabled: byId("radarEnabled").checked,
    schedule_time: byId("scheduleTime").value,
    keywords,
    cities,
    sources,
    new_grad_only: byId("newGradOnly").checked,
    exclude_keywords: splitList(byId("excludeKeywords").value),
    min_score: Number(byId("minScore").value),
    max_results: Number(byId("maxResults").value),
    email_enabled: byId("emailEnabled").checked,
    email_to: byId("emailTo").value.trim(),
  };
  try {
    const settings = await apiRequest("/radar/settings", { method: "PATCH", body: payload });
    renderSettings(settings);
    showToast("工作流已保存。");
  } catch (error) {
    showToast(errorMessage(error), "error");
  } finally {
    setButtonLoading(button, false);
  }
}

function setResumeMode(mode) {
  if (!["text", "file"].includes(mode)) mode = "text";
  state.resumeMode = mode;
  document.querySelectorAll("[data-resume-mode]").forEach((button) => {
    const active = button.dataset.resumeMode === mode;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  byId("resumeTextInput").hidden = mode !== "text";
  byId("resumeFileInputWrap").hidden = mode !== "file";
}

function setReportTab(tab) {
  if (!["summary", "skills", "quality"].includes(tab)) tab = "summary";
  document.querySelectorAll("[data-report-tab]").forEach((button) => {
    const active = button.dataset.reportTab === tab;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll("[data-report-pane]").forEach((pane) => {
    const active = pane.dataset.reportPane === tab;
    pane.classList.toggle("is-active", active);
    pane.hidden = !active;
  });
}

function bindTabKeyboard(selector) {
  const buttons = [...document.querySelectorAll(selector)];
  buttons.forEach((button, index) => {
    button.addEventListener("keydown", (event) => {
      let nextIndex = null;
      if (event.key === "ArrowRight") nextIndex = (index + 1) % buttons.length;
      else if (event.key === "ArrowLeft") nextIndex = (index - 1 + buttons.length) % buttons.length;
      else if (event.key === "Home") nextIndex = 0;
      else if (event.key === "End") nextIndex = buttons.length - 1;
      if (nextIndex === null) return;
      event.preventDefault();
      buttons[nextIndex].click();
      buttons[nextIndex].focus();
    });
  });
}

function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      const result = String(reader.result || "");
      resolve(result.includes(",") ? result.split(",", 2)[1] : result);
    });
    reader.addEventListener("error", () => reject(new Error("读取文件失败")));
    reader.readAsDataURL(file);
  });
}

async function analyzeResume(event) {
  event.preventDefault();
  const button = byId("analyzeResumeButton");
  setButtonLoading(button, true);
  try {
    let payload;
    if (state.resumeMode === "file") {
      const file = state.resumeFile || byId("resumeFile").files[0];
      if (!file) throw new Error("请选择简历文件");
      if (file.size > MAX_RESUME_FILE_BYTES) throw new Error("文件不能超过 4 MB");
      payload = {
        file: {
          name: file.name,
          content_base64: await fileToBase64(file),
        },
      };
    } else {
      const text = byId("resumeText").value.trim();
      if (text.length < 40) throw new Error("简历内容过短，请至少提供 40 个字符");
      payload = { text };
    }
    const result = await apiRequest("/resumes/analyze", {
      method: "POST",
      body: payload,
      timeout: 60000,
    });
    state.resumeResult = result;
    renderResumeReport(result);
    const refreshResults = await Promise.allSettled([loadDashboard()]);
    showToast("简历画像已建立，现在可以运行求职雷达。");
    warnIfRefreshFailed(refreshResults);
  } catch (error) {
    showToast(errorMessage(error), "error");
  } finally {
    setButtonLoading(button, false);
  }
}

function renderResumeReport(result) {
  byId("resumePlaceholder").hidden = true;
  byId("resumeReportContent").hidden = false;
  byId("resumeScore").textContent = String(result.score.total);
  byId("resumeScoreRing").style.setProperty("--score-angle", `${Math.max(0, Math.min(100, result.score.total)) * 3.6}deg`);
  byId("resumeName").textContent = result.name;
  byId("resumeLevel").textContent = `${result.score.level} · 本地可解释评分`;
  byId("resumeSummary").textContent = result.summary;

  const suggestions = byId("resumeSuggestions");
  suggestions.replaceChildren();
  (result.score.suggestions || []).forEach((text) => {
    suggestions.append(make("div", { className: "advice-item", text }));
  });

  const skills = byId("resumeSkills");
  skills.replaceChildren();
  Object.values(result.structured.skills || {}).flat().forEach((skill) => {
    skills.append(make("span", { className: "chip", text: skill }));
  });
  if (!skills.childElementCount) skills.append(make("p", { className: "muted", text: "尚未识别到明确技能。" }));

  const evidence = byId("resumeEvidence");
  evidence.replaceChildren();
  [...(result.structured.projects || []), ...(result.structured.experience || [])].slice(0, 10).forEach((line) => {
    evidence.append(make("div", { className: "evidence-line", text: line }));
  });
  if (!evidence.childElementCount) evidence.append(make("p", { className: "muted", text: "项目与经历证据较少。" }));

  const dimensions = byId("resumeDimensions");
  dimensions.replaceChildren();
  (result.score.dimensions || []).forEach((item) => {
    const row = make("div", { className: "dimension-row" });
    const head = make("div", { className: "dimension-head" });
    append(head, make("span", { text: item.label }), make("strong", { text: `${item.score}/${item.max}` }));
    const bar = make("div", { className: "dimension-bar" });
    const fill = make("span");
    fill.style.width = `${Math.round((item.score / item.max) * 100)}%`;
    bar.append(fill);
    append(row, head, bar);
    dimensions.append(row);
  });
}

function updateResumeFileName(file) {
  byId("resumeFileName").textContent = file ? `${file.name} · ${(file.size / 1024).toFixed(0)} KB` : "尚未选择文件";
}

function renderJobPool(items) {
  state.jobPool = Array.isArray(items) ? items : [];
  syncRadarRunButtons();
  byId("jobPoolCount").textContent = String(state.jobPool.length);
  const body = byId("jobPoolBody");
  body.replaceChildren();
  byId("jobPoolEmpty").hidden = state.jobPool.length > 0;
  state.jobPool.forEach((job) => {
    const row = make("tr");
    const identity = make("td");
    append(identity, make("strong", { text: job.title }), make("small", { text: job.company }));
    append(
      row,
      identity,
      make("td", { text: job.location || "—" }),
      make("td", { text: job.salary || "面议" }),
      make("td", { text: job.source || "导入" }),
    );
    body.append(row);
  });
}

async function crawlJobs(event) {
  event.preventDefault();
  const button = byId("crawlJobsButton");
  setButtonLoading(button, true);
  try {
    const result = await apiRequest("/jobs/crawl", {
      method: "POST",
      body: {
        keyword: byId("crawlKeyword").value.trim(),
        city: byId("crawlCity").value,
        pages: Number(byId("crawlPages").value),
        use_demo: byId("crawlUseDemo").checked,
      },
      timeout: 120000,
    });
    renderJobPool(result.items);
    const refreshResults = await Promise.allSettled([loadDashboard()]);
    showToast(`岗位池已更新：${result.count} 个${result.mode === "demo" ? "演示" : "真实"}岗位。`);
    warnIfRefreshFailed(refreshResults);
  } catch (error) {
    showToast(errorMessage(error), "error", 6500);
  } finally {
    setButtonLoading(button, false);
  }
}

async function importJobs(event) {
  event.preventDefault();
  const button = byId("importJobsButton");
  setButtonLoading(button, true);
  try {
    const result = await apiRequest("/jobs/import", {
      method: "POST",
      body: { job_text: byId("jobImportText").value.trim() },
    });
    renderJobPool(result.items);
    const refreshResults = await Promise.allSettled([loadDashboard()]);
    showToast(`已导入 ${result.count} 个岗位。`);
    warnIfRefreshFailed(refreshResults);
  } catch (error) {
    showToast(errorMessage(error), "error");
  } finally {
    setButtonLoading(button, false);
  }
}

async function enhanceResume(event) {
  event.preventDefault();
  const button = byId("enhanceResumeButton");
  const source = byId("enhanceResumeSource").value;
  const resumeText = source === "paste" ? byId("enhanceResumeText").value.trim() : "";
  const jd = byId("enhanceJd").value.trim();
  if (source === "paste" && resumeText.length < 40) {
    showToast("请粘贴至少 40 个字符的简历正文。", "warning");
    byId("enhanceResumeText").focus();
    return;
  }
  if (jd.length < 20) {
    showToast("目标岗位 JD 过短，请至少提供 20 个字符的职责与要求。", "warning");
    byId("enhanceJd").focus();
    return;
  }
  const aiMayBeUsed = state.health?.capabilities?.ai?.enabled !== false;
  if (aiMayBeUsed && !byId("aiProcessingConsent").checked) {
    showToast("请先确认 AI 数据处理授权；如果服务端未启用 AI，刷新能力状态后将自动切换为本地模式。", "warning", 6500);
    byId("aiProcessingConsent").focus();
    return;
  }

  const output = byId("enhancedResumeOutput");
  output.textContent = "正在生成定向版本…";
  output.setAttribute("aria-busy", "true");
  byId("coverageRow").hidden = true;
  byId("copyEnhancedButton").disabled = true;
  byId("enhanceKeywords").replaceChildren();
  byId("enhanceRecommendations").replaceChildren();
  setButtonLoading(button, true);
  try {
    const result = await apiRequest("/resumes/enhance", {
      method: "POST",
      body: {
        resume_source: source,
        resume_text: resumeText,
        jd,
        template: byId("enhanceTemplate").value,
        allow_ai: aiMayBeUsed && byId("aiProcessingConsent").checked,
      },
      timeout: 90000,
    });
    output.textContent = result.optimized_resume;
    byId("coverageValue").textContent = `${result.coverage}%`;
    byId("coverageRow").hidden = false;
    byId("copyEnhancedButton").disabled = false;
    const keywords = byId("enhanceKeywords");
    keywords.replaceChildren();
    (result.matched_keywords || []).slice(0, 8).forEach((keyword) => {
      keywords.append(make("span", { className: "chip", text: keyword }));
    });
    const advice = byId("enhanceRecommendations");
    advice.replaceChildren();
    if (result.warning) advice.append(make("div", { className: "advice-item", text: result.warning }));
    (result.recommendations || []).forEach((text) => {
      advice.append(make("div", { className: "advice-item", text }));
    });
    showToast(result.mode === "ai" ? "AI 定向版本已生成。" : "本地规则版本已生成。");
  } catch (error) {
    output.textContent = "生成失败。请检查输入或服务状态后重试。";
    showToast(errorMessage(error), "error");
  } finally {
    output.removeAttribute("aria-busy");
    if (aiMayBeUsed) byId("aiProcessingConsent").checked = false;
    setButtonLoading(button, false);
  }
}

async function copyEnhancedResume() {
  const text = byId("enhancedResumeOutput").textContent;
  try {
    await navigator.clipboard.writeText(text);
    showToast("已复制定向简历。");
  } catch {
    showToast("浏览器未授权剪贴板，请手动选择复制。", "warning");
  }
}

function renderApplicationStats(statistics) {
  const stats = statistics || { total: 0, by_status: {} };
  byId("appTotal").textContent = String(stats.total || 0);
  Object.keys(STATUS_LABELS).forEach((status) => {
    const id = {
      submitted: "appSubmitted",
      viewed: "appViewed",
      interviewing: "appInterviewing",
      offered: "appOffered",
      rejected: "appRejected",
    }[status];
    byId(id).textContent = String(stats.by_status?.[status] || 0);
  });
  byId("appDueSoon").textContent = String(stats.due_soon || 0);
  byId("appResponseRate").textContent = `${stats.response_rate || 0}%`;
}

function renderApplications(result) {
  state.applications = result.items || [];
  renderApplicationStats(result.statistics);
  const body = byId("applicationsBody");
  body.replaceChildren();
  byId("applicationsEmpty").hidden = state.applications.length > 0;
  state.applications.forEach((record) => {
    const row = make("tr");
    const identity = make("td");
    append(identity, make("strong", { text: record.job_title }), make("small", { text: record.company_name }));
    const statusCell = make("td");
    statusCell.append(make("span", {
      className: `status-badge status-${record.status}`,
      text: STATUS_LABELS[record.status] || record.status,
    }));
    const actions = make("td");
    const actionWrap = make("div", { className: "table-actions" });
    const edit = make("button", { className: "text-button", type: "button", text: "编辑" });
    edit.addEventListener("click", () => openApplicationDialog(record));
    const remove = make("button", { className: "text-button danger", type: "button", text: "删除" });
    remove.addEventListener("click", () => deleteApplication(record));
    append(actionWrap, edit, remove);
    actions.append(actionWrap);
    const nextStep = make("td");
    append(
      nextStep,
      make("strong", { text: record.next_action || record.notes || "待补充下一步" }),
      make("small", {
        className: record.follow_up_at && new Date(record.follow_up_at) < new Date() ? "is-overdue" : "",
        text: record.interview_at
          ? `面试 ${formatDate(record.interview_at)}`
          : record.follow_up_at ? `跟进 ${formatDate(record.follow_up_at)}` : "未设置提醒",
      }),
    );
    append(
      row,
      identity,
      make("td", { text: String(record.applied_at || "").slice(0, 10) || "—" }),
      statusCell,
      nextStep,
      actions,
    );
    body.append(row);
  });
}

async function loadApplications() {
  const params = new URLSearchParams();
  const status = byId("applicationStatusFilter")?.value || "";
  const query = byId("applicationQuery")?.value.trim() || "";
  if (status) params.set("status", status);
  if (query) params.set("query", query);
  const suffix = params.toString() ? `?${params}` : "";
  const result = await apiRequest(`/applications${suffix}`);
  renderApplications(result);
}

function openApplicationDialog(record = null, job = null) {
  const editing = Boolean(record);
  byId("applicationDialogTitle").textContent = editing ? "更新投递" : "新增投递";
  byId("applicationId").value = record?.id || "";
  byId("applicationJobDescription").value =
    record?.job_description || job?.description || job?.job_description || "";
  byId("applicationJobKey").value = record?.job_key || job?.job_key || "";
  byId("applicationSourceUrl").value = record?.source_url || job?.url || "";
  byId("applicationCompany").value = record?.company_name || job?.company || "";
  byId("applicationTitle").value = record?.job_title || job?.title || "";
  byId("applicationStatus").value = record?.status || "submitted";
  byId("applicationDate").value = record?.applied_at
    ? String(record.applied_at).slice(0, 10)
    : localDateInputValue();
  byId("applicationLocation").value = record?.location || job?.location || "";
  byId("applicationSalary").value = record?.salary_range || job?.salary || "";
  byId("applicationNotes").value = record?.notes || "";
  byId("applicationNextAction").value = record?.next_action || "";
  byId("applicationResumeVersion").value = record?.resume_version || "";
  byId("applicationFollowUp").value = datetimeLocalInputValue(record?.follow_up_at);
  byId("applicationInterviewAt").value = datetimeLocalInputValue(record?.interview_at);
  byId("applicationCompany").disabled = editing;
  byId("applicationTitle").disabled = editing;
  byId("applicationDate").disabled = editing;
  byId("applicationDialog").showModal();
  window.setTimeout(() => byId(editing ? "applicationStatus" : "applicationCompany").focus(), 0);
}

async function saveApplication(event) {
  event.preventDefault();
  const button = byId("saveApplicationButton");
  setButtonLoading(button, true);
  const id = byId("applicationId").value;
  try {
    if (id) {
      await apiRequest(`/applications/${id}`, {
        method: "PATCH",
        body: {
          status: byId("applicationStatus").value,
          notes: byId("applicationNotes").value.trim(),
          location: byId("applicationLocation").value.trim(),
          salary_range: byId("applicationSalary").value.trim(),
          next_action: byId("applicationNextAction").value.trim(),
          resume_version: byId("applicationResumeVersion").value.trim(),
          follow_up_at: byId("applicationFollowUp").value,
          interview_at: byId("applicationInterviewAt").value,
        },
      });
    } else {
      await apiRequest("/applications", {
        method: "POST",
        body: {
          company_name: byId("applicationCompany").value.trim(),
          job_title: byId("applicationTitle").value.trim(),
          job_description: byId("applicationJobDescription").value,
          job_key: byId("applicationJobKey").value,
          source_url: byId("applicationSourceUrl").value,
          status: byId("applicationStatus").value,
          applied_at: byId("applicationDate").value,
          location: byId("applicationLocation").value.trim(),
          salary_range: byId("applicationSalary").value.trim(),
          next_action: byId("applicationNextAction").value.trim(),
          resume_version: byId("applicationResumeVersion").value.trim(),
          follow_up_at: byId("applicationFollowUp").value,
          interview_at: byId("applicationInterviewAt").value,
          notes: byId("applicationNotes").value.trim(),
        },
      });
    }
    byId("applicationDialog").close();
    const refreshResults = await Promise.allSettled([loadApplications(), loadDashboard()]);
    showToast(id ? "投递状态已更新。" : "投递已建档。");
    warnIfRefreshFailed(refreshResults);
  } catch (error) {
    showToast(errorMessage(error), "error");
  } finally {
    setButtonLoading(button, false);
  }
}

async function deleteApplication(record) {
  if (!window.confirm(`确认删除“${record.company_name} · ${record.job_title}”的投递记录？`)) return;
  try {
    await apiRequest(`/applications/${record.id}`, { method: "DELETE" });
    const refreshResults = await Promise.allSettled([loadApplications(), loadDashboard()]);
    showToast("投递记录已删除。");
    warnIfRefreshFailed(refreshResults);
  } catch (error) {
    showToast(errorMessage(error), "error");
  }
}

async function loadHealth() {
  const health = await apiRequest("/health");
  renderHealth(health);
}

async function loadDashboard() {
  const dashboard = await apiRequest("/dashboard");
  state.dashboard = dashboard;
  byId("jobPoolCount").textContent = String(dashboard.jobs || 0);
  renderApplicationStats(dashboard.applications);
  renderOnboarding(dashboard.onboarding);
  if (dashboard.radar?.settings) renderSettings(dashboard.radar.settings);
}

async function loadRadar() {
  const overview = await apiRequest("/radar");
  renderRadarOverview(overview);
}

async function loadJobPool() {
  const result = await apiRequest("/jobs");
  renderJobPool(result.items);
}

async function loadJobFeedback() {
  const result = await apiRequest("/jobs/feedback");
  (result.items || []).forEach((item) => {
    state.jobFeedback.set(item.job_key, item.action);
    if (item.action === "saved") state.savedJobKeys.add(item.job_key);
  });
  storeSavedJobKeys();
  syncSavedCount();
}

function bindEvents() {
  document.querySelectorAll("[data-panel]").forEach((button) => {
    button.addEventListener("click", () => switchPanel(button.dataset.panel));
  });
  document.querySelectorAll("[data-switch-panel]").forEach((button) => {
    button.addEventListener("click", () => switchPanel(button.dataset.switchPanel));
  });
  byId("mobileMenuButton").addEventListener("click", () => {
    if (byId("sidebar").classList.contains("is-open")) closeMobileMenu({ restoreFocus: true });
    else openMobileMenu();
  });
  byId("mobileOverlay").addEventListener("click", () => closeMobileMenu({ restoreFocus: true }));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && byId("sidebar").classList.contains("is-open")) {
      event.preventDefault();
      closeMobileMenu({ restoreFocus: true });
    }
  });
  if (typeof MOBILE_NAV_QUERY.addEventListener === "function") {
    MOBILE_NAV_QUERY.addEventListener("change", syncMobileNavigation);
  } else {
    MOBILE_NAV_QUERY.addListener(syncMobileNavigation);
  }
  byId("runRadarButton").addEventListener("click", () => runRadar());
  byId("topRunRadarButton").addEventListener("click", () => runRadar({ button: byId("topRunRadarButton") }));
  byId("runFromPoolButton").addEventListener("click", () => {
    if (state.jobPool.length === 0) {
      showToast("岗位池为空，请先采集或导入岗位。", "warning");
      return;
    }
    switchPanel("radar");
    runRadar({ button: byId("runRadarButton"), sources: ["latest"] });
  });
  byId("radarSettingsForm").addEventListener("submit", saveRadarSettings);
  byId("minScore").addEventListener("input", () => {
    byId("minScoreValue").textContent = byId("minScore").value;
  });
  byId("radarResultQuery").addEventListener("input", renderRadarItems);
  byId("radarResultSort").addEventListener("change", renderRadarItems);
  byId("radarSavedOnly").addEventListener("click", () => {
    const button = byId("radarSavedOnly");
    const active = button.getAttribute("aria-pressed") !== "true";
    button.setAttribute("aria-pressed", String(active));
    renderRadarItems();
  });
  byId("radarFilterReset").addEventListener("click", resetRadarFilters);
  document.querySelector("[data-reset-radar-filters]")
    .addEventListener("click", resetRadarFilters);
  document.addEventListener("keydown", (event) => {
    const target = event.target;
    const isTyping = target instanceof HTMLInputElement
      || target instanceof HTMLTextAreaElement
      || target instanceof HTMLSelectElement
      || target?.isContentEditable;
    if (event.key === "/" && !isTyping && state.currentPanel === "radar") {
      event.preventDefault();
      byId("radarResultQuery").focus();
    }
  });

  document.querySelectorAll("[data-resume-mode]").forEach((button) => {
    button.addEventListener("click", () => setResumeMode(button.dataset.resumeMode));
  });
  bindTabKeyboard("[data-resume-mode]");
  byId("resumeAnalyzeForm").addEventListener("submit", analyzeResume);
  byId("resumeFile").addEventListener("change", (event) => {
    state.resumeFile = event.target.files[0] || null;
    updateResumeFileName(state.resumeFile);
  });
  const dropZone = byId("resumeDropZone");
  ["dragenter", "dragover"].forEach((type) => {
    dropZone.addEventListener(type, (event) => {
      event.preventDefault();
      dropZone.classList.add("is-dragging");
    });
  });
  ["dragleave", "drop"].forEach((type) => {
    dropZone.addEventListener(type, (event) => {
      event.preventDefault();
      dropZone.classList.remove("is-dragging");
    });
  });
  dropZone.addEventListener("drop", (event) => {
    const file = event.dataTransfer.files[0];
    if (!file) return;
    state.resumeFile = file;
    updateResumeFileName(file);
  });
  document.querySelectorAll("[data-report-tab]").forEach((button) => {
    button.addEventListener("click", () => setReportTab(button.dataset.reportTab));
  });
  bindTabKeyboard("[data-report-tab]");

  byId("crawlJobsForm").addEventListener("submit", crawlJobs);
  byId("importJobsForm").addEventListener("submit", importJobs);
  byId("enhanceResumeForm").addEventListener("submit", enhanceResume);
  byId("enhanceResumeSource").addEventListener("change", () => {
    byId("enhanceResumeTextField").hidden = byId("enhanceResumeSource").value !== "paste";
  });
  byId("copyEnhancedButton").addEventListener("click", copyEnhancedResume);

  byId("addApplicationButton").addEventListener("click", () => openApplicationDialog());
  byId("applicationForm").addEventListener("submit", saveApplication);
  document.querySelectorAll("[data-close-dialog]").forEach((button) => {
    button.addEventListener("click", () => byId("applicationDialog").close());
  });
  byId("filterApplicationsButton").addEventListener("click", () => {
    loadApplications().catch((error) => showToast(errorMessage(error), "error"));
  });
  byId("applicationQuery").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      loadApplications().catch((error) => showToast(errorMessage(error), "error"));
    }
  });
  window.addEventListener("hashchange", () => switchPanel(location.hash.slice(1), false));
}

async function initialise() {
  state.savedJobKeys = readSavedJobKeys();
  syncSavedCount();
  bindEvents();
  syncMobileNavigation();
  setResumeMode(state.resumeMode);
  setReportTab("summary");
  syncRadarRunButtons();
  switchPanel(location.hash.slice(1) || "radar", false);
  resumeStoredRadarRun();
  await loadJobFeedback().catch(() => {
    showToast("岗位反馈暂时无法同步，收藏仍会保存在当前浏览器。", "warning");
  });
  const healthTask = loadHealth().catch((error) => {
    renderHealthFailure();
    showToast(errorMessage(error), "error", 6500);
  });
  const dataTasks = [
    ["概览", loadDashboard()],
    ["雷达", loadRadar()],
    ["岗位池", loadJobPool()],
    ["投递看板", loadApplications()],
  ];
  const results = await Promise.allSettled([
    healthTask,
    ...dataTasks.map(([, task]) => task),
  ]);
  const failedModules = results
    .slice(1)
    .map((result, index) => (result.status === "rejected" ? dataTasks[index][0] : null))
    .filter(Boolean);
  if (failedModules.length) {
    showToast(`部分数据加载失败：${failedModules.join("、")}。可刷新页面重试。`, "warning", 7000);
  }
}

document.addEventListener("DOMContentLoaded", initialise);
