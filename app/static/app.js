/* 译词 · 前端工具 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function toast(message, type = "") {
  let host = $("#toast-host");
  if (!host) {
    host = document.createElement("div");
    host.id = "toast-host";
    document.body.appendChild(host);
  }
  const node = document.createElement("div");
  node.className = `toast ${type}`;
  node.textContent = message;
  host.appendChild(node);
  setTimeout(() => node.remove(), 3600);
}

function showLoading(tip = "AI 正在思考…") {
  let mask = $("#loading");
  if (!mask) {
    mask = document.createElement("div");
    mask.id = "loading";
    mask.innerHTML = '<div class="spinner"></div><div class="tip"></div>';
    document.body.appendChild(mask);
  }
  mask.querySelector(".tip").textContent = tip;
  mask.classList.add("on");
}

function hideLoading() {
  const mask = $("#loading");
  if (mask) mask.classList.remove("on");
}

async function postJSON(url, data) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data || {}),
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch (err) {
    payload = { ok: false, error: `服务端返回异常（HTTP ${response.status}）` };
  }
  if (!response.ok && payload.ok !== false) payload.ok = false;
  return payload;
}

/* ---------- 评分动画 ---------- */
function flashScores(result) {
  let host = $("#score-flash");
  if (!host) {
    host = document.createElement("div");
    host.id = "score-flash";
    document.body.appendChild(host);
  }
  const sentence = Math.max(0, Math.min(1, result.sentence_score || 0));
  const keyword = Math.max(0, Math.min(1, result.keyword_score || 0));
  const ok = result.passed;
  host.innerHTML = `
    <div class="score-card fade-in">
      <div class="verdict ${ok ? "ok" : "bad"}">${ok ? "过关" : "再来一次"}</div>
      <div class="meter ${sentence >= 0.8 ? "" : "bad"}">
        <div class="k"><span>整句翻译</span><span>${sentence.toFixed(2)}</span></div>
        <div class="track"><i data-w="${sentence * 100}"></i></div>
      </div>
      <div class="meter ${keyword >= 0.8 ? "" : "bad"}">
        <div class="k"><span>关键词翻译</span><span>${keyword.toFixed(2)}</span></div>
        <div class="track"><i data-w="${keyword * 100}"></i></div>
      </div>
      <div class="small muted">${ok ? "" : "看看标准译文和释义，稍后会再考一次"}</div>
    </div>`;
  host.classList.add("on");
  requestAnimationFrame(() => {
    $$("#score-flash .track > i").forEach((bar) => {
      bar.style.width = `${bar.dataset.w}%`;
    });
  });
  return new Promise((resolve) => setTimeout(resolve, 1500));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/* ---------- 高亮目标词 ---------- */
function highlight(sentence, word) {
  if (!sentence) return "";
  const escaped = sentence.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  if (!word) return escaped;
  const stem = word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const pattern = new RegExp(`\\b${stem}[a-z]{0,3}\\b`, "gi");
  return escaped.replace(pattern, (match) => `<mark>${match}</mark>`);
}

/* ---------- 发音 ---------- */
function speak(text) {
  const word = String(text || "").trim();
  if (!word) return;
  if (!("speechSynthesis" in window)) {
    toast("当前浏览器不支持发音", "error");
    return;
  }
  const utter = new SpeechSynthesisUtterance(word);
  utter.lang = "en-US";
  utter.rate = 0.9;
  window.speechSynthesis.cancel();
  window.speechSynthesis.speak(utter);
}

/* ---------- 收藏 ---------- */
async function toggleFavorite(uid, container, ctype, button) {
  const result = await postJSON("/api/favorite", { uid, container, ctype });
  if (!result.ok) {
    toast(result.error || "操作失败", "error");
    return;
  }
  button.dataset.fav = result.favorite ? "1" : "0";
  if (button.classList.contains("icon-btn")) {
    button.classList.toggle("on", result.favorite);
    button.title = result.favorite ? "取消收藏" : "收藏";
  } else {
    button.textContent = result.favorite ? "★ 已收藏" : "☆ 收藏";
  }
  toast(result.favorite ? "已收藏" : "已取消收藏", "success");
}
