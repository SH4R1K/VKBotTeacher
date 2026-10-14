"use strict";

const $ = (id) => document.getElementById(id);

/* Лимит фото держит sender/vk_api.py (MAX_ATTACHMENTS). Дублируем здесь,
   чтобы интерфейс не давал отметить то, что сервер всё равно срежет молча. */
const MAX_PHOTOS = 10;
const DRAFT_KEY = "tohavibe.draft.v1";
const THEME_KEY = "tohavibe.theme";
const HISTORY_SIZE = 20;

const state = {
  transport: { mode: "clipboard", supports_photos: false, ready: true },
  chats: [],
  lessons: new Set(),
  photos: new Set(),
  pickedLessons: new Set(),
  pickedChats: new Set(),
  pickedPhotos: new Set(),
  current: null,
  title: "",
  settings: {
    mode: "clipboard",
    options: [],
    vk: {},
    chats: [],
    llm: { provider: "", providers: [] },
  },
  vkChatItems: [],
  vkPicked: new Set(),
  history: { page: 1, pages: 1, total: 0, search: "" },
};

const MODE_LABELS = {
  clipboard: "clipboard — копировать в буфер, вставляешь сам",
  user_token: "user_token — автоотправка от твоего имени",
  community: "community — автоотправка от имени сообщества",
};

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined && value !== false) node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    // append() приводит null к строке и вставляет текстовый узел "null".
    if (child !== null && child !== undefined && child !== false) node.append(child);
  }
  return node;
}

const icon = (name, extra = "") =>
  el("svg", { class: `ico ${extra}`.trim(), "aria-hidden": "true" }, [el("use", { href: `#i-${name}` })]);

const useIcon = (id, name) => $(id).querySelector("use").setAttribute("href", `#i-${name}`);

/* --- сообщения: тосты и инлайн-строки --- */

function toast(text, kind = "ok", detail = "") {
  const box = el("div", { class: `toast ${kind}` }, [
    icon(kind === "ok" ? "check-circle" : kind === "warn" ? "alert" : "alert"),
    el("div", { class: "toast-text" }, [
      el("span", { text }),
      detail ? el("div", { class: "more", text: detail }) : null,
    ]),
    el(
      "button",
      { type: "button", class: "icon sm ghost del", title: "Скрыть", onclick: () => node.remove() },
      [icon("x", "sm")]
    ),
  ]);
  $("toasts").append(node);
  if (kind === "ok") setTimeout(() => node.remove(), 4500);
  return node;
}

function status(id, text, kind = "") {
  const box = $(id);
  box.className = `status-line ${kind}`.trim();
  if (!text) {
    box.replaceChildren();
    return;
  }
  const mark = kind === "ok" ? icon("check-circle", "sm") : kind === "bad" || kind === "warn" ? icon("alert", "sm") : null;
  box.replaceChildren(...[mark, el("span", { text })].filter(Boolean));
}

function busy(node, active) {
  if (!node) return;
  node.classList.toggle("is-busy", active);
  if (node.tagName === "BUTTON") {
    node.disabled = active;
    node.setAttribute("aria-busy", String(active));
  }
}

async function guard(button, action, statusId) {
  try {
    if (statusId) status(statusId, "");
    busy(button, true);
    await action();
  } catch (err) {
    toast(err.message, "bad");
    if (statusId) status(statusId, err.message, "bad");
  } finally {
    busy(button, false);
    syncActions();
  }
}

/* --- прогресс долгой операции --- */

let progressTimer = null;

const elapsed = (ms) => {
  const total = Math.floor(ms / 1000);
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
};

function stopProgress() {
  if (progressTimer) clearInterval(progressTimer);
  progressTimer = null;
}

function startProgress(text, statusId = "gen-status") {
  const box = $(statusId);
  stopProgress();
  box.className = "status-line";
  const stamp = el("span");
  box.replaceChildren(el("span", { class: "spinner" }), stamp);
  const started = Date.now();
  const paint = () => {
    stamp.textContent = `${text} · ${elapsed(Date.now() - started)}`;
  };
  paint();
  progressTimer = setInterval(paint, 1000);
}

function extractPeerId(text) {
  const runs = String(text).match(/\d{7,}/g) || [];
  return runs.find((run) => run.startsWith("2")) || runs[0] || null;
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const text = await response.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    const kind = response.headers.get("Content-Type") || "неизвестный тип";
    throw new Error(
      `Сервер вернул ${response.status} без JSON (${kind}). ` +
        `Чаще всего это старый процесс на 8765 — перезапусти python app.py. Ответ: ${text.slice(0, 120)}`
    );
  }
  if (!response.ok || data.error) throw new Error(data.error || `Сервер вернул ${response.status}`);
  return data;
}

const post = (path, body) =>
  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

function filterNames(names, query) {
  const needle = query.trim().toLowerCase();
  if (!needle) return names;
  return names.filter((name) => name.toLowerCase().includes(needle));
}

/* --- вкладки --- */

function initTabs(list, onSwitch) {
  const tabs = [...list.querySelectorAll('[role="tab"]')];
  const select = (tab) => {
    for (const item of tabs) {
      const on = item === tab;
      item.setAttribute("aria-selected", String(on));
      item.tabIndex = on ? 0 : -1;
      const panel = $(item.getAttribute("aria-controls"));
      if (panel) panel.hidden = !on;
    }
    if (onSwitch) onSwitch(tab);
  };
  list.addEventListener("click", (event) => {
    const tab = event.target.closest('[role="tab"]');
    if (tab) select(tab);
  });
  list.addEventListener("keydown", (event) => {
    const index = tabs.indexOf(document.activeElement);
    if (index < 0) return;
    let next = -1;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    if (next < 0) return;
    event.preventDefault();
    tabs[next].focus();
    select(tabs[next]);
  });
  select(tabs.find((tab) => tab.getAttribute("aria-selected") === "true") || tabs[0]);
}

const selectPanel = (tabId) => {
  $(tabId).click();
};

/* --- тема --- */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem(THEME_KEY, theme);
  useIcon("btn-theme", theme === "dark" ? "sun" : "moon");
  $(`btn-theme`).title = theme === "dark" ? "Включить светлую тему" : "Включить тёмную тему";
}

/* --- методички --- */

function lessonRow(name) {
  const box = el("input", { type: "checkbox" });
  box.checked = state.pickedLessons.has(name);
  box.addEventListener("change", () => {
    if (box.checked) state.pickedLessons.add(name);
    else state.pickedLessons.delete(name);
    syncActions();
    renderLessons();
  });
  const generate = el(
    "button",
    {
      type: "button",
      class: "icon sm ghost",
      title: "Сгенерировать сообщение только по этой методичке",
      onclick: () => generateOne(name, generate),
    },
    [icon("bolt", "sm"), el("span", { class: "visually-hidden", text: `Сгенерировать: ${name}` })]
  );
  return el("li", {}, [
    box,
    el("span", { class: "list-row-name", title: name, text: name }),
    generate,
  ]);
}

function renderLessons() {
  const list = $("list-lessons");
  const all = [...state.lessons].sort((a, b) => a.localeCompare(b, "ru"));
  const shown = filterNames(all, $("lesson-search").value);
  list.replaceChildren();
  if (!shown.length) {
    list.append(el("li", { class: "empty", text: all.length ? "Ничего не найдено" : "Методичек пока нет" }));
  }
  for (const name of shown) list.append(lessonRow(name));
  const picked = shown.filter((name) => state.pickedLessons.has(name)).length;
  $("lesson-count").textContent =
    (shown.length === all.length ? `${all.length} шт.` : `${shown.length} из ${all.length}`) +
    (picked ? ` · отмечено ${picked}` : "");
}

/* --- фото --- */

const photoUrl = (name) => `/api/photo?name=${encodeURIComponent(name)}`;
const thumbUrl = (name) => `/api/photo/thumb?name=${encodeURIComponent(name)}`;

function openPhotoView(name) {
  $("photo-view-img").src = photoUrl(name);
  $("photo-view-title").textContent = name;
  $("dlg-photo-view").showModal();
}

function confirmDialog(title, text, okLabel) {
  const dlg = $("dlg-confirm");
  $("confirm-title").textContent = title;
  $("confirm-text").textContent = text;
  $("confirm-ok").textContent = okLabel;
  return new Promise((resolve) => {
    const finish = (value) => {
      $("confirm-ok").removeEventListener("click", onOk);
      $("confirm-cancel").removeEventListener("click", onNo);
      dlg.removeEventListener("close", onClose);
      if (dlg.open) dlg.close();
      resolve(value);
    };
    const onOk = () => finish(true);
    const onNo = () => finish(false);
    const onClose = () => finish(false);
    $("confirm-ok").addEventListener("click", onOk);
    $("confirm-cancel").addEventListener("click", onNo);
    dlg.addEventListener("close", onClose);
    dlg.showModal();
    $("confirm-cancel").focus();
  });
}

async function deletePhoto(name) {
  const ok = await confirmDialog(
    "Удалить фото?",
    `Файл ${name} исчезнет из photos/ безвозвратно.`,
    "Удалить"
  );
  if (!ok) return;
  await post("/api/photo/delete", { name });
  state.photos.delete(name);
  state.pickedPhotos.delete(name);
  renderPhotos();
  syncActions();
  toast(`Удалено: ${name}`, "ok");
}

function photoCard(name) {
  const picked = state.pickedPhotos.has(name);
  const box = el("input", { type: "checkbox" });
  box.checked = picked;
  box.addEventListener("change", () => {
    if (box.checked && !picked && state.pickedPhotos.size >= MAX_PHOTOS) {
      box.checked = false;
      toast(`ВК принимает не больше ${MAX_PHOTOS} фото за раз — лишние уйдут молча. Сними одно, чтобы взять другое.`, "warn");
      return;
    }
    if (box.checked) state.pickedPhotos.add(name);
    else state.pickedPhotos.delete(name);
    syncPhotos();
  });
  const img = el("img", {
    src: thumbUrl(name),
    alt: name,
    loading: "lazy",
    decoding: "async",
    title: "Показать крупно",
  });
  img.addEventListener("click", () => openPhotoView(name));
  const del = el(
    "button",
    { type: "button", class: "icon sm ghost danger", title: "Удалить фото", onclick: () => deletePhoto(name) },
    [icon("trash", "sm"), el("span", { class: "visually-hidden", text: `Удалить ${name}` })]
  );
  return el("li", { class: `card${picked ? " picked" : ""}` }, [
    el("div", { class: "thumb" }, [img]),
    el("p", { class: "card-name", text: name, title: name }),
    el("div", { class: "card-row" }, [box, el("label", { text: "прикрепить" }), del]),
  ]);
}

function syncPhotos() {
  const picked = state.pickedPhotos.size;
  $("photo-selected-count").textContent = `Выбрано ${picked} из ${MAX_PHOTOS}`;
  $("photo-selected-count").className = `badge plain ${picked ? "ok" : ""}`.trim();
  syncActions();
}

function renderPhotos() {
  const list = $("list-photos");
  const all = [...state.photos].sort((a, b) => b.localeCompare(a, "ru"));
  const shown = filterNames(all, $("photo-search").value);
  list.replaceChildren();
  if (!shown.length) {
    list.append(el("li", { class: "empty", text: all.length ? "Ничего не найдено" : "Фото не загружены" }));
  }
  for (const name of shown) list.append(photoCard(name));
  $("photo-count").textContent = shown.length === all.length ? `${all.length} шт.` : `${shown.length} из ${all.length}`;
  $("photo-tab-count").textContent = all.length ? `· ${all.length}` : "";
  $("photo-hint").textContent = state.transport.supports_photos
    ? `Отмеченные фото прикрепляются к сообщению при отправке через API. ВК принимает не больше ${MAX_PHOTOS} за раз — остальные не уйдут. Клик по фото — предпросмотр.`
    : "Режим clipboard: фото придётся прикрепить в ВК вручную.";
  syncPhotos();
}

/* --- беседы --- */

function renderChats() {
  const list = $("list-chats");
  list.replaceChildren();
  if (!state.chats.length) {
    list.append(el("li", { class: "empty", text: "Чаты не заданы — открой «Настройки» → «Чаты рассылки»" }));
    syncSendSummary();
    return;
  }
  for (const chat of state.chats) {
    const box = el("input", { type: "checkbox" });
    box.checked = state.pickedChats.has(chat.name);
    box.addEventListener("change", () => {
      if (box.checked) state.pickedChats.add(chat.name);
      else state.pickedChats.delete(chat.name);
      syncActions();
      syncSendSummary();
    });
    list.append(
      el("li", {}, [
        box,
        el("label", { text: chat.name }),
        chat.api_capable ? null : el("span", { class: "badge warn", text: "без peer_id" }),
      ])
    );
  }
  syncSendSummary();
}

function syncSendSummary() {
  const chats = state.pickedChats.size;
  const photos = state.pickedPhotos.size;
  const parts = [];
  if (chats) parts.push(`${chats} ${chats === 1 ? "беседа" : "бесед"}`);
  if (photos) parts.push(`${photos} фото`);
  $("send-summary").textContent = parts.length ? `К отправке: ${parts.join(" · ")}` : "Отметь беседы и фото";
  const badge = $("send-count");
  badge.textContent = String(chats);
  badge.hidden = chats === 0;
}

/* --- факты и результаты --- */

function renderFacts(lesson) {
  const box = $("facts");
  box.replaceChildren();
  if (!lesson) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const badges = [
    `М1У5: ${lesson.code || "—"}`,
    `Тема: ${lesson.title || "—"}`,
    `Этапов: ${lesson.stages.length}`,
    `Задач: ${lesson.stages.reduce((sum, s) => sum + s.tasks.length, 0)}`,
    `Результатов: ${lesson.outcomes.length}`,
  ];
  if (!lesson.is_usable) badges.push("структура не распознана — полный текст");
  for (const text of badges) box.append(el("span", { class: "badge plain", text }));
}

function renderResults(data) {
  const list = $("results");
  list.replaceChildren();
  for (const item of data.results) {
    const detail = item.attachments ? `${item.detail} · фото отправлено: ${item.attachments}` : item.detail;
    list.append(
      el("li", { class: item.ok ? "ok" : "bad" }, [
        icon(item.ok ? "check-circle" : "alert", "sm"),
        el("div", { class: "body" }, [
          el("span", { class: "list-row-name", text: item.name }),
          el("span", { class: "detail", text: detail }),
        ]),
      ])
    );
  }
  for (const name of data.missing_photos || []) {
    list.append(
      el("li", { class: "bad" }, [
        icon("alert", "sm"),
        el("div", { class: "body" }, [
          el("span", { class: "list-row-name", text: "Фото не найдено" }),
          el("span", { class: "detail", text: name }),
        ]),
      ])
    );
  }
}

function syncActions() {
  const hasText = $("message").value.trim().length > 0;
  $("btn-generate-batch").disabled = state.pickedLessons.size === 0;
  $("generate-batch-label").textContent =
    state.pickedLessons.size ? `Сгенерировать выбранные (${state.pickedLessons.size})` : "Сгенерировать выбранные";
  $("btn-regenerate").disabled = !state.current;
  $("btn-copy").disabled = !hasText;
  $("btn-save").disabled = !hasText;
  $("btn-send").disabled = !hasText || state.pickedChats.size === 0;
  $("message-title").textContent = state.title;
  syncSendSummary();
}

async function loadState() {
  const data = await api("/api/state");
  state.transport = data.transport;
  state.chats = data.chats;
  state.lessons = new Set(data.lessons);
  state.photos = new Set(data.photos);
  const t = data.transport;
  const badge = $("badge-transport");
  const modeText = `транспорт: ${t.mode}`;
  badge.textContent = modeText;
  badge.className = `badge ${t.mode === "clipboard" ? "warn" : t.ready ? "ok" : "bad"}`;
  const modeHint =
    t.mode === "clipboard"
      ? "Режим буфера обмена: приложение копирует текст, отправляешь сам"
      : t.ready
        ? "Токен на месте, автоотправка работает"
        : "Токена нет — автоотправка не сработает";
  badge.title = `${modeText}\n${modeHint}`;
  const llmText = `${data.llm.model} @ ${data.llm.base_url}`;
  $("badge-llm").textContent = llmText;
  $("badge-llm").title = `${llmText}\nПровайдер ИИ и модель. Меняются в настройках.`;
  renderLessons();
  renderPhotos();
  renderChats();
  syncActions();
}

/* --- черновик --- */

let draftTimer = null;

function saveDraft() {
  const text = $("message").value;
  if (!text.trim()) {
    localStorage.removeItem(DRAFT_KEY);
    return;
  }
  localStorage.setItem(DRAFT_KEY, JSON.stringify({ text, title: state.title, current: state.current }));
}

function scheduleDraft() {
  clearTimeout(draftTimer);
  draftTimer = setTimeout(saveDraft, 400);
}

function dropDraft() {
  clearTimeout(draftTimer);
  localStorage.removeItem(DRAFT_KEY);
  $("btn-draft-restore").hidden = true;
}

function readDraft() {
  try {
    return JSON.parse(localStorage.getItem(DRAFT_KEY) || "null");
  } catch {
    return null;
  }
}

function restoreDraft(silent) {
  const draft = readDraft();
  if (!draft || !draft.text) return false;
  $("message").value = draft.text;
  state.title = draft.title || "";
  state.current = draft.current || null;
  $("btn-draft-restore").hidden = true;
  renderFacts(null);
  syncActions();
  if (!silent) toast("Черновик возвращён в редактор", "ok");
  return true;
}

/* --- генерация --- */

async function upload(files) {
  const picked = [...files];
  if (!picked.length) return;
  const form = new FormData();
  for (const file of picked) form.append("file", file, file.name);
  await guard($("drop-lesson"), async () => {
    await api("/api/upload", { method: "POST", body: form });
    await loadState();
  });
}

async function generateOne(name, button) {
  await guard(button, async () => {
    startProgress("Генерация");
    const data = await post("/api/generate", { name });
    $("message").value = data.text;
    state.current = data.name;
    state.title = `${data.lesson.code} ${data.lesson.title}`.trim() || data.name;
    renderFacts(data.lesson);
    status("gen-status", `Готово: ${name}`, "ok");
    saveDraft();
  }, "gen-status");
  stopProgress();
}

async function generateBatch() {
  await guard($("btn-generate-batch"), async () => {
    const names = [...state.pickedLessons];
    startProgress(`Генерация ${names.length} шт.`);
    const data = await post("/api/generate_batch", { names });
    const failed = data.items.filter((item) => item.error);
    const done = data.items.filter((item) => !item.error);
    if (done.length) {
      $("message").value = done.map((item) => item.text).join("\n\n\n");
      state.current = done.map((item) => item.name).join(", ");
      state.title = done.map((item) => item.title).join("; ") || "lesson";
      renderFacts(done[0].lesson);
    }
    if (failed.length) {
      status("gen-status", `Готово ${done.length} из ${names.length}. Не получилось: ${failed.map((item) => item.name).join(", ")}`, "warn");
      toast(
        `Генерация с ошибками: ${done.length} из ${names.length}`,
        "warn",
        failed.map((item) => `${item.name}: ${item.error}`).join("\n")
      );
    } else {
      status("gen-status", `Готово: ${done.length} шт.`, "ok");
    }
    saveDraft();
  }, "gen-status");
  stopProgress();
}

/* --- настройки ВК и рассылки --- */

const maskFor = (token) => (token ? `${"•".repeat(8)}${token.slice(-4)}` : "");

function setTokenField(inputId, token) {
  const field = $(inputId);
  field.dataset.real = token || "";
  field.dataset.masked = token ? "1" : "";
  field.value = maskFor(token);
}

function readTokenField(inputId) {
  const field = $(inputId);
  return (field.dataset.masked === "1" ? field.dataset.real : field.value).trim();
}

function wireTokenField(inputId, buttonId) {
  const field = $(inputId);
  const button = $(buttonId);
  field.addEventListener("input", () => {
    if (field.dataset.masked !== "1") field.dataset.real = field.value;
  });
  button.addEventListener("click", () => {
    const masked = field.dataset.masked === "1";
    field.dataset.masked = masked ? "" : "1";
    field.value = masked ? field.dataset.real : maskFor(field.dataset.real);
    useIcon(buttonId, masked ? "eye" : "eye-off");
    button.title = masked ? "Показать" : "Скрыть";
  });
}

function moveChat(index, step) {
  const target = index + step;
  if (target < 0 || target >= state.settings.chats.length) return;
  const list = state.settings.chats;
  [list[index], list[target]] = [list[target], list[index]];
  renderChatRows();
}

function iconButton(name, title, onclick, extra = "") {
  return el(
    "button",
    { type: "button", class: `icon sm ${extra}`.trim(), title, onclick },
    [icon(name, "sm"), el("span", { class: "visually-hidden", text: title })]
  );
}

function renderChatRows() {
  const body = $("chats-body");
  body.replaceChildren();
  const chats = state.settings.chats;
  if (!chats.length) {
    body.append(
      el("tr", {}, [
        el("td", { colspan: "3", class: "empty" }, [
          el("span", { text: "Ни одного чата. Добавь вручную или загрузи беседы из ВК." }),
        ]),
      ])
    );
    return;
  }
  chats.forEach((chat, index) => {
    const name = el("input", { type: "text", value: chat.name, placeholder: "Название беседы" });
    name.addEventListener("input", () => {
      chats[index].name = name.value;
    });
    const peer = el("input", {
      type: "text",
      value: chat.peer_id ?? "",
      placeholder: "—",
      inputmode: "numeric",
    });
    peer.addEventListener("input", () => {
      chats[index].peer_id = peer.value.trim();
    });
    peer.addEventListener("paste", (event) => {
      const found = extractPeerId(event.clipboardData.getData("text"));
      if (!found) return;
      event.preventDefault();
      peer.value = found;
      chats[index].peer_id = found;
      status("chat-diag", `Из ссылки взят peer_id ${found}`, "ok");
    });
    const up = iconButton("up", "Выше в списке", () => moveChat(index, -1));
    const down = iconButton("down", "Ниже в списке", () => moveChat(index, 1));
    const remove = iconButton("x", "Убрать чат", () => {
      chats.splice(index, 1);
      renderChatRows();
    }, "danger");
    const ping = iconButton("mail", "Отправить одно тестовое сообщение в эту беседу", (event) =>
      testSendToChat(event.currentTarget, peer.value)
    );
    up.disabled = index === 0;
    down.disabled = index === chats.length - 1;
    body.append(
      el("tr", {}, [
        el("td", {}, [name]),
        el("td", { class: "peer" }, [peer]),
        el("td", { class: "acts" }, [up, down, ping, remove]),
      ])
    );
  });
}

async function testSendToChat(button, peerRaw) {
  const mode = $("set-mode").value;
  const token = readTokenField(mode === "community" ? "set-token-community" : "set-token-user");
  status("chat-diag", "Отправляю одно тестовое сообщение…");
  button.disabled = true;
  try {
    const data = await post("/api/vk/chat/send-test", { mode, token, peer_id: peerRaw });
    if (data.ok) status("chat-diag", `Отправлено, id сообщения ${data.message_id}. Проверь беседу.`, "ok");
    else status("chat-diag", data.error || "Отправка не прошла", "bad");
  } catch (err) {
    status("chat-diag", String(err), "bad");
  } finally {
    button.disabled = false;
  }
}

function vkStatus(text, kind) {
  status("vk-status", text, kind || "");
}

function providerPreset(id) {
  return state.settings.llm.providers.find((item) => item.id === id) || {};
}

function applyProviderPreset() {
  const preset = providerPreset($("set-llm-provider").value);
  $("set-llm-url").value = preset.base_url || "";
  const key = $("set-llm-key");
  key.dataset.real = preset.api_key || "";
  key.dataset.masked = "";
  key.value = preset.api_key || "";
  useIcon("show-llm-key", "eye");
  $("show-llm-key").title = "Показать ключ";
  $("llm-models").replaceChildren();
  status("llm-status", "");
}

function readLlmFields() {
  return {
    provider: $("set-llm-provider").value,
    base_url: $("set-llm-url").value.trim(),
    api_key: readTokenField("set-llm-key"),
    model: $("set-llm-model").value.trim(),
    temperature: $("set-llm-temp").value,
    timeout_seconds: $("set-llm-timeout").value,
  };
}

const llmStatus = (text, kind) => status("llm-status", text, kind || "");

async function checkLlm() {
  const draft = readLlmFields();
  llmStatus("Проверяю…");
  const data = await post("/api/llm/check", { llm: draft });
  if (!data.ok) {
    llmStatus(`Не подключается: ${data.error}`, "bad");
    return;
  }
  $("llm-models").replaceChildren(...data.models.map((id) => el("option", { value: id })));
  const head = `Подключение есть, моделей: ${data.models.length}`;
  if (!data.models.length) llmStatus(`${head}. Список пуст — впиши имя модели вручную.`, "warn");
  else if (data.model_found) llmStatus(`${head}. Модель «${draft.model}» на месте.`, "ok");
  else llmStatus(`${head}, но модели «${draft.model}» среди них нет.`, "warn");
}

async function openSettings() {
  const data = await api("/api/settings");
  state.settings = {
    mode: data.mode,
    options: data.options,
    vk: data.vk,
    chats: data.chats,
    llm: data.llm,
  };
  const select = $("set-mode");
  select.replaceChildren(
    ...data.options.map((value) => el("option", { value, text: MODE_LABELS[value] || value }))
  );
  select.value = data.mode;
  $("set-version").value = data.vk.api_version;
  for (const [inputId, buttonId, token] of [
    ["set-token-user", "show-token-user", data.vk.user_token],
    ["set-token-community", "show-token-community", data.vk.community_token],
    ["set-llm-key", "show-llm-key", data.llm.api_key],
  ]) {
    setTokenField(inputId, token);
    useIcon(buttonId, "eye");
    $(buttonId).title = "Показать";
  }

  const provider = $("set-llm-provider");
  provider.replaceChildren(
    ...data.llm.providers.map((item) => el("option", { value: item.id, text: item.label }))
  );
  provider.value = data.llm.provider;
  $("set-llm-url").value = data.llm.base_url;
  $("set-llm-model").value = data.llm.model;
  $("set-llm-temp").value = data.llm.temperature;
  $("set-llm-timeout").value = data.llm.timeout_seconds;
  $("llm-models").replaceChildren();
  llmStatus("");

  renderChatRows();
  vkStatus("");
  $("dlg-settings").showModal();
}

async function saveSettings() {
  const data = await post("/api/settings", {
    mode: $("set-mode").value,
    vk: {
      user_token: readTokenField("set-token-user"),
      community_token: readTokenField("set-token-community"),
      api_version: $("set-version").value.trim(),
    },
    llm: readLlmFields(),
    chats: state.settings.chats,
  });
  state.settings.chats = data.chats;
  state.settings.llm = data.llm;
  state.pickedChats = new Set();
  $("dlg-settings").close();
  await loadState();
  const providerLabel = data.llm.providers.find((p) => p.id === data.llm.provider)?.label;
  if (data.warnings && data.warnings.length) {
    toast("Сохранено, но проверь", "warn", data.warnings.join("\n"));
  } else {
    toast(
      `Настройки сохранены в config.toml: ИИ ${providerLabel || data.llm.provider} (${data.llm.model}), ` +
        `режим ${data.mode}, чатов ${data.chats.length}`,
      "ok"
    );
  }
}

async function checkVkToken() {
  const mode = $("set-mode").value === "community" ? "community" : "user_token";
  const inputId = mode === "community" ? "set-token-community" : "set-token-user";
  vkStatus("Проверяю токен…");
  const data = await post("/api/vk/check", { mode, token: readTokenField(inputId) });
  if (!data.ok) {
    vkStatus(`Токен не работает: ${data.error}`, "bad");
    return;
  }
  const kind = mode === "community" ? "сообщество" : "пользователь";
  const lines = [`Токен живой: ${data.name} (${kind} id ${data.id})`];
  if (data.messages_ok) {
    lines.push(
      data.chats_count
        ? `право messages есть, видно бесед: ${data.chats_count}`
        : "право messages есть. ВК не отдаёт список бесед, куда сообщество добавлено, " +
          "так и должно быть. peer_id беседы бери из ссылки в браузере: открой беседу, " +
          "скопируй адрес вида vk.com/im?sel=2000000000 и вставь его в поле peer_id"
    );
  } else {
    lines.push(`права messages нет: ${data.messages_error}`);
    if (data.hint) lines.push(data.hint);
  }
  vkStatus(lines.join(". "), data.messages_ok ? "ok" : "bad");
}

async function loadVkChats() {
  const mode = $("set-mode").value === "community" ? "community" : "user_token";
  const inputId = mode === "community" ? "set-token-community" : "set-token-user";
  vkStatus("Загружаю беседы…");
  const data = await post("/api/vk/conversations", { mode, token: readTokenField(inputId) });
  if (!data.ok) {
    vkStatus(`Беседы не загрузились: ${data.error}`, "bad");
    return;
  }
  state.vkChatItems = data.items;
  state.vkPicked = new Set();
  const list = $("vk-chats");
  list.replaceChildren();
  if (!data.items.length) {
    list.append(el("li", { class: "empty", text: "Бесед не нашлось" }));
  }
  for (const item of data.items) {
    const box = el("input", { type: "checkbox" });
    box.disabled = item.in_list;
    box.addEventListener("change", () => {
      if (box.checked) state.vkPicked.add(item.peer_id);
      else state.vkPicked.delete(item.peer_id);
    });
    list.append(
      el("li", {}, [
        box,
        el("label", { text: `${item.title} — peer_id ${item.peer_id}` }),
        item.in_list ? el("span", { class: "badge plain", text: "уже в рассылке" }) : null,
      ])
    );
  }
  vkStatus(
    data.items.length
      ? `Загружено бесед: ${data.items.length}`
      : mode === "community"
        ? "Бесед 0 — сообщество не состоит ни в одной беседе, кроме созданных им самих. " +
          "Добавь его в каждую родительскую беседу: Управление сообществом → Сообщения → " +
          "Настройки для бота → «Возможности ботов» + «Разрешать добавлять сообщество в беседы», " +
          "затем в меню сообщества «Пригласить в беседу». После этого беседа появится здесь."
        : "Бесед не видно. Проверь, что токен видит диалоги, и добавь беседы вручную.",
    data.items.length ? "ok" : "bad"
  );
  $("dlg-chats").showModal();
}

function addVkChats() {
  const chats = state.settings.chats;
  let added = 0;
  for (const item of state.vkChatItems) {
    if (!state.vkPicked.has(item.peer_id)) continue;
    const exists = chats.some(
      (chat) => Number(chat.peer_id) === item.peer_id || chat.name === item.title
    );
    if (exists) continue;
    chats.push({ name: item.title, peer_id: item.peer_id });
    added += 1;
  }
  state.vkPicked = new Set();
  $("dlg-chats").close();
  renderChatRows();
  toast(added ? `Добавлено чатов: ${added}` : "Все отмеченные уже были в рассылке", "ok");
}

function wireDrop(dropId, inputId, files) {
  const drop = $(dropId);
  const input = $(inputId);
  drop.addEventListener("click", () => input.click());
  input.addEventListener("change", () => files(input.files));
  ["dragenter", "dragover"].forEach((type) =>
    drop.addEventListener(type, (event) => {
      event.preventDefault();
      drop.classList.add("over");
    })
  );
  ["dragleave", "drop"].forEach((type) =>
    drop.addEventListener(type, () => drop.classList.remove("over"))
  );
  drop.addEventListener("drop", (event) => {
    event.preventDefault();
    files(event.dataTransfer.files);
  });
}

/* --- история --- */

async function openHistory() {
  state.history = { page: 1, pages: 1, total: 0, search: $("history-search").value.trim() };
  $("dlg-history").showModal();
  await loadHistory();
}

async function loadHistory() {
  const { page, search } = state.history;
  const query = new URLSearchParams({ page: String(page), size: String(HISTORY_SIZE), search });
  const data = await api(`/api/history?${query}`);
  state.history = { page: data.page, pages: data.pages, total: data.total, search };
  renderHistory(data);
}

async function gotoHistory(page) {
  state.history.page = page;
  try {
    await loadHistory();
  } catch (err) {
    toast(err.message, "bad");
  }
}

async function loadHistoryEntry(item) {
  const saved = await api(`/api/history/read?name=${encodeURIComponent(item.name)}`);
  saveDraft();
  $("message").value = saved.text;
  state.title = item.title;
  state.current = null;
  renderFacts(null);
  syncActions();
  $("dlg-history").close();
  $("btn-draft-restore").hidden = !readDraft();
  status("gen-status", "Текст загружен из истории", "ok");
}

async function deleteHistoryEntry(item) {
  const ok = await confirmDialog(
    "Удалить запись?",
    `«${item.title}» (${item.saved_at}) исчезнет из history/ безвозвратно.`,
    "Удалить"
  );
  if (!ok) return;
  await post("/api/history/delete", { name: item.name });
  // С последней записи единственной страницы падаем на предыдущую, иначе получим пустой список.
  if (state.history.page > state.history.pages && state.history.pages > 1) {
    state.history.page -= 1;
  }
  await loadHistory();
  toast(`Удалено: history/${item.name}`, "ok");
}

function pageWindow(page, pages) {
  if (pages <= 7) return Array.from({ length: pages }, (_, i) => i + 1);
  const wanted = new Set([1, pages, page, page - 1, page + 1]);
  const out = [];
  for (let n = 1; n <= pages; n += 1) {
    if (wanted.has(n)) out.push(n);
    else if (out[out.length - 1] !== "…") out.push("…");
  }
  return out;
}

function renderHistoryPager(data) {
  const pager = $("history-pager");
  pager.replaceChildren();
  if (data.pages < 2) return;
  const go = (page) => el("button", { type: "button", text: String(page), onclick: () => gotoHistory(page) });
  pager.append(el("button", { type: "button", text: "← Назад", disabled: data.page <= 1, onclick: () => gotoHistory(data.page - 1) }));
  for (const page of pageWindow(data.page, data.pages)) {
    if (page === "…") {
      pager.append(el("span", { class: "gap", text: "…" }));
      continue;
    }
    const button = go(page);
    if (page === data.page) button.classList.add("on");
    pager.append(button);
  }
  pager.append(el("button", { type: "button", text: "Вперёд →", disabled: data.page >= data.pages, onclick: () => gotoHistory(data.page + 1) }));
}

function renderHistory(data) {
  const list = $("history-list");
  list.replaceChildren();
  if (!data.items.length) {
    list.append(el("li", { class: "empty", text: data.search ? "Ничего не найдено" : "История пуста" }));
  }
  for (const item of data.items) {
    const open = el(
      "button",
      {
        type: "button",
        class: "open",
        title: "Загрузить текст в редактор",
        onclick: () => loadHistoryEntry(item).catch((err) => toast(err.message, "bad")),
      },
      [
        el("span", { class: "list-row-name", text: `${item.title} — ${item.saved_at}` }),
      ]
    );
    const del = iconButton("trash", "Удалить запись", () =>
      deleteHistoryEntry(item).catch((err) => toast(err.message, "bad"))
    , "danger");
    list.append(el("li", {}, [open, del]));
  }
  $("history-count").textContent =
    `Записей: ${data.total}` + (data.pages > 1 ? `, страница ${data.page} из ${data.pages}` : "");
  renderHistoryPager(data);
}

/* --- горячие клавиши --- */

function openKeys() {
  $("dlg-keys").showModal();
}

document.addEventListener("keydown", (event) => {
  const ctrl = event.ctrlKey || event.metaKey;
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName);
  if (ctrl && event.key === "Enter") {
    event.preventDefault();
    if (event.shiftKey) {
      if (!$("btn-regenerate").disabled) $("btn-regenerate").click();
    } else if (!$("btn-send").disabled) $("btn-send").click();
    return;
  }
  if (ctrl && !event.shiftKey && event.key.toLowerCase() === "s") {
    event.preventDefault();
    if (!$("btn-save").disabled) $("btn-save").click();
    return;
  }
  if (ctrl && event.key.toLowerCase() === "k") {
    event.preventDefault();
    selectPanel("tab-lessons");
    $("lesson-search").focus();
    $("lesson-search").select();
    return;
  }
  if (ctrl && event.key.toLowerCase() === "h") {
    event.preventDefault();
    $("btn-history").click();
    return;
  }
  if (event.key === "?" && !typing) {
    event.preventDefault();
    openKeys();
  }
});

/* --- события --- */

$("btn-theme").addEventListener("click", () => {
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
});

$("btn-generate-batch").addEventListener("click", generateBatch);

$("btn-regenerate").addEventListener("click", () => {
  if (state.current && !state.current.includes(",")) generateOne(state.current, $("btn-regenerate"));
  else toast("Пакетное сообщение перегенерировать целиком нельзя — отметь одну методичку слева", "warn");
});

$("btn-draft-restore").addEventListener("click", () => {
  if (restoreDraft()) toast("Черновик возвращён в редактор", "ok");
});

$("btn-copy").addEventListener("click", () =>
  guard($("btn-copy"), async () => {
    const data = await post("/api/copy", { text: $("message").value });
    toast(data.detail, data.ok ? "ok" : "bad");
  })
);

$("btn-save").addEventListener("click", () =>
  guard($("btn-save"), async () => {
    const data = await post("/api/save", { title: state.title, text: $("message").value });
    dropDraft();
    toast(`Сохранено: history/${data.name}`, "ok");
  })
);

$("btn-send").addEventListener("click", () =>
  guard($("btn-send"), async () => {
    const data = await post("/api/send", {
      text: $("message").value,
      title: state.title,
      chats: [...state.pickedChats],
      photos: [...state.pickedPhotos],
    });
    dropDraft();
    renderResults(data);
    const bad = data.results.filter((item) => !item.ok).length;
    toast(
      bad ? `Отправлено с ошибками: ${data.results.length - bad} из ${data.results.length}` : `Отправлено: ${data.results.length}`,
      bad ? "warn" : "ok",
      data.results.filter((item) => !item.ok).map((item) => `${item.name}: ${item.detail}`).join("\n")
    );
  }, "send-summary")
);

$("message").addEventListener("input", () => {
  syncActions();
  scheduleDraft();
});

$("btn-style").addEventListener("click", () =>
  guard(null, async () => {
    const data = await api("/api/style");
    $("style-text").value = data.text;
    $("dlg-style").showModal();
  })
);

$("btn-style-save").addEventListener("click", () =>
  guard($("btn-style-save"), async () => {
    await post("/api/style", { text: $("style-text").value });
    toast("Стиль сохранён", "ok");
  })
);

$("btn-style-cancel").addEventListener("click", () => $("dlg-style").close());

$("btn-history").addEventListener("click", () => guard(null, openHistory));
$("btn-history-search").addEventListener("click", () => gotoHistory(1));
$("btn-history-reset").addEventListener("click", () => {
  $("history-search").value = "";
  gotoHistory(1);
});
$("history-search").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    gotoHistory(1);
  }
});
$("btn-history-close").addEventListener("click", () => $("dlg-history").close());
$("btn-keys-close").addEventListener("click", () => $("dlg-keys").close());

$("btn-photo-view-close").addEventListener("click", () => $("dlg-photo-view").close());

$("lesson-search").addEventListener("input", renderLessons);
$("photo-search").addEventListener("input", renderPhotos);

$("btn-lessons-all").addEventListener("click", () => {
  const all = [...state.lessons].sort((a, b) => a.localeCompare(b, "ru"));
  const shown = filterNames(all, $("lesson-search").value);
  const allPicked = shown.length > 0 && shown.every((name) => state.pickedLessons.has(name));
  for (const name of shown) {
    if (allPicked) state.pickedLessons.delete(name);
    else state.pickedLessons.add(name);
  }
  $("btn-lessons-all").textContent = allPicked ? "Снять все" : "Выбрать все";
  renderLessons();
  syncActions();
});

$("btn-photos-all").addEventListener("click", () => {
  const all = [...state.photos].sort((a, b) => b.localeCompare(a, "ru"));
  const shown = filterNames(all, $("photo-search").value);
  const room = MAX_PHOTOS - state.pickedPhotos.size;
  const toAdd = shown.filter((name) => !state.pickedPhotos.has(name));
  if (toAdd.length > room) {
    toast(`ВК принимает ${MAX_PHOTOS} фото за раз — отмечено ещё ${room}`, "warn");
  }
  for (const name of toAdd.slice(0, room)) state.pickedPhotos.add(name);
  renderPhotos();
});

$("btn-photos-none").addEventListener("click", () => {
  state.pickedPhotos.clear();
  renderPhotos();
});

$("btn-settings").addEventListener("click", () => guard(null, openSettings));

$("btn-settings-save").addEventListener("click", () =>
  guard($("btn-settings-save"), async () => {
    await saveSettings();
  })
);

$("btn-settings-cancel").addEventListener("click", () => $("dlg-settings").close());

$("btn-vk-check").addEventListener("click", () =>
  guard($("btn-vk-check"), async () => {
    await checkVkToken();
  }, "vk-status")
);

$("btn-vk-load").addEventListener("click", () =>
  guard($("btn-vk-load"), async () => {
    await loadVkChats();
  }, "vk-status")
);

$("btn-chat-add").addEventListener("click", () => {
  state.settings.chats.push({ name: "", peer_id: "" });
  renderChatRows();
  const rows = $("chats-body").querySelectorAll("input");
  const last = rows[rows.length - 2];
  if (last) last.focus();
});

$("btn-chats-add").addEventListener("click", addVkChats);
$("btn-chats-close").addEventListener("click", () => $("dlg-chats").close());

wireTokenField("set-token-user", "show-token-user");
wireTokenField("set-token-community", "show-token-community");
wireTokenField("set-llm-key", "show-llm-key");

$("set-llm-provider").addEventListener("change", applyProviderPreset);

$("btn-llm-check").addEventListener("click", () =>
  guard($("btn-llm-check"), async () => {
    try {
      await checkLlm();
    } catch (err) {
      llmStatus(err.message, "bad");
    }
  }, "llm-status")
);

wireDrop("drop-lesson", "file-lesson", (files) => upload(files));
wireDrop("drop-photo", "file-photo", (files) => upload(files));

initTabs($("tablist-left"));
initTabs($("tablist-settings"));
applyTheme(document.documentElement.dataset.theme || "light");

loadState()
  .then(() => {
    if ($("message").value.trim()) return;
    if (restoreDraft(true)) toast("Восстановлен черновик из прошлой сессии", "ok");
  })
  .catch((err) => toast(err.message, "bad"));