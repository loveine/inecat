const $ = (s) => document.querySelector(s);
let user = null, csrfToken = null, offset = 0, mode = "login";
const gallery = $("#gallery");

async function api(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (csrfToken && ["POST", "PATCH", "DELETE"].includes((options.method || "GET").toUpperCase())) headers.set("X-CSRF-Token", csrfToken);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(url, {...options, headers, credentials: "same-origin"});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `请求失败 (${response.status})`);
  return body;
}

function notice(text = "") { $("#notice").textContent = text; }
function element(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }

async function refreshUser() {
  const data = await api("/api/me"); user = data.user;
  $("#identity").textContent = user ? `你好，${user.username}${user.is_admin ? "（管理员）" : ""}` : "访客浏览";
  $("#auth-form").classList.toggle("hidden", !!user);
  $(".tabs").classList.toggle("hidden", !!user);
  $("#logout").classList.toggle("hidden", !user);
  $("#upload-panel").classList.toggle("hidden", !user);
  $("#auth-panel").classList.toggle("hidden", !!user);
  if (user) { csrfToken = (await api("/api/csrf")).token; if (user.is_admin) { $("#admin-panel").classList.remove("hidden"); await loadUsers(); } }
}

function addPhoto(photo) {
  const card = element("article", "card");
  const img = document.createElement("img"); img.src = photo.url; img.alt = photo.title; img.loading = "lazy";
  const body = element("div", "card-body"); body.append(element("h3", "", photo.title));
  if (photo.description) body.append(element("p", "", photo.description));
  body.append(element("p", "byline", `由 ${photo.owner} 分享`));
  if (user && (user.is_admin || user.id === photo.owner_id)) {
    const actions = element("div", "card-actions");
    const edit = element("button", "", "编辑说明"); edit.addEventListener("click", () => editPhoto(photo));
    const remove = element("button", "", "删除"); remove.addEventListener("click", () => deletePhoto(photo));
    actions.append(edit, remove); body.append(actions);
  }
  card.append(img, body); gallery.append(card);
}

async function loadPhotos(reset = false) {
  if (reset) { offset = 0; gallery.replaceChildren(); }
  const result = await api(`/api/photos?limit=36&offset=${offset}`);
  result.photos.forEach(addPhoto); offset += result.photos.length;
  $("#count").textContent = `${offset} 张`;
  $("#more").classList.toggle("hidden", result.photos.length < result.limit);
}

async function editPhoto(photo) {
  const title = prompt("照片标题", photo.title); if (title === null) return;
  const description = prompt("照片说明", photo.description); if (description === null) return;
  try { await api(`/api/photos/${photo.id}`, {method: "PATCH", body: JSON.stringify({title, description})}); await loadPhotos(true); }
  catch (error) { alert(error.message); }
}
async function deletePhoto(photo) {
  if (!confirm(`删除“${photo.title}”？此操作无法撤销。`)) return;
  try { await api(`/api/photos/${photo.id}`, {method: "DELETE"}); await loadPhotos(true); }
  catch (error) { alert(error.message); }
}

document.querySelectorAll(".tab").forEach(button => button.addEventListener("click", () => {
  mode = button.dataset.mode;
  document.querySelectorAll(".tab").forEach(item => item.classList.toggle("active", item === button));
  $("#auth-submit").textContent = mode === "login" ? "登录" : "创建账号";
  $("#auth-form [name=password]").autocomplete = mode === "login" ? "current-password" : "new-password";
  notice();
}));

$("#auth-form").addEventListener("submit", async event => {
  event.preventDefault(); notice();
  const form = new FormData(event.currentTarget);
  try { await api(`/api/${mode}`, {method: "POST", body: JSON.stringify({username: form.get("username"), password: form.get("password")})}); event.currentTarget.reset(); await refreshUser(); await loadPhotos(true); }
  catch (error) { notice(error.message); }
});

$("#logout").addEventListener("click", async () => { try { await api("/api/logout", {method: "POST", body: "{}"}); user = null; csrfToken = null; $("#admin-panel").classList.add("hidden"); await refreshUser(); } catch (error) { notice(error.message); } });

$("#upload-form").addEventListener("submit", async event => {
  event.preventDefault(); notice();
  const data = new FormData(event.currentTarget);
  try {
    const query = new URLSearchParams({title: data.get("title"), description: data.get("description")});
    await api(`/api/photos?${query}`, {method: "POST", body: data}); event.currentTarget.reset(); await loadPhotos(true);
  } catch (error) { notice(error.message); }
});

async function loadUsers() {
  const {users} = await api("/api/admin/users"); const target = $("#users"); target.replaceChildren();
  users.forEach(account => {
    const row = element("div", "user-row");
    const info = element("span", "", `${account.username}${account.is_admin ? " · 管理员" : ""}`);
    info.append(element("br")); info.append(element("small", "", account.is_active ? "正常" : "已停用"));
    const button = element("button", "", account.is_active ? "停用" : "恢复");
    button.disabled = account.id === user.id;
    button.addEventListener("click", async () => { try { await api(`/api/admin/users/${account.id}/active?active=${!account.is_active}`, {method: "PATCH"}); await loadUsers(); } catch (error) { alert(error.message); } });
    row.append(info, button); target.append(row);
  });
}

$("#more").addEventListener("click", () => loadPhotos().catch(error => notice(error.message)));
(async () => { try { await refreshUser(); await loadPhotos(true); } catch (error) { notice(error.message); } })();
