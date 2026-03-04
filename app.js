const form = document.getElementById('searchForm');
const statusEl = document.getElementById('status');
const resultsEl = document.getElementById('results');
const metaEl = document.getElementById('meta');
const csvBtn = document.getElementById('csvBtn');

let latestRows = [];
let latestNoResultsHint = '';

function datePlus(days) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

document.getElementById('checkIn').value = datePlus(14);
document.getElementById('checkOut').value = datePlus(17);

function toCurrency(value, currency) {
  if (value == null || Number.isNaN(value)) return 'N/A';
  const ccy = currency || 'USD';
  return new Intl.NumberFormat(undefined, { style: 'currency', currency: ccy, maximumFractionDigits: 0 }).format(value);
}

function rowToCsv(row) {
  const columns = [
    row.name,
    row.property_type,
    row.source,
    row.price_per_night,
    row.total_price,
    row.currency,
    row.review_score,
    row.review_count,
    row.free_cancellation,
    row.family_friendly,
    row.safe_area,
    row.distance_miles,
    row.address,
    row.url,
  ];
  return columns.map((v) => {
    const s = String(v ?? '');
    return `"${s.replaceAll('"', '""')}"`;
  }).join(',');
}

function downloadCsv(rows) {
  const header = ['name','property_type','source','price_per_night','total_price','currency','review_score','review_count','free_cancellation','family_friendly','safe_area','distance_miles','address','url'];
  const lines = [header.join(','), ...rows.map(rowToCsv)];
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'stay_scanner_results.csv';
  a.click();
  URL.revokeObjectURL(url);
}

function renderRows(rows) {
  resultsEl.innerHTML = '';
  if (!rows.length) {
    const hint = latestNoResultsHint ? ` ${latestNoResultsHint}` : '';
    resultsEl.innerHTML = `<div class="panel">No properties matched all filters.${hint}</div>`;
    return;
  }

  for (const row of rows) {
    const card = document.createElement('article');
    card.className = 'card';
    card.innerHTML = `
      <div class="top">
        <div>
          <div class="name">${row.name || 'Unknown property'}</div>
          <div class="meta">${row.property_type || 'stay'} · ${row.source || 'unknown'} · ${row.address || 'Address unavailable'}</div>
        </div>
        <div class="price">${toCurrency(row.price_per_night, row.currency)}/night</div>
      </div>
      <div class="meta">Total: ${toCurrency(row.total_price, row.currency)} · Rating: ${row.review_score ?? 'N/A'} (${row.review_count ?? 0}) · Distance: ${row.distance_miles?.toFixed?.(1) ?? 'N/A'} mi</div>
      <div class="badges">
        ${row.free_cancellation ? '<span class="badge">Free cancellation</span>' : ''}
        ${row.family_friendly ? '<span class="badge">Family friendly</span>' : ''}
        ${row.safe_area ? '<span class="badge">Safe area</span>' : ''}
      </div>
      <div class="link">${row.url ? `<a href="${row.url}" target="_blank" rel="noopener noreferrer">Open listing</a>` : ''}</div>
    `;
    resultsEl.appendChild(card);
  }
}

function formatExecutionStatus(items) {
  if (!Array.isArray(items) || !items.length) return '';
  return items.map((s) => {
    const retries = Array.isArray(s.retries) ? s.retries.length : 0;
    const parts = [
      s.provider || 'unknown',
      `outcome=${s.outcome || 'n/a'}`,
      `attempts=${s.attempts ?? 0}`,
      `rows=${s.result_rows ?? 0}`,
      `time=${s.duration_ms ?? 0}ms`,
    ];
    if (retries > 0) parts.push(`retries=${retries}`);
    return parts.join(', ');
  }).join(' | ');
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  statusEl.textContent = 'Searching providers...';
  csvBtn.disabled = true;
  resultsEl.innerHTML = '';
  metaEl.textContent = '';

  const payload = {
    city: document.getElementById('city').value.trim(),
    check_in: document.getElementById('checkIn').value,
    check_out: document.getElementById('checkOut').value,
    travelers: Number(document.getElementById('travelers').value),
    rooms: Number(document.getElementById('rooms').value),
  };

  try {
    const base = document.getElementById('apiBase').value.trim().replace(/\/$/, '');
    const resp = await fetch(`${base}/search`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || `HTTP ${resp.status}`);
    }

    latestRows = data.results || [];
    latestNoResultsHint = data.no_results_hint || '';
    renderRows(latestRows);
    csvBtn.disabled = latestRows.length === 0;
    const execText = formatExecutionStatus(data.execution_status);
    statusEl.textContent = `Done. ${latestRows.length} filtered stays.${execText ? ` Execution: ${execText}` : ''}`;
    const errs = data.provider_errors && data.provider_errors.length
      ? ` | errors: ${data.provider_errors.join(' ; ')}`
      : '';
    metaEl.textContent = `providers used: ${(data.providers_used || []).join(', ') || 'none'} | raw: ${data.raw_result_count ?? 0}${errs}`;
  } catch (err) {
    statusEl.textContent = `Search failed: ${err.message}`;
    latestRows = [];
    renderRows([]);
  }
});

csvBtn.addEventListener('click', () => {
  if (latestRows.length) {
    downloadCsv(latestRows);
  }
});
