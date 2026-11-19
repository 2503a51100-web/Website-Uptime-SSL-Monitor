// Website Uptime and SSL Monitor - frontend logic.
// Talks to the Flask API with fetch(). All numbers shown come from the backend.

(function () {
    "use strict";

    const $ = (id) => document.getElementById(id);

    const form = $("check-form");
    const urlInput = $("url-input");
    const checkBtn = $("check-btn");
    const formError = $("form-error");
    const siteFilter = $("site-filter");

    let chart = null;

    // ---------- helpers ----------

    function escapeHtml(value) {
        const div = document.createElement("div");
        div.textContent = value == null ? "" : String(value);
        return div.innerHTML;
    }

    function formatDateTime(iso) {
        if (!iso) return "-";
        const d = new Date(iso);
        return isNaN(d) ? iso : d.toLocaleString();
    }

    function formatTime(ms) {
        return ms == null ? "-" : Math.round(ms) + " ms";
    }

    function hostOf(url) {
        try { return new URL(url).host; } catch (e) { return url; }
    }

    // CSS class for an SSL status: green / orange / red / grey
    function sslClass(status) {
        switch (status) {
            case "Valid": return "up";
            case "Expiring Soon": return "warn";
            case "Expired":
            case "Invalid": return "down";
            default: return "grey";   // Not Applicable, Unavailable
        }
    }

    async function api(path, options) {
        const response = await fetch(path, options);
        let data = null;
        try { data = await response.json(); } catch (e) { /* no JSON body */ }
        if (!response.ok) {
            throw new Error((data && data.error) || "Request failed (HTTP " + response.status + ").");
        }
        return data;
    }

    function showFormError(message) {
        formError.textContent = message;
        formError.hidden = false;
    }

    function clearFormError() {
        formError.hidden = true;
        formError.textContent = "";
    }

    // ---------- result panel ----------

    function showResult(r, caption) {
        $("result").hidden = false;
        $("result-caption").textContent = caption;

        const up = r.status === "UP";
        $("result-banner").className = "banner " + (up ? "up" : "down");
        $("result-status").textContent = r.status;
        $("result-banner-text").textContent = up
            ? "The website is reachable."
            : "The website could not be reached or returned an error.";

        $("r-url").textContent = r.url;
        $("r-status").innerHTML = '<span class="' + (up ? "text-up" : "text-down") + '">' + escapeHtml(r.status) + "</span>";
        $("r-code").textContent = r.status_code != null ? r.status_code : "No response";
        $("r-time").textContent = r.response_time_ms != null ? formatTime(r.response_time_ms) : "No response";

        const cls = sslClass(r.ssl_status);
        const sslText = { up: "text-up", warn: "text-warn", down: "text-down", grey: "text-muted" }[cls];
        $("r-ssl").innerHTML = '<span class="' + sslText + '">' + escapeHtml(r.ssl_status) + "</span>"
            + (r.ssl_message ? '<br><span class="text-muted">' + escapeHtml(r.ssl_message) + "</span>" : "");

        $("r-expiry").textContent = r.ssl_expiry_date || "Not available";

        if (r.ssl_days_remaining == null) {
            $("r-days").textContent = "Not available";
        } else if (r.ssl_days_remaining < 0) {
            $("r-days").innerHTML = '<span class="text-down">Expired ' + Math.abs(r.ssl_days_remaining) + " day(s) ago</span>";
        } else {
            const c = r.ssl_status === "Expiring Soon" ? "text-warn" : "";
            $("r-days").innerHTML = '<span class="' + c + '">' + r.ssl_days_remaining + " day(s)</span>";
        }

        // orange warning box when the certificate is about to expire
        const warn = $("ssl-warning");
        if (r.ssl_status === "Expiring Soon") {
            warn.textContent = "Warning: the SSL certificate expires in " + r.ssl_days_remaining
                + " day(s). Renew it before it expires.";
            warn.hidden = false;
        } else {
            warn.hidden = true;
        }

        $("r-error-row").hidden = !r.error_message;
        $("r-error").textContent = r.error_message || "";
        $("r-checked").textContent = formatDateTime(r.checked_at);
    }

    // ---------- history table ----------

    function renderHistory(records) {
        const body = $("history-body");
        body.innerHTML = "";
        $("history-empty").hidden = records.length > 0;
        $("history-table").parentElement.hidden = records.length === 0;

        records.forEach((r) => {
            const tr = document.createElement("tr");
            const statusBadge = '<span class="badge ' + (r.status === "UP" ? "up" : "down") + '">' + escapeHtml(r.status) + "</span>";
            const sslBadge = '<span class="badge ' + sslClass(r.ssl_status) + '">' + escapeHtml(r.ssl_status) + "</span>";
            tr.innerHTML =
                '<td class="url-cell">' + escapeHtml(r.url) + "</td>" +
                "<td>" + statusBadge + "</td>" +
                "<td>" + (r.status_code != null ? escapeHtml(r.status_code) : "-") + "</td>" +
                "<td>" + formatTime(r.response_time_ms) + "</td>" +
                "<td>" + sslBadge + "</td>" +
                "<td>" + escapeHtml(formatDateTime(r.checked_at)) + "</td>" +
                '<td class="actions">' +
                    '<button type="button" class="btn btn-small btn-outline" data-action="view" data-id="' + r.id + '">View</button>' +
                    '<button type="button" class="btn btn-small btn-danger-outline" data-action="delete" data-id="' + r.id + '">Delete</button>' +
                "</td>";
            body.appendChild(tr);
        });
    }

    // ---------- stats ----------

    function renderStats(s) {
        $("stat-total").textContent = s.total_checks;
        $("stat-up").textContent = s.successful_checks;
        $("stat-down").textContent = s.failed_checks;
        $("stat-uptime").textContent = s.uptime_percentage == null ? "N/A" : s.uptime_percentage + "%";
    }

    // ---------- chart (real response times from the database) ----------

    function renderChart(records) {
        const canvas = $("response-chart");
        const empty = $("chart-empty");

        if (typeof Chart === "undefined") {
            canvas.hidden = true;
            empty.hidden = false;
            empty.textContent = "The graph library (Chart.js) could not be loaded. Check your internet connection and reload the page.";
            return;
        }

        // history comes newest first; the graph shows the last 20 checks oldest to newest
        const points = records.slice(0, 20).reverse();
        const timed = points.filter((r) => r.response_time_ms != null);

        if (timed.length === 0) {
            canvas.hidden = true;
            empty.hidden = false;
            empty.textContent = records.length === 0
                ? "No data yet. The graph appears after your first check."
                : "No response times to show yet. Failed checks without a response are not plotted.";
            if (chart) { chart.destroy(); chart = null; }
            return;
        }

        canvas.hidden = false;
        empty.hidden = true;

        const labels = points.map((r) => hostOf(r.url) + " " + new Date(r.checked_at).toLocaleTimeString());
        const values = points.map((r) => r.response_time_ms);   // null = gap in the line

        if (chart) chart.destroy();
        chart = new Chart(canvas, {
            type: "line",
            data: {
                labels: labels,
                datasets: [{
                    label: "Response time (ms)",
                    data: values,
                    borderColor: "#1769aa",
                    backgroundColor: "#1769aa",
                    borderWidth: 2,
                    tension: 0,
                    pointRadius: 3,
                    spanGaps: false
                }]
            },
            options: {
                responsive: true,
                plugins: { legend: { display: false } },
                scales: {
                    y: { beginAtZero: true, title: { display: true, text: "Milliseconds" } },
                    x: { ticks: { maxRotation: 60, autoSkip: true } }
                }
            }
        });
    }

    // ---------- loading data ----------

    async function loadSites() {
        const sites = await api("/api/sites");
        const current = siteFilter.value;
        siteFilter.innerHTML = '<option value="">All websites</option>';
        sites.forEach((s) => {
            const opt = document.createElement("option");
            opt.value = s;
            opt.textContent = s;
            siteFilter.appendChild(opt);
        });
        siteFilter.value = sites.includes(current) ? current : "";
    }

    async function loadHistoryAndStats() {
        const query = siteFilter.value ? "&url=" + encodeURIComponent(siteFilter.value) : "";
        const [records, stats] = await Promise.all([
            api("/api/history?limit=100" + query),
            api("/api/stats?" + query.slice(1))
        ]);
        renderStats(stats);
        renderHistory(records);
        renderChart(records);
        return records;
    }

    async function refreshAll() {
        await loadSites();
        return loadHistoryAndStats();
    }

    // ---------- events ----------

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        clearFormError();

        const url = urlInput.value.trim();
        if (!url) {
            showFormError("Please enter a website URL.");
            return;
        }

        checkBtn.disabled = true;
        checkBtn.textContent = "Checking...";
        try {
            const result = await api("/api/check", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ url: url })
            });
            showResult(result, "Result of the check you just ran");
            await refreshAll();
        } catch (err) {
            showFormError(err.message);
        } finally {
            checkBtn.disabled = false;
            checkBtn.textContent = "Check Website";
        }
    });

    siteFilter.addEventListener("change", () => {
        loadHistoryAndStats().catch((e) => showFormError(e.message));
    });

    $("history-body").addEventListener("click", async (event) => {
        const button = event.target.closest("button[data-action]");
        if (!button) return;
        const id = button.dataset.id;

        try {
            if (button.dataset.action === "view") {
                const record = await api("/api/history/" + id);
                showResult(record, "Saved check #" + record.id);
                $("monitor").scrollIntoView();
            } else if (button.dataset.action === "delete") {
                if (!confirm("Delete this history record?")) return;
                await api("/api/history/" + id, { method: "DELETE" });
                await refreshAll();
            }
        } catch (err) {
            alert(err.message);
        }
    });

    // ---------- first load: show saved history and the latest result ----------

    refreshAll()
        .then((records) => {
            // The latest result comes from the unfiltered, newest-first history
            if (records.length > 0) {
                showResult(records[0], "Latest saved check (#" + records[0].id + ")");
            }
        })
        .catch((err) => showFormError("Could not load saved data: " + err.message));
})();
