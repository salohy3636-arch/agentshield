const API = (path) => `${window.AGENTSHIELD_API_BASE || ""}${path}`;

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString("ar-EG", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function decisionLabel(decision) {
  if (decision === "pass") return { text: "مرّر", cls: "pass" };
  if (decision === "hold") return { text: "قيد المراجعة", cls: "hold" };
  if (decision === "block") return { text: "محظور", cls: "block" };
  if (decision.startsWith("human_pass")) return { text: "أجازه إنسان", cls: "human_pass" };
  if (decision.startsWith("human_block")) return { text: "رفضه إنسان", cls: "human_block" };
  return { text: decision, cls: "hold" };
}

async function loadStats() {
  try {
    const res = await fetch(API("/v1/stats"));
    const s = await res.json();
    document.getElementById("stat-total").textContent = s.total_actions;
    document.getElementById("stat-passed").textContent = s.passed;
    document.getElementById("stat-held").textContent = s.held;
    document.getElementById("stat-blocked").textContent = s.blocked;
    document.getElementById("stat-avg").textContent = s.avg_risk_score;
  } catch (e) {
    console.error("stats failed", e);
  }
}

function tierColor(score) {
  if (score < 40) return "#34D399";
  if (score <= 70) return "#F5A524";
  return "#EF4444";
}

function drawRiskChart(entries) {
  const canvas = document.getElementById("risk-chart");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || canvas.parentElement.clientWidth;
  const h = 140;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const points = entries.slice(-30).reverse(); // oldest -> newest, left -> right
  if (!points.length) {
    ctx.fillStyle = "#8A93A6";
    ctx.font = "13px Inter, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText("لا توجد بيانات كافية بعد", w / 2, h / 2);
    return;
  }

  const padTop = 12, padBottom = 18, padSide = 6;
  const plotH = h - padTop - padBottom;
  const stepX = points.length > 1 ? (w - padSide * 2) / (points.length - 1) : 0;

  // threshold guide lines at 40 and 70
  ctx.strokeStyle = "#232B3B";
  ctx.lineWidth = 1;
  [40, 70].forEach((val) => {
    const y = padTop + plotH * (1 - val / 100);
    ctx.beginPath();
    ctx.moveTo(padSide, y);
    ctx.lineTo(w - padSide, y);
    ctx.stroke();
  });

  // line path
  ctx.beginPath();
  ctx.strokeStyle = "#8A93A6";
  ctx.lineWidth = 1.5;
  points.forEach((p, i) => {
    const x = padSide + i * stepX;
    const y = padTop + plotH * (1 - Math.min(p.risk_score, 100) / 100);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // colored points
  points.forEach((p, i) => {
    const x = padSide + i * stepX;
    const y = padTop + plotH * (1 - Math.min(p.risk_score, 100) / 100);
    ctx.beginPath();
    ctx.arc(x, y, 3.2, 0, Math.PI * 2);
    ctx.fillStyle = tierColor(p.risk_score);
    ctx.fill();
  });
}

async function loadLedger() {
  const tbody = document.getElementById("ledger-body");
  try {
    const res = await fetch(API("/v1/ledger/recent?limit=40"));
    const data = await res.json();
    drawRiskChart(data.entries);
    if (!data.entries.length) {
      tbody.innerHTML = `<tr><td colspan="6" class="empty-state">لا توجد بيانات بعد — اضغط "تعبئة بيانات تجريبية".</td></tr>`;
      return;
    }
    tbody.innerHTML = data.entries.map((e) => {
      const d = decisionLabel(e.decision);
      return `<tr>
        <td>${fmtTime(e.timestamp)}</td>
        <td>${e.agent_id}</td>
        <td>${e.action_type}</td>
        <td>${e.risk_score}</td>
        <td><span class="decision-badge ${d.cls}">${d.text}</span></td>
        <td class="hash">${e.entry_hash.slice(0, 12)}…</td>
      </tr>`;
    }).join("");
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty-state">تعذّر الاتصال بالخادم.</td></tr>`;
  }
}

async function loadApprovals() {
  const list = document.getElementById("approvals-list");
  const count = document.getElementById("approvals-count");
  try {
    const res = await fetch(API("/v1/approvals/pending"));
    const data = await res.json();
    count.textContent = data.pending.length;
    if (!data.pending.length) {
      list.innerHTML = `<p class="empty-state">لا توجد إجراءات بانتظار المراجعة حالياً.</p>`;
      return;
    }
    list.innerHTML = data.pending.map((p) => `
      <div class="approval-row" data-token="${p.token}">
        <span class="approval-meta"><span class="agent">${p.agent_id}</span> · ${p.action_type} · درجة ${p.risk_score}</span>
        <span class="approval-actions">
          <button class="btn-approve" data-decision="true">إجازة</button>
          <button class="btn-deny" data-decision="false">رفض</button>
        </span>
      </div>
    `).join("");
  } catch (e) {
    list.innerHTML = `<p class="empty-state">تعذّر الاتصال بالخادم.</p>`;
  }
}

async function loadAccount() {
  const box = document.getElementById("account-box");
  try {
    const res = await fetch(API("/v1/accounts/demo"));
    const a = await res.json();
    box.innerHTML = `
      <div class="account-stat"><span class="label">الخطة الحالية</span><span class="value">${a.tier}</span></div>
      <div class="account-stat"><span class="label">رصيد الأرصدة</span><span class="value">$${a.credit_balance_usd.toFixed(2)}</span></div>
      <div class="account-stat"><span class="label">سقف الأرصدة</span><span class="value">$${a.credit_relief_cap_usd}</span></div>
      <div class="account-stat"><span class="label">سقف النقد</span><span class="value">$${a.cash_relief_cap_usd}</span></div>
    `;
  } catch (e) {
    box.innerHTML = `<p class="empty-state">تعذّر تحميل بيانات الحساب.</p>`;
  }
}

async function verifyLedger() {
  const statusEl = document.getElementById("ledger-status");
  statusEl.innerHTML = `<span class="dot dot-pending"></span> جارٍ التحقق من السجل…`;
  try {
    const res = await fetch(API("/v1/ledger/verify"));
    const data = await res.json();
    if (data.valid) {
      statusEl.innerHTML = `<span class="dot" style="background:var(--safe)"></span> السجل سليم (${data.entries_checked} إدخال)`;
    } else {
      statusEl.innerHTML = `<span class="dot" style="background:var(--block)"></span> تحذير: كسر في السلسلة عند #${data.broken_at_index}`;
    }
  } catch (e) {
    statusEl.innerHTML = `<span class="dot" style="background:var(--block)"></span> تعذّر التحقق`;
  }
}

async function refreshAll() {
  await Promise.all([loadStats(), loadLedger(), loadApprovals(), loadAccount()]);
  verifyLedger();
}

document.getElementById("refresh-btn")?.addEventListener("click", refreshAll);
document.getElementById("verify-btn")?.addEventListener("click", verifyLedger);

document.getElementById("seed-btn")?.addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  btn.textContent = "جارٍ التوليد…";
  try {
    await fetch(API("/v1/demo/seed"), { method: "POST" });
    await refreshAll();
  } catch (err) {
    alert("فشل توليد البيانات التجريبية.");
  } finally {
    btn.disabled = false;
    btn.textContent = "تعبئة بيانات تجريبية";
  }
});

document.getElementById("approvals-list")?.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-decision]");
  if (!btn) return;
  const row = btn.closest(".approval-row");
  const token = row.dataset.token;
  const approved = btn.dataset.decision === "true";
  btn.disabled = true;
  try {
    await fetch(API(`/v1/approvals/${token}/decide`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved, approver: "dashboard-operator" }),
    });
    await refreshAll();
  } catch (err) {
    alert("فشل تسجيل القرار.");
    btn.disabled = false;
  }
});

refreshAll();
setInterval(refreshAll, 8000);
