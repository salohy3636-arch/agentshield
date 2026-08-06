// Shared config: point this at your deployed API if the web site is hosted
// separately from the FastAPI service (e.g. static site on Vercel/Netlify,
// API on Render/Fly). Leave empty ("") when the API serves these files itself.
window.AGENTSHIELD_API_BASE = window.AGENTSHIELD_API_BASE || "";

function apiUrl(path) {
  return `${window.AGENTSHIELD_API_BASE}${path}`;
}

// ---------------------------------------------------------------------------
// Hero ticker (marketing page): simulated feed so visitors see the product's
// core interaction without needing live traffic. The dashboard uses the same
// visual language driven by real /v1/ledger/recent data (see dashboard.js).
// ---------------------------------------------------------------------------
(function heroTicker() {
  const el = document.getElementById("hero-ticker");
  if (!el) return;

  const agents = ["billing-bot-01", "support-agent-eu", "trading-bot-alpha", "workflow-agent-03"];
  const actions = ["issue_refund", "execute_trade", "send_email", "update_ticket", "modify_billing"];
  const tiers = [
    { key: "safe", label: "PASS", weight: 6 },
    { key: "hold", label: "HOLD", weight: 3 },
    { key: "block", label: "BLOCK", weight: 1 },
  ];

  function weightedTier() {
    const total = tiers.reduce((s, t) => s + t.weight, 0);
    let r = Math.random() * total;
    for (const t of tiers) {
      if (r < t.weight) return t;
      r -= t.weight;
    }
    return tiers[0];
  }

  function addRow() {
    const tier = weightedTier();
    const score = tier.key === "safe" ? Math.round(Math.random() * 39)
                : tier.key === "hold" ? 40 + Math.round(Math.random() * 30)
                : 71 + Math.round(Math.random() * 29);

    const row = document.createElement("div");
    row.className = `ticker-row ${tier.key}`;
    row.innerHTML = `
      <span class="ticker-agent">${agents[Math.floor(Math.random() * agents.length)]} · ${actions[Math.floor(Math.random() * actions.length)]}</span>
      <span class="ticker-verdict">${tier.label} · ${score}</span>
    `;
    el.prepend(row);
    while (el.children.length > 8) el.removeChild(el.lastChild);
  }

  for (let i = 0; i < 6; i++) addRow();
  setInterval(addRow, 1400);
})();

// ---------------------------------------------------------------------------
// Pricing: monthly/yearly toggle + Stripe Checkout wiring
// ---------------------------------------------------------------------------
(function pricing() {
  const toggleBtns = document.querySelectorAll(".toggle-btn");
  const amounts = document.querySelectorAll(".price-amount");
  const periods = document.querySelectorAll(".price-period");
  if (!toggleBtns.length) return;

  let cycle = "monthly";

  function render() {
    toggleBtns.forEach((b) => b.classList.toggle("active", b.dataset.cycle === cycle));
    amounts.forEach((a) => { a.textContent = `$${a.dataset[cycle]}`; });
    periods.forEach((p) => {
      p.textContent = cycle === "monthly" ? "/شهرياً" : "/سنوياً";
    });
  }

  toggleBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      cycle = btn.dataset.cycle;
      render();
    });
  });

  document.querySelectorAll(".plan-cta").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const tier = btn.dataset.tier;
      const original = btn.textContent;
      btn.disabled = true;
      btn.textContent = "جارٍ التحويل إلى الدفع…";
      try {
        const res = await fetch(apiUrl("/v1/billing/checkout-session"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tier, cycle }),
        });
        const data = await res.json();
        if (res.ok && data.checkout_url) {
          window.location.href = data.checkout_url;
        } else {
          alert(data.detail || "تعذّر بدء عملية الدفع. تأكد من إعداد أسعار Stripe في الخادم.");
          btn.disabled = false;
          btn.textContent = original;
        }
      } catch (err) {
        alert("تعذّر الاتصال بالخادم. تأكد أن API يعمل.");
        btn.disabled = false;
        btn.textContent = original;
      }
    });
  });

  render();
})();
