"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  transport: { mode: "clipboard", supports_photos: false, ready: true },
  chats: [],
  lessons: new Set(),
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

function extractPeerId(text) {
  const runs = String(text).match(/\d{7,}/g) || [];
  return runs.find((run) => run.startsWith("2")) || runs[0] || null;
}

function notice(message, kind = "bad") {
  const box = $("notice");
  box.textContent = message || "";
  box.className = message ? `notice ${kind}` : "notice";
  box.hidden = !message;
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

function busy(button, active, label) {
  if (!button) return;
  if (active) {
    button.dataset.was = button.textContent;
    button.textContent = label;
    button.disabled = true;
  } else {
    button.textContent = button.dataset.was || button.textContent;
    button.disabled = false;
  }
}

async function guard(button, label, action) {
  try {
    notice("");
    busy(button, true, label);
    await action();
  } catch (err) {
    notice(err.message);
  } finally {
    busy(button, false);
  }
}

function filterNames(names, query) {
  const needle = query.trim().toLowerCase();
  if (!needle) return names;
  return names.filter((name) => name.toLowerCase().includes(needle));
}

function renderLessons() {
  const list = $("list-lessons");
  const all = [...state.lessons].sort((a, b) => a.localeCompare(b, "ru"));
  const shown = filterNames(all, $("lesson-search").value);
  list.replaceChildren();
  if (!shown.length) {
    list.append(el("li", { class: "empty", text: all.length ? "Ничего не найдено" : "Методичек пока нет" }));
  }
  for (const name of shown) {
    const box = el("input", { type: "checkbox" });
    box.checked = state.pickedLessons.has(name);
    box.addEventListener("change", () => {
      if (box.checked) state.pickedLessons.add(name);
      else state.pickedLessons.delete(name);
      syncActions();
    });
    list.append(el("li", {}, [box, el("label", { text: name })]));
  }
  $("lesson-count").textContent = shown.length === all.length ? `${all.length} шт.` : `${shown.length} из ${all.length}`;
}

const photoUrl = (name) => `/api/photo?name=${encodeURIComponent(name)}`;
const thumbUrl = (name) => `/api/photo/thumb?name=${encodeURIComponent(name)}`;

function openPhotoView(name) {
  $("photo-view-img").src = photoUrl(name);
  $("photo-view-title").textContent = name;
  $("dlg-photo-view").showModal();
}

async function deletePhoto(name) {
  if (!confirm(`Удалить фото ${name}? Файл исчезнет из photos/ безвозвратно.`)) return;
  try {
    await post("/api/photo/delete", { name });
    state.photos.delete(name);
    state.pickedPhotos.delete(name);
    renderPhotos();
    syncActions();
    notice(`Удалено: ${name}`, "ok");
  } catch (err) {
    notice(err.message);
  }
}

function photoCard(name) {
  const box = el("input", { type: "checkbox" });
  box.checked = state.pickedPhotos.has(name);
  box.addEventListener("change", () => {
    if (box.checked) state.pickedPhotos.add(name);
    else state.pickedPhotos.delete(name);
  });
  const img = el("img", {
    src: thumbUrl(name),
    alt: name,
    loading: "lazy",
    decoding: "async",
    title: "Показать крупно",
  });
  img.addEventListener("click", () => openPhotoView(name));
  const del = el("button", {
    type: "button",
    class: "del",
    text: "✕",
    title: "Удалить фото",
    onclick: () => deletePhoto(name),
  });
  return el("li", { class: "card" }, [
    el("div", { class: "thumb" }, [img]),
    el("p", { class: "card-name", text: name, title: name }),
    el("div", { class: "card-row" }, [box, el("label", { text: "в сообщение" }), del]),
  ]);
}

function renderPhotos() {
  const list = $("list-photos");
  const all = [...state.photos].sort((a, b) => b.localeCompare(a, "ru"));
  const shown = filterNames(all, $("photo-search").value);
  list.replaceChildren();
  if (!shown.length) {
    list.append(
      el("li", { class: "empty", text: all.length ? "Ничего не найдено" : "Фото не выбраны" })
    );
  }
  for (const name of shown) list.append(photoCard(name));
  $("photo-count").textContent = shown.length === all.length ? `${all.length} шт.` : `${shown.length} из ${all.length}`;
  $("photo-hint").textContent = state.transport.supports_photos
    ? "Выбранные фото прикрепляются к сообщению при отправке через API. Клик по фото — предпросмотр, ✕ — удалить."
    : "Режим clipboard: фото придётся прикрепить в ВК вручную.";
}

function renderChats() {
  const list = $("list-chats");
  list.replaceChildren();
  if (!state.chats.length) {
    list.append(el("li", { class: "empty", text: "Чаты не заданы — открой «Настройки ВК и рассылки»" }));
    return;
  }
  for (const chat of state.chats) {
    const box = el("input", { type: "checkbox" });
    box.checked = state.pickedChats.has(chat.name);
    box.addEventListener("change", () => {
      if (box.checked) state.pickedChats.add(chat.name);
      else state.pickedChats.delete(chat.name);
      syncActions();
    });
    const label = el("label", { text: chat.name });
    list.append(
      el("li", {}, [
        box,
        label,
        chat.api_capable ? null : el("span", { class: "badge", text: "без peer_id" }),
      ])
    );
  }
}

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
  for (const text of badges) box.append(el("span", { class: "badge", text }));
}

function syncActions() {
  const hasText = $("message").value.trim().length > 0;
  $("btn-generate").disabled = state.lessons.size !== 1;
  $("btn-generate-batch").disabled = state.pickedLessons.size === 0;
  $("btn-regenerate").disabled = !state.current;
  $("btn-copy").disabled = !hasText;
  $("btn-save").disabled = !hasText;
  $("btn-send").disabled = !hasText || state.pickedChats.size === 0;
}

async function loadState() {
  const data = await api("/api/state");
  state.transport = data.transport;
  state.chats = data.chats;
  state.lessons = new Set(data.lessons);
  state.photos = new Set(data.photos);
  state.pickedPhotos = new Set(data.photos);
  $("badge-transport").textContent =
    `транспорт: ${data.transport.mode}` +
    (data.transport.mode === "clipboard" ? "" : data.transport.ready ? " ✓" : " (нет токена)");
  $("badge-llm").textContent = `${data.llm.model} @ ${data.llm.base_url}`;
  renderLessons();
  renderPhotos();
  renderChats();
  syncActions();
}

async function upload(files) {
  const picked = [...files];
  if (!picked.length) return;
  const form = new FormData();
  for (const file of picked) form.append("file", file, file.name);
  await guard($("drop-lesson"), "Загрузка…", async () => {
    await api("/api/upload", { method: "POST", body: form });
    await loadState();
    notice("");
  });
}

async function generate(name) {
  const data = await post("/api/generate", { name });
  $("message").value = data.text;
  state.current = data.name;
  state.title = `${data.lesson.code} ${data.lesson.title}`.trim() || data.name;
  renderFacts(data.lesson);
  syncActions();
}

function renderResults(results) {
  const list = $("results");
  list.replaceChildren();
  for (const item of results) {
    list.append(
      el("li", { class: item.ok ? "ok" : "bad" }, [
        el("label", {}, [
          document.createTextNode(item.name),
          el("span", { class: "detail", text: item.detail }),
        ]),
      ])
    );
  }
}

/* --- Настройки ВК и рассылки --- */

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
    button.textContent = masked ? "Скрыть" : "Показать";
  });
}

function moveChat(index, step) {
  const target = index + step;
  if (target < 0 || target >= state.settings.chats.length) return;
  const list = state.settings.chats;
  [list[index], list[target]] = [list[target], list[index]];
  renderChatRows();
}

function renderChatRows() {
  const body = $("chats-body");
  body.replaceChildren();
  const chats = state.settings.chats;
  if (!chats.length) {
    const row = el("tr");
    const cell = el("td", { colspan: "3", class: "empty" });
    cell.append(el("span", { text: "Ни одного чата. Добавь вручную или загрузи беседы из ВК." }));
    row.append(cell);
    body.append(row);
    return;
  }
  chats.forEach((chat, index) => {
    const name = el("input", { type: "text", value: chat.name });
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
      vkStatus(`Из ссылки взят peer_id ${found}`, "ok");
    });
    const up = el("button", { type: "button", text: "↑", onclick: () => moveChat(index, -1) });
    const down = el("button", { type: "button", text: "↓", onclick: () => moveChat(index, 1) });
    const remove = el("button", {
      type: "button",
      text: "✕",
      onclick: () => {
        chats.splice(index, 1);
        renderChatRows();
      },
    });
    const ping = el("button", {
      type: "button",
      text: "✉",
      title: "Отправить одно тестовое сообщение в эту беседу",
      onclick: (event) => testSendToChat(event.currentTarget, peer.value),
    });
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
  const box = $("chat-diag");
  const mode = $("set-mode").value;
  const token = readTokenField(mode === "community" ? "set-token-community" : "set-token-user");
  box.textContent = "Отправляю одно тестовое сообщение…";
  box.className = "hint";
  box.style.whiteSpace = "pre-line";
  button.disabled = true;
  try {
    const data = await post("/api/vk/chat/send-test", { mode, token, peer_id: peerRaw });
    box.textContent = data.ok
      ? `Отправлено, id сообщения ${data.message_id}. Проверь беседу.`
      : data.error || "Отправка не прошла";
    box.className = `hint ${data.ok ? "ok" : "bad"}`;
  } catch (err) {
    box.textContent = String(err);
    box.className = "hint bad";
  } finally {
    button.disabled = false;
  }
}

function vkStatus(text, kind) {
  const box = $("vk-status");
  box.textContent = text || "";
  box.className = kind ? `hint ${kind}` : "hint";
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
  $("show-llm-key").textContent = "Показать";
  $("llm-models").replaceChildren();
  llmStatus("");
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

function llmStatus(text, kind) {
  const box = $("llm-status");
  box.textContent = text || "";
  box.className = kind ? `hint ${kind}` : "hint";
}

async function checkLlm() {
  const draft = readLlmFields();
  llmStatus("Проверяю…");
  const data = await post("/api/llm/check", { llm: draft });
  if (!data.ok) {
    llmStatus(`Не подключается: ${data.error}`, "bad");
    return;
  }
  $("llm-models").replaceChildren(
    ...data.models.map((id) => el("option", { value: id }))
  );
  const head = `Подключение есть, моделей: ${data.models.length}`;
  if (!data.models.length) llmStatus(`${head}. Список пуст — впиши имя модели вручную.`, "bad");
  else if (data.model_found) llmStatus(`${head}. Модель «${draft.model}» на месте.`, "ok");
  else llmStatus(`${head}, но модели «${draft.model}» среди них нет.`, "bad");
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
  setTokenField("set-token-user", data.vk.user_token);
  setTokenField("set-token-community", data.vk.community_token);
  $("show-token-user").textContent = "Показать";
  $("show-token-community").textContent = "Показать";

  const provider = $("set-llm-provider");
  provider.replaceChildren(
    ...data.llm.providers.map((item) => el("option", { value: item.id, text: item.label }))
  );
  provider.value = data.llm.provider;
  $("set-llm-url").value = data.llm.base_url;
  $("set-llm-model").value = data.llm.model;
  $("set-llm-temp").value = data.llm.temperature;
  $("set-llm-timeout").value = data.llm.timeout_seconds;
  setTokenField("set-llm-key", data.llm.api_key);
  $("show-llm-key").textContent = "Показать";
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
    notice(`Сохранено, но проверь: ${data.warnings.join("; ")}`, "bad");
  } else {
    notice(
      `Сохранено в config.toml: ИИ ${providerLabel || data.llm.provider} ` +
        `(${data.llm.model}), режим ${data.mode}, чатов ${data.chats.length}`,
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
  const data = await post("/api/vk/conversations", {
    mode,
    token: readTokenField(inputId),
  });
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
        item.in_list ? el("span", { class: "badge", text: "уже в рассылке" }) : null,
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
  notice(
    added ? `Добавлено чатов: ${added}` : "Все отмеченные уже были в рассылке",
    "ok"
  );
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

$("btn-generate").addEventListener("click", () =>
  guard($("btn-generate"), "Генерация…", async () => {
    await generate([...state.lessons][0]);
  })
);

$("btn-generate-batch").addEventListener("click", () =>
  guard($("btn-generate-batch"), "Генерация…", async () => {
    const names = [...state.pickedLessons];
    const data = await post("/api/generate_batch", { names });
    const failed = data.items.filter((item) => item.error);
    const done = data.items.filter((item) => !item.error);
    if (done.length) {
      $("message").value = done.map((item) => item.text).join("\n\n\n");
      state.current = done.map((item) => item.name).join(", ");
      state.title = done.map((item) => item.title).join("; ") || "lesson";
      renderFacts(done[0].lesson);
    }
    if (failed.length) notice(failed.map((item) => `${item.name}: ${item.error}`).join("\n"));
    syncActions();
  })
);

$("btn-regenerate").addEventListener("click", () =>
  guard($("btn-regenerate"), "Перегенерирую…", async () => {
    await generate(state.current);
  })
);

$("btn-copy").addEventListener("click", () =>
  guard($("btn-copy"), "Копирую…", async () => {
    const data = await post("/api/copy", { text: $("message").value });
    notice(data.detail, data.ok ? "ok" : "bad");
  })
);

$("btn-save").addEventListener("click", () =>
  guard($("btn-save"), "Сохраняю…", async () => {
    const data = await post("/api/save", { title: state.title, text: $("message").value });
    notice(`Сохранено: history/${data.name}`, "ok");
  })
);

$("btn-send").addEventListener("click", () =>
  guard($("btn-send"), "Отправляю…", async () => {
    const data = await post("/api/send", {
      text: $("message").value,
      title: state.title,
      chats: [...state.pickedChats],
      photos: [...state.pickedPhotos],
    });
    renderResults(data.results);
  })
);

$("message").addEventListener("input", syncActions);

$("btn-style").addEventListener("click", () =>
  guard(null, "", async () => {
    const data = await api("/api/style");
    $("style-text").value = data.text;
    $("dlg-style").showModal();
  })
);

$("btn-style-save").addEventListener("click", () =>
  guard(null, "", async () => {
    await post("/api/style", { text: $("style-text").value });
    notice("Стиль сохранён", "ok");
  })
);

$("btn-style-cancel").addEventListener("click", () => $("dlg-style").close());

/* --- История: поиск, пагинация, удаление --- */

const HISTORY_SIZE = 20;

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
    notice(err.message);
  }
}

async function loadHistoryEntry(item) {
  const saved = await api(`/api/history/read?name=${encodeURIComponent(item.name)}`);
  $("message").value = saved.text;
  state.title = item.title;
  state.current = null;
  renderFacts(null);
  syncActions();
  $("dlg-history").close();
}

async function deleteHistoryEntry(item) {
  if (!confirm(`Удалить запись «${item.title}» (${item.saved_at})? Файл исчезнет из history/ безвозвратно.`)) return;
  await post("/api/history/delete", { name: item.name });
  // С последней записи единственной страницы падаем на предыдущую, иначе получим пустой список.
  if (state.history.page > state.history.pages && state.history.pages > 1) {
    state.history.page -= 1;
  }
  await loadHistory();
  notice(`Удалено: history/${item.name}`, "ok");
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
    const open = el("button", {
      type: "button",
      class: "open",
      text: `${item.title} — ${item.saved_at}`,
      title: "Загрузить текст в редактор",
      onclick: () => loadHistoryEntry(item).catch((err) => notice(err.message)),
    });
    const del = el("button", {
      type: "button",
      class: "del",
      text: "✕",
      title: "Удалить запись",
      onclick: () => deleteHistoryEntry(item).catch((err) => notice(err.message)),
    });
    list.append(el("li", {}, [open, del]));
  }
  $("history-count").textContent =
    `Записей: ${data.total}` + (data.pages > 1 ? `, страница ${data.page} из ${data.pages}` : "");
  renderHistoryPager(data);
}

$("btn-history").addEventListener("click", () => guard(null, "", openHistory));

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

$("btn-photo-view-close").addEventListener("click", () => $("dlg-photo-view").close());

$("lesson-search").addEventListener("input", renderLessons);
$("photo-search").addEventListener("input", renderPhotos);

$("btn-history-close").addEventListener("click", () => $("dlg-history").close());

$("btn-settings").addEventListener("click", () =>
  guard(null, "", async () => {
    await openSettings();
  })
);

$("btn-settings-save").addEventListener("click", () =>
  guard($("btn-settings-save"), "Сохраняю…", async () => {
    await saveSettings();
  })
);

$("btn-settings-cancel").addEventListener("click", () => $("dlg-settings").close());

$("btn-vk-check").addEventListener("click", () =>
  guard($("btn-vk-check"), "Проверяю…", async () => {
    await checkVkToken();
  })
);

$("btn-vk-load").addEventListener("click", () =>
  guard($("btn-vk-load"), "Загружаю…", async () => {
    await loadVkChats();
  })
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
  guard($("btn-llm-check"), "Проверяю…", async () => {
    try {
      await checkLlm();
    } catch (err) {
      llmStatus(err.message, "bad");
    }
  })
);

wireDrop("drop-lesson", "file-lesson", (files) => upload(files));
wireDrop("drop-photo", "file-photo", (files) => upload(files));

loadState().catch((err) => notice(err.message));