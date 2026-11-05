const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

const noticeEl = document.getElementById("notice");
const groupsEl = document.getElementById("groups");
const formEl = document.getElementById("config-form");
const saveBtn = document.getElementById("save-btn");
const reloadBtn = document.getElementById("reload-btn");
const tokenBadge = document.getElementById("token-badge");

const FIELDS = [
  {
    group: "basic",
    key: "sign_points",
    label: "每次签到积分",
    type: "int",
    min: 0,
    max: 1000,
    default: 10,
    hint: "每天第一次签到成功时增加的积分。",
  },
  {
    group: "basic",
    key: "brick_messages",
    label: "烧成一块砖所需消息数",
    type: "int",
    min: 1,
    max: 1000,
    default: 10,
    hint: "只统计其他群成员的消息，砖主自己的消息不计入。",
  },
  {
    group: "basic",
    key: "max_bricks",
    label: "每人砖块上限",
    type: "int",
    min: 1,
    max: 100,
    default: 3,
    hint: "可用砖块与正在烧的砖块合计上限。",
  },
  {
    group: "basic",
    key: "brick_start_cost_points",
    label: "开始烧砖消耗积分",
    type: "int",
    min: 0,
    max: 1000000,
    default: 0,
    hint: "0 表示免费烧砖。",
  },
  {
    group: "basic",
    key: "mute_seconds",
    label: "拍人禁言时长（秒）",
    type: "int",
    min: 1,
    max: 2592000,
    default: 60,
    hint: "需要机器人在群内拥有管理员权限，QQ 群一般最长 30 天。",
  },
  {
    group: "basic",
    key: "leaderboard_size",
    label: "积分榜显示人数",
    type: "int",
    min: 1,
    max: 50,
    default: 10,
    hint: "发送“积分榜”时展示前 N 名。",
  },
  {
    group: "basic",
    key: "timezone",
    label: "时区",
    type: "text",
    default: "Asia/Shanghai",
    placeholder: "Asia/Shanghai",
    hint: "签到日期和每日/每月兑换限次按此时区重置。",
  },
  {
    group: "basic",
    key: "protect_admins",
    label: "禁止拍群主和管理员",
    type: "bool",
    default: true,
    hint: "开启后普通成员不能消耗砖块禁言群主或管理员。",
  },
  {
    group: "basic",
    key: "notify_on_complete",
    label: "砖烧好时 @ 砖主",
    type: "bool",
    default: true,
    hint: "关闭后仍会增加砖块，只是不在群里提示。",
  },
  {
    group: "exchange",
    key: "xiaoning_enabled",
    label: "开启积分兑换",
    type: "bool",
    default: false,
    hint: "关闭后兑换指令会提示未开启；绑定和查询仍可使用。",
  },
  {
    group: "exchange",
    key: "xiaoning_exchange_mode",
    label: "兑换类型",
    type: "select",
    default: "both",
    options: [
      ["balance", "仅添加余额"],
      ["reset_card", "仅重置卡"],
      ["both", "余额和重置卡"],
    ],
    hint: "选择单类型时，另一类兑换会被拒绝。",
  },
  {
    group: "exchange",
    key: "xiaoning_base_url",
    label: "Xiaoning-API Base URL",
    type: "text",
    default: "",
    placeholder: "https://api.example.com",
    hint: "不要带 /v1；插件会自动去掉末尾 /v1 或 /api。",
  },
  {
    group: "exchange",
    key: "xiaoning_access_token",
    label: "管理员访问令牌",
    type: "password",
    default: "",
    placeholder: "留空表示保持当前令牌",
    hint: "新版 PAT 加余额需要 user:write scope；重置卡接口当前可能需要旧版管理员令牌。",
  },
  {
    group: "exchange",
    key: "xiaoning_fixed_user_id",
    label: "统一兑换账号 ID",
    type: "int",
    min: 0,
    max: 1000000000000,
    default: 0,
    hint: "大于 0 时所有兑换都发到该 Xiaoning 用户 ID；为 0 时由用户自行绑定账号。",
  },
  {
    group: "exchange",
    key: "xiaoning_points_per_balance",
    label: "每 1 额度消耗积分",
    type: "int",
    min: 1,
    max: 1000000000,
    default: 10,
    hint: "按面板显示的额度计算，实际写入 API 的 quota 会自动换算。",
  },
  {
    group: "exchange",
    key: "xiaoning_points_per_reset_card",
    label: "每 1 张重置卡消耗积分",
    type: "int",
    min: 1,
    max: 1000000000,
    default: 100,
    hint: "兑换时会一次性扣除对应积分。",
  },
  {
    group: "exchange",
    key: "xiaoning_daily_balance_limit",
    label: "每日额度上限 / 人",
    type: "int",
    min: 0,
    max: 100000,
    default: 1,
    hint: "0 表示不限制。",
  },
  {
    group: "exchange",
    key: "xiaoning_monthly_balance_limit",
    label: "每月额度上限 / 人",
    type: "int",
    min: 0,
    max: 1000000,
    default: 10,
    hint: "0 表示不限制；每日和每月限制同时生效。",
  },
  {
    group: "exchange",
    key: "xiaoning_daily_reset_card_limit",
    label: "每日重置卡上限 / 人",
    type: "int",
    min: 0,
    max: 1000,
    default: 1,
    hint: "0 表示不限制。",
  },
  {
    group: "exchange",
    key: "xiaoning_monthly_reset_card_limit",
    label: "每月重置卡上限 / 人",
    type: "int",
    min: 0,
    max: 10000,
    default: 5,
    hint: "0 表示不限制；每日和每月限制同时生效。",
  },
  {
    group: "exchange",
    key: "xiaoning_quota_per_unit",
    label: "1 额度对应的 quota",
    type: "int",
    min: 0,
    max: 1000000000000,
    default: 0,
    hint: "0 表示自动读取 /api/status；读取失败时会要求手动填写，New API 默认通常为 500000。",
  },
  {
    group: "exchange",
    key: "xiaoning_timeout_seconds",
    label: "API 请求超时（秒）",
    type: "int",
    min: 3,
    max: 120,
    default: 15,
    hint: "请求 Xiaoning-API 的最长等待时间。",
  },
];

const GROUPS = [
  {
    id: "basic",
    title: "基础玩法",
    hint: "签到、烧砖、拍人与积分榜参数。",
  },
  {
    id: "exchange",
    title: "Xiaoning-API 积分兑换",
    hint: "余额/重置卡兑换、访问令牌、比例与限次。",
  },
];

const inputs = new Map();
let tokenConfigured = false;

function showNotice(message, type = "info") {
  noticeEl.textContent = message;
  noticeEl.className = `notice ${type}`;
  noticeEl.classList.remove("hidden");
}

function hideNotice() {
  noticeEl.classList.add("hidden");
  noticeEl.textContent = "";
}

function setBusy(busy) {
  saveBtn.disabled = busy;
  reloadBtn.disabled = busy;
  saveBtn.textContent = busy ? "保存中..." : "保存设置";
}

function makeInput(field) {
  if (field.type === "bool") {
    const wrap = document.createElement("label");
    wrap.className = "switch-row";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.key = field.key;
    const text = document.createElement("span");
    text.textContent = "启用";
    wrap.append(input, text);
    inputs.set(field.key, input);
    return { element: wrap, input };
  }

  let input;
  if (field.type === "select") {
    input = document.createElement("select");
    for (const [value, label] of field.options || []) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      input.appendChild(option);
    }
  } else {
    input = document.createElement("input");
    input.type =
      field.type === "int" ? "number" : field.type === "password" ? "password" : "text";
    if (field.type === "int") {
      input.step = "1";
      if (field.min !== undefined) input.min = String(field.min);
      if (field.max !== undefined) input.max = String(field.max);
    }
    input.autocomplete = field.type === "password" ? "new-password" : "off";
    if (field.placeholder) input.placeholder = field.placeholder;
  }
  input.dataset.key = field.key;
  inputs.set(field.key, input);
  return { element: input, input };
}

function renderFields() {
  groupsEl.textContent = "";
  for (const group of GROUPS) {
    const section = document.createElement("section");
    section.className = "group";

    const head = document.createElement("div");
    head.className = "group-head";
    const title = document.createElement("h2");
    title.className = "group-title";
    title.textContent = group.title;
    const hint = document.createElement("p");
    hint.className = "group-hint";
    hint.textContent = group.hint;
    head.append(title, hint);
    section.appendChild(head);

    for (const field of FIELDS.filter((item) => item.group === group.id)) {
      const row = document.createElement("div");
      row.className = "field";

      const labelWrap = document.createElement("div");
      labelWrap.className = "field-label";
      const strong = document.createElement("strong");
      strong.textContent = field.label;
      labelWrap.appendChild(strong);
      if (field.hint) {
        const small = document.createElement("small");
        small.textContent = field.hint;
        labelWrap.appendChild(small);
      }

      const inputWrap = document.createElement("div");
      inputWrap.className = "field-input";
      const { element, input } = makeInput(field);
      inputWrap.appendChild(element);
      row.append(labelWrap, inputWrap);
      section.appendChild(row);
    }

    groupsEl.appendChild(section);
  }
}

function applyConfig(config) {
  for (const field of FIELDS) {
    const input = inputs.get(field.key);
    if (!input) continue;
    const value = config[field.key];
    if (field.type === "bool") {
      input.checked = value === true;
    } else if (value !== undefined && value !== null) {
      input.value = String(value);
    } else {
      input.value = String(field.default ?? "");
    }
  }
  tokenConfigured = Boolean(config.access_token_configured);
  updateTokenBadge();
}

function updateTokenBadge() {
  tokenBadge.textContent = tokenConfigured ? "令牌：已配置" : "令牌：未配置";
  tokenBadge.classList.toggle("ok", tokenConfigured);
  const tokenInput = inputs.get("xiaoning_access_token");
  if (tokenInput) {
    tokenInput.placeholder = tokenConfigured
      ? "已配置，留空则保持不变"
      : "请输入管理员访问令牌";
    tokenInput.value = "";
  }
}

async function load() {
  hideNotice();
  setBusy(true);
  try {
    const result = await bridge.apiGet("settings");
    const data = result?.data && result?.status ? result.data : result;
    if (result?.status === "error") {
      throw new Error(result.message || "读取配置失败");
    }
    applyConfig(data?.config || data || {});
    showNotice("配置已读取。", "success");
  } catch (error) {
    showNotice(`读取配置失败：${error?.message || error}`, "error");
  } finally {
    setBusy(false);
  }
}

function collectConfig() {
  const config = {};
  for (const field of FIELDS) {
    const input = inputs.get(field.key);
    if (!input) continue;
    if (field.type === "bool") {
      config[field.key] = Boolean(input.checked);
    } else if (field.type === "int") {
      const raw = input.value.trim();
      const value = Number(raw);
      if (!raw || !Number.isFinite(value) || !Number.isInteger(value)) {
        throw new Error(`${field.label} 必须是整数`);
      }
      if (field.min !== undefined && value < field.min) {
        throw new Error(`${field.label} 不能小于 ${field.min}`);
      }
      if (field.max !== undefined && value > field.max) {
        throw new Error(`${field.label} 不能大于 ${field.max}`);
      }
      config[field.key] = value;
    } else if (field.type === "password") {
      const value = input.value.trim();
      if (value) config[field.key] = value;
    } else {
      config[field.key] = input.value;
    }
  }
  return config;
}

async function save() {
  hideNotice();
  let payload;
  try {
    payload = collectConfig();
  } catch (error) {
    showNotice(error.message, "error");
    return;
  }

  setBusy(true);
  try {
    const result = await bridge.apiPost("settings", { config: payload });
    if (result?.status === "error") {
      throw new Error(result.message || "保存失败");
    }
    const data = result?.data && result?.status ? result.data : result;
    applyConfig(data?.config || data || {});
    showNotice("设置已保存并立即生效。", "success");
  } catch (error) {
    showNotice(`保存失败：${error?.message || error}`, "error");
  } finally {
    setBusy(false);
  }
}

formEl.addEventListener("submit", (event) => {
  event.preventDefault();
  void save();
});

reloadBtn.addEventListener("click", () => {
  void load();
});

renderFields();
if (!bridge) {
  showNotice("未检测到 AstrBot 插件页 Bridge，请从 AstrBot WebUI 插件页面打开。", "error");
  saveBtn.disabled = true;
  reloadBtn.disabled = true;
} else {
  try {
    await bridge.ready();
    await load();
  } catch (error) {
    showNotice(`插件页初始化失败：${error?.message || error}`, "error");
  }
}
