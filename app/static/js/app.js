let currentShortCode = "";

document.addEventListener("DOMContentLoaded", () => {
    setupExpiryChips();
    setupForms();
});

function setupExpiryChips() {
    const chips = document.querySelectorAll(".expiry-chips .chip");
    const expiryInput = document.getElementById("expiryInput");

    chips.forEach(chip => {
        chip.addEventListener("click", () => {
            chips.forEach(c => c.classList.remove("active"));
            chip.classList.add("active");

            const hours = parseInt(chip.getAttribute("data-hours"), 10);
            if (hours === 0) {
                expiryInput.value = "";
            } else {
                const target = new Date(Date.now() + hours * 3600 * 1000);
                expiryInput.value = target.toISOString().slice(0, 16);
            }
        });
    });
}

function setupForms() {
    const form = document.getElementById("shortenForm");
    const shortenBtn = document.getElementById("shortenBtn");
    const resultBox = document.getElementById("resultBox");
    const errorBox = document.getElementById("errorBox");

    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        errorBox.style.display = "none";
        resultBox.style.display = "none";

        const url = document.getElementById("urlInput").value.trim();
        const alias = document.getElementById("aliasInput").value.trim();
        const expiryVal = document.getElementById("expiryInput").value;

        let expires_at = null;
        if (expiryVal) {
            expires_at = new Date(expiryVal).toISOString();
        }

        shortenBtn.disabled = true;
        shortenBtn.innerHTML = `<span>Shortening...</span>`;

        try {
            const payload = { url };
            if (alias) payload.alias = alias;
            if (expires_at) payload.expires_at = expires_at;

            const res = await fetch("/api/v1/links", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });

            const data = await res.json();
            if (!res.ok) {
                throw new Error(data.detail || "Failed to shorten URL.");
            }

            // Success
            currentShortCode = data.code;
            document.getElementById("resultShortUrl").innerText = data.short_url;
            resultBox.style.display = "block";
            resultBox.scrollIntoView({ behavior: "smooth", block: "nearest" });

            // Automatically load analytics for this link
            document.getElementById("analyticsCodeInput").value = data.code;
            loadAnalytics(data.code);

        } catch (err) {
            errorBox.innerText = err.message;
            errorBox.style.display = "block";
        } finally {
            shortenBtn.disabled = false;
            shortenBtn.innerHTML = `<span>⚡ Shorten URL</span>`;
        }
    });
}

function copyShortUrl() {
    const text = document.getElementById("resultShortUrl").innerText;
    navigator.clipboard.writeText(text).then(() => {
        const btn = document.getElementById("copyBtn");
        const orig = btn.innerHTML;
        btn.innerHTML = "✓ Copied!";
        setTimeout(() => { btn.innerHTML = orig; }, 2000);
    });
}

function openQrModal() {
    if (!currentShortCode) return;
    const modal = document.getElementById("qrModal");
    const img = document.getElementById("qrModalImg");
    img.src = `/api/v1/links/${currentShortCode}/qr`;
    modal.style.display = "flex";
}

function closeQrModal() {
    document.getElementById("qrModal").style.display = "none";
}

async function searchAnalytics() {
    const code = document.getElementById("analyticsCodeInput").value.trim();
    if (!code) return;
    loadAnalytics(code);
}

async function loadAnalytics(code) {
    const container = document.getElementById("analyticsContent");
    const statusMsg = document.getElementById("analyticsStatus");
    statusMsg.innerText = "Fetching real-time analytics...";
    statusMsg.style.display = "block";

    try {
        const res = await fetch(`/api/v1/links/${encodeURIComponent(code)}/stats`);
        if (!res.ok) {
            const err = await res.json();
            throw new Error(err.detail || "Could not load stats.");
        }
        const data = await res.json();
        statusMsg.style.display = "none";
        container.style.display = "block";

        // Fill stats
        document.getElementById("statTotalClicks").innerText = data.total_clicks.toLocaleString();
        document.getElementById("statUniqueVisitors").innerText = data.total_unique_visitors.toLocaleString();
        document.getElementById("statActive").innerText = data.is_active ? "Active" : "Disabled";
        document.getElementById("statActive").style.color = data.is_active ? "#10b981" : "#f43f5e";
        document.getElementById("statTargetUrl").innerText = data.long_url;

        // Render Timeline SVG Chart
        renderTimelineSvg(data.timeline);

        // Render Breakdowns
        renderBreakdownList("devicesList", data.devices, "device", data.total_clicks);
        renderBreakdownList("browsersList", data.browsers, "browser", data.total_clicks);
        renderBreakdownList("countriesList", data.countries, "country", data.total_clicks);
        renderBreakdownList("referrersList", data.referrers, "referrer", data.total_clicks);

        currentShortCode = code;

    } catch (err) {
        statusMsg.innerText = `Error: ${err.message}`;
        statusMsg.style.display = "block";
        container.style.display = "none";
    }
}

function renderTimelineSvg(timeline) {
    const svg = document.getElementById("timelineSvg");
    if (!timeline || timeline.length === 0) {
        svg.innerHTML = `<text x="50%" y="50%" fill="#64748b" text-anchor="middle">No click events recorded yet.</text>`;
        return;
    }

    const width = 600;
    const height = 220;
    const padding = 35;

    const maxClicks = Math.max(...timeline.map(t => t.clicks), 5);
    const stepX = (width - padding * 2) / (timeline.length - 1 || 1);

    let clickPoints = [];
    let uniquePoints = [];

    timeline.forEach((pt, i) => {
        const x = padding + i * stepX;
        const yClick = height - padding - (pt.clicks / maxClicks) * (height - padding * 2);
        const yUnique = height - padding - (pt.unique_visitors / maxClicks) * (height - padding * 2);
        clickPoints.push(`${x},${yClick}`);
        uniquePoints.push(`${x},${yUnique}`);
    });

    const clickPolyline = clickPoints.join(" ");
    const uniquePolyline = uniquePoints.join(" ");

    svg.innerHTML = `
        <!-- Grid lines -->
        <line x1="${padding}" y1="${height - padding}" x2="${width - padding}" y2="${height - padding}" stroke="rgba(255,255,255,0.1)" stroke-width="1" />
        <line x1="${padding}" y1="${padding}" x2="${width - padding}" y2="${padding}" stroke="rgba(255,255,255,0.05)" stroke-dasharray="4" />
        
        <!-- Unique Visitors Line (Cyan) -->
        <polyline fill="none" stroke="#06b6d4" stroke-width="2.5" stroke-dasharray="3" points="${uniquePolyline}" />
        
        <!-- Total Clicks Line (Indigo/Pink) -->
        <polyline fill="none" stroke="#6366f1" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" points="${clickPolyline}" />

        <!-- Labels -->
        <text x="${padding}" y="${height - 10}" fill="#64748b" font-size="11">${timeline[0].date}</text>
        <text x="${width - padding}" y="${height - 10}" fill="#64748b" font-size="11" text-anchor="end">${timeline[timeline.length - 1].date}</text>
        <text x="${padding - 8}" y="${padding + 5}" fill="#64748b" font-size="11" text-anchor="end">${maxClicks}</text>
        <text x="${padding - 8}" y="${height - padding}" fill="#64748b" font-size="11" text-anchor="end">0</text>
    `;
}

function renderBreakdownList(elementId, items, keyProp, total) {
    const listEl = document.getElementById(elementId);
    if (!items || items.length === 0) {
        listEl.innerHTML = `<div style="color: #64748b; font-size: 0.85rem;">No data recorded yet.</div>`;
        return;
    }

    const divisor = total > 0 ? total : items.reduce((acc, cur) => acc + cur.count, 0) || 1;
    let html = "";

    items.forEach(item => {
        const pct = Math.round((item.count / divisor) * 100);
        html += `
            <div class="breakdown-item">
                <div class="breakdown-meta">
                    <span>${item[keyProp] || 'Unknown'}</span>
                    <span style="font-weight: 600; color: #cbd5e1;">${item.count} (${pct}%)</span>
                </div>
                <div class="progress-bar-bg">
                    <div class="progress-bar-fill" style="width: ${pct}%"></div>
                </div>
            </div>
        `;
    });

    listEl.innerHTML = html;
}

async function testRedirect() {
    if (!currentShortCode) return;
    window.open(`/${currentShortCode}`, "_blank");
    setTimeout(() => {
        loadAnalytics(currentShortCode);
    }, 1500);
}

async function disableCurrentLink() {
    if (!currentShortCode) return;
    if (!confirm(`Are you sure you want to deactivate link /${currentShortCode}? Redirects will return 410 Gone.`)) {
        return;
    }
    try {
        const res = await fetch(`/api/v1/links/${currentShortCode}`, { method: "DELETE" });
        if (res.status === 204) {
            alert(`Link /${currentShortCode} disabled and invalidated from Redis cache.`);
            loadAnalytics(currentShortCode);
        } else {
            alert("Could not disable link.");
        }
    } catch (e) {
        alert("Error: " + e.message);
    }
}
