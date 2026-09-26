// ============================================================
// State
// ============================================================

const state = {
  data: null,
  ts: null,          // lazy-loaded from ts.json on first modal open
  tsLoading: null,   // in-flight Promise to avoid double-fetch
  tsSize: null,      // lazy-loaded from ts_size.json (per floor-area bucket)
  tsSizeLoading: null,
  schools: null,     // lazy-loaded from schools.json
  schoolsLoading: null,
  selectedSchools: new Set(),  // currently selected school names for filtering
  selectedProjects: new Set(), // currently selected project names for filtering (multi-select)
  projectNameList: [],         // sorted project names for the search autocomplete
  charts: {},
  sort: { col: "n", dir: "desc" },
  page: 1,
  perPage: 50,
  modalProject: null,       // full project row; purchase_area_sqft updated when bucket row clicked
  sizeBucketClusters: null, // clusters rendered in the modal size-bucket table
  modalCharts: {},
  // Row registries -- indexed by data-row on <button data-row="N">
  pageRows: [],
  nbRows: [],
  mrtRows: [],
};

// ============================================================
// Constants -- Bala's curve
// ============================================================

// Fraction of 99-yr leasehold value at each remaining lease term.
const BALA_TABLE = [
  [99, 1.0000], [90, 0.9625], [80, 0.9000], [70, 0.8250],
  [60, 0.7375], [50, 0.6375], [40, 0.5250], [30, 0.4000],
  [20, 0.2625], [10, 0.1250], [5,  0.0563], [0,  0.0000],
];

// ============================================================
// Math utilities
// ============================================================

// Subtract n calendar months from a "YYYY-MM" string.
function subtractMonths(yyyyMm, n) {
  const [y, m] = yyyyMm.split("-").map(Number);
  const d = new Date(y, m - 1 - n, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function haversineM(lat1, lon1, lat2, lon2) {
  const R = 6_371_000;
  const dLat = (lat2 - lat1) * Math.PI / 180;
  const dLon = (lon2 - lon1) * Math.PI / 180;
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1 * Math.PI / 180) *
    Math.cos(lat2 * Math.PI / 180) *
    Math.sin(dLon / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function balaValue(years) {
  if (years <= 0) return 0;
  if (years >= 99) return 1.0;
  for (let i = 0; i < BALA_TABLE.length - 1; i++) {
    const [y1, v1] = BALA_TABLE[i];
    const [y2, v2] = BALA_TABLE[i + 1];
    if (years <= y1 && years >= y2) {
      const t = (y1 - y2) > 0 ? (years - y2) / (y1 - y2) : 0;
      return v2 + t * (v1 - v2);
    }
  }
  return 0;
}

function leaseRemainingAt(p, year) {
  if (!p.lease_start_year || !p.lease_duration) return null;
  return Math.max(0, p.lease_duration - (year - p.lease_start_year));
}

// Multiplier to normalise a leasehold PSF to 99-yr equivalent at a given year.
function leaseFactor(p, year, normMode) {
  if (normMode === "none") return 1.0;
  if (p.tenure_type === "freehold") return 1.0;
  if (!p.lease_duration || !p.lease_start_year) return 1.0;
  const rem = leaseRemainingAt(p, year);
  if (rem === null || rem >= 99) return 1.0;
  if (rem <= 0) return 1.0;
  if (normMode === "proportionate") return 99 / rem;
  if (normMode === "bala") return 1.0 / balaValue(rem);
  return 1.0;
}

function gfaFactor(p, gfaOn, gfaPct) {
  if (!gfaOn || !p.gfa_harmonized) return 1.0;
  return 1.0 - (gfaPct / 100);
}

// ============================================================
// Purchase type helpers
// ============================================================

function getPurchaseType() {
  return document.querySelector('input[name="purchaseType"]:checked')?.value ?? "all";
}

// Returns the modal purchase type radio value (inside the chart section).
function getModalPurchaseType() {
  return document.querySelector('input[name="modalPurchaseType"]:checked')?.value ?? "all";
}

// Returns the chart axis toggle value: "sale" or "purchase".
function getChartAxis() {
  return document.querySelector('input[name="chartAxis"]:checked')?.value ?? "sale";
}

// Returns the data.json field name for the active CAGR column.
function activeCagrField() {
  return { all: "median_cagr", new: "cagr_new", sub: "cagr_sub", res: "cagr_res" }[getPurchaseType()] ?? "median_cagr";
}

// Returns the ts.json key for the active CAGR series (sale-date view).
function activeTsKey() {
  return { all: "c", new: "cn", sub: "cs", res: "cr" }[getPurchaseType()] ?? "c";
}

// ============================================================
// Formatting
// ============================================================

const fmtPct  = x => x == null ? "—" : `${Number(x).toFixed(2)}%`;
const fmtNum  = x => x == null ? "—" : new Intl.NumberFormat("en-SG").format(x);
const fmtYrs  = x => x == null ? "—" : `${Number(x).toFixed(1)} yrs`;
const fmtDist = x => x == null ? "—" : `${Math.round(x)} m`;

// Render a CAGR percentage with colour: green positive, red negative, muted null.
const fmtCagrCell = x => {
  if (x == null) return '<span class="muted-val">—</span>';
  const cls = x >= 0 ? "cagr-pos" : "cagr-neg";
  const sign = x >= 0 ? "+" : "";
  return `<span class="${cls}">${sign}${Number(x).toFixed(2)}%</span>`;
};

function leaseLabel(p) {
  if (!p.tenure_type || p.tenure_type === "other") return "—";
  if (p.tenure_type === "freehold") return "FH";
  if (p.lease_remaining != null) return `${p.lease_remaining} yr`;
  if (p.lease_duration)          return `${p.lease_duration} yr`;
  return "—";
}

// ============================================================
// Global adjustment controls (single set in the explorer card)
// ============================================================

function getAdj() {
  return {
    gfaOn:     !!(document.getElementById("adj-gfa")?.checked),
    gfaPct:    parseFloat(document.getElementById("adj-gfa-pct")?.value || "5") || 5,
    leaseNorm: document.getElementById("adj-lease")?.value || "none",
  };
}

function updateAdjNote() {
  const adj = getAdj();
  const el = document.getElementById("adj-active-note");
  if (!el) return;
  const parts = [];
  if (adj.gfaOn) parts.push(`GFA -${adj.gfaPct}%`);
  if (adj.leaseNorm !== "none") {
    parts.push(adj.leaseNorm === "bala" ? "Bala's curve" : "Proportionate lease");
  }
  el.textContent = parts.length ? `Active: ${parts.join(", ")}` : "No PSF adjustments active";
}

// ============================================================
// Data loading
// ============================================================

async function loadData() {
  const res = await fetch("data.json?v=9");
  if (!res.ok) throw new Error("data.json not found. Run the pipeline first.");
  state.data = await res.json();
  // Pre-build MRT lookup: project_name → {mrt, dist}
  // Uses the first bucket row that has a non-empty nearest_mrt.
  state.mrtMap = {};
  for (const row of state.data.projects) {
    if (row.nearest_mrt && !state.mrtMap[row.project_name]) {
      state.mrtMap[row.project_name] = { mrt: row.nearest_mrt, dist: row.nearest_mrt_distance_m };
    }
  }
  render();
}

// ============================================================
// Main render
// ============================================================

function render() {
  const d = state.data;
  renderMetrics(d.summary);
  renderCharts(d);
  populateFilters(d.projects);
  renderProjects();
  updateAdjNote();
}

// ============================================================
// Metrics
// ============================================================

function renderMetrics(s) {
  document.getElementById("metrics").innerHTML = [
    ["Transactions", fmtNum(s.transactions),  "clean source records"],
    ["Repeat sales", fmtNum(s.repeat_sales),   "qualifying exact-unit pairs"],
    ["Projects",     fmtNum(s.projects),       "developments represented"],
    ["Median CAGR",  fmtPct(s.median_cagr),    "annualised nominal appreciation"],
    ["Profitable",   fmtPct(s.profit_rate),     "repeat sales with higher resale price"],
  ].map(([label, val, detail]) =>
    `<div class="metric">
       <div class="label">${label}</div>
       <div class="value">${val}</div>
       <div class="detail">${detail}</div>
     </div>`
  ).join("");
}

// ============================================================
// Overview charts
// ============================================================

function makeChart(id, config) {
  if (state.charts[id]) state.charts[id].destroy();
  state.charts[id] = new Chart(document.getElementById(id), config);
}

function baseOptions() {
  return {
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    scales: {
      x: { grid: { display: false }, ticks: { color: "#667085" } },
      y: { grid: { color: "#edf0f3" }, ticks: { color: "#667085" } },
    },
  };
}

function renderCharts(d) {
  makeChart("distributionChart", {
    type: "bar",
    data: {
      labels: ["P25", "Median", "P75"],
      datasets: [{ data: [d.summary.p25_cagr, d.summary.median_cagr, d.summary.p75_cagr], borderWidth: 0 }],
    },
    options: {
      ...baseOptions(),
      scales: { x: { grid: { display: false } }, y: { title: { display: true, text: "CAGR (%)" } } },
    },
  });

  const cohorts = [...d.purchase_cohorts].sort((a, b) =>
    String(a.cohort).localeCompare(String(b.cohort))
  );
  makeChart("cohortChart", {
    type: "line",
    data: {
      labels: cohorts.map(x => x.cohort),
      datasets: [{ data: cohorts.map(x => x.median_cagr), tension: .25, pointRadius: 3, borderWidth: 2 }],
    },
    options: {
      ...baseOptions(),
      scales: { ...baseOptions().scales, y: { ...baseOptions().scales.y, title: { display: true, text: "Median CAGR (%)" } } },
    },
  });

  makeChart("sizeChart", {
    type: "bar",
    data: {
      labels: d.size_buckets.map(x => x.size_bucket),
      datasets: [{ data: d.size_buckets.map(x => x.median_cagr), borderWidth: 0 }],
    },
    options: {
      ...baseOptions(),
      scales: {
        x: { grid: { display: false }, ticks: { maxRotation: 45, minRotation: 35 } },
        y: { title: { display: true, text: "Median CAGR (%)" } },
      },
    },
  });

  const areas = d.planning_areas.filter(x => x.n >= 50).slice(0, 15).reverse();
  makeChart("locationChart", {
    type: "bar",
    data: {
      labels: areas.map(x => x.planning_area),
      datasets: [{ data: areas.map(x => x.median_cagr), borderWidth: 0 }],
    },
    options: {
      ...baseOptions(),
      indexAxis: "y",
      scales: { x: { title: { display: true, text: "Median CAGR (%)" } }, y: { grid: { display: false } } },
    },
  });
}

// ============================================================
// Filter population
// ============================================================

/**
 * Repopulate the project-name autocomplete list and region (#regionFilter)
 * dropdown from the given project subset. Preserves the region selection when possible.
 */
function repopulateDropdowns(projects) {
  const regionEl  = document.getElementById("regionFilter");

  const prevRegion = regionEl?.value   ?? "";

  // Project names — show HDB blocks by "address" (= project_name) or condo names.
  state.projectNameList = [...new Set(projects.map(x => x.project_name).filter(Boolean))].sort();

  // Regions — HDB rows have no region so the list may be empty.
  const regions = [...new Set(projects.map(x => x.region).filter(Boolean))].sort();
  regionEl.innerHTML =
    `<option value="">All regions</option>` +
    regions.map(r => `<option value="${escapeHtml(r)}"${r === prevRegion ? " selected" : ""}>${escapeHtml(r)}</option>`).join("");

  // MRT stations for the station search box.
  const stations = [...new Set(projects.map(x => x.nearest_mrt).filter(Boolean))].sort();
  const stationList = document.getElementById("mrt-station-list");
  if (stationList) stationList.innerHTML = stations.map(n => `<option value="${escapeHtml(n)}">`).join("");
}

function populateFilters(projects) {
  // Store full project list so asset-class repopulation can filter it.
  state.allProjects = projects;
  repopulateDropdowns(projects);

  [
    "regionFilter", "minN",
    "minSize", "maxSize",
    "buildYearMin", "buildYearMax",
    "mrtStation", "mrtDistMax", "leaseLeftMin",
    "houseAgeMax", "unitCountMin",
    "maxPsf", "lastTxAfter",
    "school-dist-min", "school-dist-max",
  ].forEach(id => {
    document.getElementById(id)?.addEventListener("input", () => {
      state.page = 1;
      renderProjects();
    });
  });

  // MRT trend lease mode — re-render the trend chart only
  document.getElementById("mrt-trend-lease")?.addEventListener("change", () => renderMrtTrend());

  // Global purchase type radios (explorer) — re-render table only
  document.querySelectorAll('input[name="purchaseType"]').forEach(radio => {
    radio.addEventListener("change", () => {
      state.page = 1;
      renderProjects();
    });
  });

  // Asset class radios (Private / HDB / All) — repopulate dropdowns, then re-render.
  document.querySelectorAll('input[name="assetClass"]').forEach(radio => {
    radio.addEventListener("change", () => {
      const cls = document.querySelector('input[name="assetClass"]:checked')?.value ?? "all";
      const subset = cls === "all"
        ? state.allProjects
        : state.allProjects.filter(p => (p.asset_class ?? "Private") === cls);
      repopulateDropdowns(subset);
      state.page = 1;
      renderProjects();
    });
  });

  // Modal chart controls — purchase type and axis toggle
  document.querySelectorAll('input[name="modalPurchaseType"], input[name="chartAxis"]').forEach(radio => {
    radio.addEventListener("change", () => {
      if (state.modalProject) renderPsfCagrChart(state.modalProject);
    });
  });

  // Project autocomplete search (multi-select)
  const projectInput = document.getElementById("project-search");
  const projectDrop  = document.getElementById("project-autocomplete");

  function showProjectDropdown() {
    if (!projectDrop) return;
    const q = (projectInput?.value ?? "").trim().toLowerCase();
    if (!q) { projectDrop.hidden = true; return; }

    const matches = state.projectNameList.filter(n => n.toLowerCase().includes(q)).slice(0, 30);
    if (!matches.length) { projectDrop.hidden = true; return; }
    projectDrop.innerHTML = matches.map(n => {
      const sel = state.selectedProjects.has(n);
      return `<div class="project-option${sel ? " selected" : ""}" data-name="${escapeHtml(n)}">${escapeHtml(n)}</div>`;
    }).join("");
    projectDrop.hidden = false;
  }

  projectDrop?.addEventListener("mousedown", e => {
    const opt = e.target.closest(".project-option");
    if (!opt) return;
    e.preventDefault();  // keep focus on input
    const name = opt.dataset.name;
    if (state.selectedProjects.has(name)) {
      state.selectedProjects.delete(name);
    } else {
      state.selectedProjects.add(name);
    }
    renderProjectTags();
    showProjectDropdown();
    state.page = 1;
    renderProjects();
  });

  projectInput?.addEventListener("input", () => showProjectDropdown());
  projectInput?.addEventListener("focus", () => showProjectDropdown());
  projectInput?.addEventListener("blur", () => {
    // Small delay so mousedown on an option fires first
    setTimeout(() => { if (projectDrop) projectDrop.hidden = true; }, 150);
  });
  projectInput?.addEventListener("keydown", e => {
    if (e.key === "Escape") { projectDrop.hidden = true; projectInput.blur(); }
  });

  // School autocomplete search
  const schoolInput = document.getElementById("school-search");
  const schoolDrop  = document.getElementById("school-autocomplete");

  function showSchoolDropdown() {
    if (!schoolDrop) return;
    const q = (schoolInput?.value ?? "").trim().toLowerCase();
    if (!q || !state.schools) { schoolDrop.hidden = true; return; }

    const names = new Set();
    Object.values(state.schools).forEach(arr => arr.forEach(s => names.add(s.n)));
    const matches = [...names].filter(n => n.toLowerCase().includes(q)).sort().slice(0, 30);

    if (!matches.length) { schoolDrop.hidden = true; return; }
    schoolDrop.innerHTML = matches.map(n => {
      const sel = state.selectedSchools.has(n);
      return `<div class="school-option${sel ? " selected" : ""}" data-name="${escapeHtml(n)}">${escapeHtml(n)}</div>`;
    }).join("");
    schoolDrop.hidden = false;
  }

  schoolDrop?.addEventListener("mousedown", e => {
    const opt = e.target.closest(".school-option");
    if (!opt) return;
    e.preventDefault();  // keep focus on input
    const name = opt.dataset.name;
    if (state.selectedSchools.has(name)) {
      state.selectedSchools.delete(name);
    } else {
      state.selectedSchools.add(name);
    }
    renderSchoolTags();
    showSchoolDropdown();
    state.page = 1;
    renderProjects();
  });

  schoolInput?.addEventListener("input", () => {
    ensureSchools().then(() => showSchoolDropdown());
  });
  schoolInput?.addEventListener("focus", () => {
    ensureSchools().then(() => showSchoolDropdown());
  });
  schoolInput?.addEventListener("blur", () => {
    // Small delay so mousedown on an option fires first
    setTimeout(() => { if (schoolDrop) schoolDrop.hidden = true; }, 150);
  });
  // Close dropdown on Escape
  schoolInput?.addEventListener("keydown", e => {
    if (e.key === "Escape") { schoolDrop.hidden = true; schoolInput.blur(); }
  });

  document.getElementById("perPage")?.addEventListener("change", e => {
    state.perPage = Number(e.target.value);
    state.page = 1;
    renderProjects();
  });

  // Re-render modal PSF chart when global adjustments change
  ["adj-gfa", "adj-gfa-pct", "adj-lease"].forEach(id => {
    document.getElementById(id)?.addEventListener("change", () => {
      updateAdjNote();
      if (state.modalProject) renderPsfCagrChart(state.modalProject);
    });
  });

  // Re-render modal chart when date range changes
  ["chart-date-min", "chart-date-max"].forEach(id => {
    document.getElementById(id)?.addEventListener("change", () => {
      if (state.modalProject) renderPsfCagrChart(state.modalProject);
    });
  });

  // Explorer bucket tolerance + best-size-only
  document.getElementById("bucketTol")?.addEventListener("input", () => {
    state.page = 1;
    renderProjects();
  });
  document.getElementById("bestSizeOnly")?.addEventListener("change", () => {
    state.page = 1;
    renderProjects();
  });

  // Modal size-bucket tolerance — re-render size table
  document.getElementById("modal-bucket-tol")?.addEventListener("input", () => {
    if (state.modalProject) renderSizeBuckets(state.modalProject);
  });

  // Size-bucket table row click — select bucket and update chart
  document.getElementById("size-bucket-body")?.addEventListener("click", e => {
    const tr = e.target.closest("tr[data-ci]");
    if (!tr) return;
    const ci = parseInt(tr.dataset.ci, 10);
    const clusters = state.sizeBucketClusters;
    if (!clusters?.[ci]) return;
    const cl = clusters[ci];
    // Representative = row with most pairs
    const rep = cl.reduce((a, b) => (b.n > a.n ? b : a));
    // Update modal project sqft to the selected bucket; keep all other project fields
    state.modalProject = { ...state.modalProject, purchase_area_sqft: rep.purchase_area_sqft };
    // Highlight selected row
    document.querySelectorAll("#size-bucket-body tr").forEach(r => r.classList.remove("selected-row"));
    tr.classList.add("selected-row");
    // Update the modal subtitle to reflect the selected bucket
    const subtitleEl = document.getElementById("modal-subtitle");
    if (subtitleEl) {
      const p2 = state.modalProject;
      subtitleEl.textContent =
        `${p2.planning_area} · D${p2.district} · ${Math.round(p2.purchase_area_sqft)} sqft`;
    }
    // Re-render chart for selected bucket
    renderPsfCagrChart(state.modalProject);
  });
}

// ============================================================
// Filter + sort helpers
// ============================================================

function readFilters() {
  const v  = id => (document.getElementById(id)?.value ?? "").trim();
  const nb = id => { const s = v(id); return s === "" ? null : Number(s); };
  return {
    selectedProjects: state.selectedProjects,
    region:         v("regionFilter"),
    assetClass:     document.querySelector('input[name="assetClass"]:checked')?.value ?? "all",
    minN:           nb("minN") ?? 0,
    minSize:        nb("minSize"),
    maxSize:        nb("maxSize"),
    bucketTol:      nb("bucketTol") ?? 0,
    bestSizeOnly:   !!(document.getElementById("bestSizeOnly")?.checked),
    buildYearMin:   nb("buildYearMin"),
    buildYearMax:   nb("buildYearMax"),
    mrtStation:     v("mrtStation").toUpperCase(),
    mrtDistMax:     nb("mrtDistMax"),
    leaseLeftMin:   nb("leaseLeftMin"),
    houseAgeMax:    nb("houseAgeMax"),
    unitCountMin:   nb("unitCountMin"),
    maxPsf:         nb("maxPsf"),
    lastTxAfter:    v("lastTxAfter") || null,   // "YYYY-MM" string or null
    selectedSchools: state.selectedSchools,
    schoolName:     "",   // handled via selectedSchools; text box is for searching only
    schoolDistMin:  nb("school-dist-min"),
    schoolDistMax:  nb("school-dist-max"),
  };
}

// Merge rows within ±bucketTol sqft of each other (same project).
// Keeps the row with the highest pair count as the representative.
// When bestSizeOnly is true, keeps only the best (highest-n) size per project.
function applyBucketMerge(rows, bucketTol, bestSizeOnly) {
  if (bucketTol <= 0 && !bestSizeOnly) return rows;

  // Group by project_name, then cluster sizes within ±tol of each other.
  const byProject = {};
  for (const p of rows) {
    (byProject[p.project_name] ??= []).push(p);
  }

  const result = [];
  for (const [, group] of Object.entries(byProject)) {
    // Sort by sqft ascending
    group.sort((a, b) => a.purchase_area_sqft - b.purchase_area_sqft);

    if (bestSizeOnly) {
      // Keep only the entry with the most pairs
      const best = group.reduce((a, b) => (b.n > a.n ? b : a));
      result.push({ ...best, _allSizes: group.map(p => Math.round(p.purchase_area_sqft)) });
      continue;
    }

    // Cluster sizes: greedily merge neighbours within ±tol
    const clusters = [];
    let cluster = [group[0]];
    for (let i = 1; i < group.length; i++) {
      const prev = cluster[cluster.length - 1];
      if (group[i].purchase_area_sqft - prev.purchase_area_sqft <= bucketTol) {
        cluster.push(group[i]);
      } else {
        clusters.push(cluster);
        cluster = [group[i]];
      }
    }
    clusters.push(cluster);

    for (const cl of clusters) {
      // Representative = highest-n entry in this cluster
      const rep = cl.reduce((a, b) => (b.n > a.n ? b : a));
      const minSqft = Math.round(cl[0].purchase_area_sqft);
      const maxSqft = Math.round(cl[cl.length - 1].purchase_area_sqft);
      result.push({
        ...rep,
        _sqftRange: minSqft === maxSqft ? `${minSqft}` : `${minSqft}–${maxSqft}`,
        _allSizes: cl.map(p => Math.round(p.purchase_area_sqft)),
        _clusterN: cl.reduce((s, p) => s + p.n, 0),
      });
    }
  }
  return result;
}

function applyFilters(projects, f) {
  return projects.filter(p => {
    if (f.selectedProjects.size > 0 && !f.selectedProjects.has(p.project_name)) return false;
    if (f.region  && f.region !== "All regions" && p.region !== f.region)         return false;
    if (f.assetClass && f.assetClass !== "all" && (p.asset_class ?? "Private") !== f.assetClass) return false;
    if (p.n < f.minN)                                                              return false;
    if (f.minSize    != null && p.purchase_area_sqft < f.minSize)                 return false;
    if (f.maxSize    != null && p.purchase_area_sqft > f.maxSize)                 return false;
    if (f.buildYearMin != null && (p.build_year == null || p.build_year < f.buildYearMin)) return false;
    if (f.buildYearMax != null && (p.build_year == null || p.build_year > f.buildYearMax)) return false;
    if (f.mrtStation && !(p.nearest_mrt ?? "").toUpperCase().includes(f.mrtStation)) return false;
    if (f.mrtDistMax   != null && (p.nearest_mrt_distance_m == null || p.nearest_mrt_distance_m > f.mrtDistMax)) return false;
    if (f.leaseLeftMin != null && p.tenure_type !== "freehold" &&
        (p.lease_remaining == null || p.lease_remaining < f.leaseLeftMin))        return false;
    if (f.houseAgeMax  != null && (p.house_age == null || p.house_age > f.houseAgeMax))    return false;
    if (f.unitCountMin != null && (p.total_units == null || p.total_units < f.unitCountMin)) return false;
    if (f.maxPsf       != null && p.median_psf  != null && p.median_psf > f.maxPsf)        return false;
    if (f.lastTxAfter  != null && (p.last_transacted == null || p.last_transacted < f.lastTxAfter)) return false;
    // School proximity filter (only applies when schools are loaded)
    const hasSchoolFilter = f.selectedSchools.size > 0 ||
      f.schoolDistMin != null || f.schoolDistMax != null;
    if (hasSchoolFilter) {
      if (!state.schools) {
        // Schools loading — re-render when ready
        ensureSchools().then(() => { state.page = 1; renderProjects(); });
        return true;  // optimistic: include until loaded
      }
      const nearby = state.schools[p.project_name] ?? [];
      const passes = nearby.some(s => {
        const nameOk = f.selectedSchools.size === 0 || f.selectedSchools.has(s.n);
        const minOk  = f.schoolDistMin == null || s.d >= f.schoolDistMin;
        const maxOk  = f.schoolDistMax == null || s.d <= f.schoolDistMax;
        return nameOk && minOk && maxOk;
      });
      if (!passes) return false;
    }
    return true;
  });
}

function applySort(rows) {
  const { col, dir } = state.sort;
  return [...rows].sort((a, b) => {
    let av = a[col], bv = b[col];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === "string") av = av.toLowerCase();
    if (typeof bv === "string") bv = bv.toLowerCase();
    if (av === bv) return 0;
    return dir === "asc" ? (av > bv ? 1 : -1) : (av < bv ? 1 : -1);
  });
}

// ============================================================
// Project table
// ============================================================

function renderProjects() {
  const f = readFilters();
  const filtered = applyFilters(state.data.projects, f);
  const merged   = applyBucketMerge(filtered, f.bucketTol, f.bestSizeOnly);
  const sorted   = applySort(merged);
  const total    = sorted.length;
  const start    = (state.page - 1) * state.perPage;
  const page     = sorted.slice(start, start + state.perPage);

  // Store rows so event delegation can look them up by index
  state.pageRows = page;

  document.getElementById("projectTable").innerHTML = page.map((p, i) => {
    const sqftLabel = p._sqftRange ?? (p.purchase_area_sqft != null ? fmtNum(Math.round(p.purchase_area_sqft)) : "—");
    const pairsN    = p._clusterN ?? p.n;
    const typeLabel = p.asset_class === "HDB" ? "HDB" : "Condo";
    const typeBadge = p.asset_class === "HDB"
      ? `<span class="badge-hdb">HDB</span>`
      : `<span class="badge-condo">Condo</span>`;
    return `
    <tr>
      <td>
        ${escapeHtml(p.project_name)}
        ${p.gfa_harmonized ? `<span class="badge-gfa" title="GFA harmonised (post Jun 2023)">GFA</span>` : ""}
        <br><small>${escapeHtml(p.planning_area)} · ${p.asset_class === "HDB" ? "" : `D${escapeHtml(p.district)}`}</small>
      </td>
      <td>${typeBadge}</td>
      <td>${fmtNum(pairsN)}</td>
      <td>${p.tx_per_year != null ? p.tx_per_year.toFixed(1) : '<span class="muted-val">—</span>'}</td>
      <td>${fmtPct(p.median_cagr)}</td>
      <td>${fmtCagrCell(p.recent_cagr)}</td>
      <td>${p.cagr_spread != null ? `<span class="muted-val">${p.cagr_spread.toFixed(1)}pp</span>` : '<span class="muted-val">—</span>'}</td>
      <td>${p.cagr_new != null ? fmtPct(p.cagr_new) : '<span class="muted-val">—</span>'}</td>
      <td>${p.cagr_sub != null ? fmtPct(p.cagr_sub) : '<span class="muted-val">—</span>'}</td>
      <td>${p.cagr_res != null ? fmtPct(p.cagr_res) : '<span class="muted-val">—</span>'}</td>
      <td class="${p.profit_rate != null && p.profit_rate >= 90 ? "good" : ""}">${fmtPct(p.profit_rate)}</td>
      <td>${fmtYrs(p.median_holding_years)}</td>
      <td>${sqftLabel}</td>
      <td>${p.median_psf != null ? `$${fmtNum(p.median_psf)}` : '<span class="muted-val">—</span>'}</td>
      <td>${fmtCagrCell(p.psf_cagr)}</td>
      <td>${fmtCagrCell(p.psf_momentum)}</td>
      <td>${p.build_year ?? "—"}</td>
      <td>${leaseLabel(p)}</td>
      <td>${fmtMrtCell(p.nearest_mrt, p.nearest_mrt_distance_m, p.project_name)}</td>
      <td>${fmtNum(p.total_units)}</td>
      <td><button class="btn-view" data-row="${i}">View</button></td>
    </tr>`;
  }).join("");

  updateSortIndicators();
  renderPagination(total);

  state.mrtTrendRows = f.mrtStation ? filtered : null;
  renderMrtTrend();
}

// ============================================================
// MRT area price trend
// ============================================================

// Weighted median of [value, weight] points.
function weightedMedian(points) {
  const sorted = [...points].sort((a, b) => a[0] - b[0]);
  const half = sorted.reduce((s, x) => s + x[1], 0) / 2;
  let acc = 0;
  for (const [v, w] of sorted) {
    acc += w;
    if (acc >= half) return v;
  }
  return null;
}

/**
 * Plot the quarterly median PSF across all filtered rows near the selected MRT station.
 * Rows come from the explorer filters (type, MRT radius, size, and so on).
 * Each row's monthly PSF (ts_size.json) is lease-adjusted for the month's year,
 * then pooled per quarter as a transaction-weighted median.
 */
function renderMrtTrend() {
  const panel = document.getElementById("mrt-trend");
  if (!panel) return;
  const rows = state.mrtTrendRows;
  if (!rows) {
    panel.hidden = true;
    if (state.charts["mrt-trend-chart"]) { state.charts["mrt-trend-chart"].destroy(); delete state.charts["mrt-trend-chart"]; }
    return;
  }
  panel.hidden = false;

  const stations = [...new Set(rows.map(r => r.nearest_mrt).filter(Boolean))];
  document.getElementById("mrt-trend-name").textContent =
    stations.length === 1 ? stations[0] : `"${document.getElementById("mrtStation").value.trim()}" (${stations.length} stations)`;
  const note = document.getElementById("mrt-trend-note");

  if (!state.tsSize) {
    note.textContent = "Loading time series.";
    ensureTsSize().then(() => renderMrtTrend());
    return;
  }

  const mode = document.getElementById("mrt-trend-lease")?.value || "bala";
  const byQ = new Map();  // qKey -> { adj: [[psf, n]], raw: [[psf, n]], n, blocks:Set }
  for (const r of rows) {
    const series = state.tsSize[`${r.project_name}|${Math.round(r.purchase_area_sqft)}`]?.p;
    if (!series) continue;
    for (const [month, psf, n] of series) {
      const qk = monthToQKey(month);
      let q = byQ.get(qk);
      if (!q) byQ.set(qk, q = { adj: [], raw: [], n: 0, blocks: new Set() });
      q.raw.push([psf, n]);
      q.adj.push([psf * leaseFactor(r, parseInt(month.slice(0, 4)), mode), n]);
      q.n += n;
      q.blocks.add(r.project_name);
    }
  }

  const qks = [...byQ.keys()].sort();
  const blocks = new Set(rows.map(r => r.project_name));
  const tx = qks.reduce((s, k) => s + byQ.get(k).n, 0);
  note.textContent = qks.length
    ? `${fmtNum(blocks.size)} projects/blocks, ${fmtNum(tx)} transactions. Uses the explorer filters (Type, MRT ≤, size, and so on). ` +
      `Quarterly median PSF, weighted by transaction count.` +
      (mode === "none" ? "" : ` Lease adjustment uses the remaining lease in each transaction year.`)
    : "No transactions match these filters.";

  const q = qks.map(k => byQ.get(k));
  makeChart("mrt-trend-chart", {
    type: "line",
    data: {
      labels: qks.map(qKeyToLabel),
      datasets: [
        {
          label: mode === "none" ? "Median PSF" : "Lease-adjusted PSF",
          data: q.map(x => Math.round(weightedMedian(x.adj))),
          borderColor: "#2563eb", backgroundColor: "#2563eb",
          borderWidth: 2, pointRadius: 0, tension: .25,
        },
        ...(mode === "none" ? [] : [{
          label: "Raw PSF",
          data: q.map(x => Math.round(weightedMedian(x.raw))),
          borderColor: "#9ca3af", backgroundColor: "#9ca3af",
          borderWidth: 1.5, borderDash: [4, 4], pointRadius: 0, tension: .25,
        }]),
      ],
    },
    options: {
      ...baseOptions(),
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: { display: true, labels: { boxWidth: 12 } },
        tooltip: {
          callbacks: {
            label: c => `${c.dataset.label}: $${fmtNum(c.parsed.y)}`,
            afterBody: items => {
              const x = q[items[0].dataIndex];
              return `${fmtNum(x.n)} tx · ${fmtNum(x.blocks.size)} projects/blocks`;
            },
          },
        },
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: "#667085", maxTicksLimit: 12 } },
        y: { grid: { color: "#edf0f3" }, ticks: { color: "#667085", callback: v => `$${fmtNum(v)}` },
             title: { display: true, text: "PSF ($)" } },
      },
    },
  });
}

// ============================================================
// Sort
// ============================================================

function initSortHeaders() {
  document.querySelectorAll("th[data-col]").forEach(th => {
    th.addEventListener("click", () => {
      const col = th.dataset.col;
      state.sort.dir = state.sort.col === col && state.sort.dir === "desc" ? "asc" : "desc";
      state.sort.col = col;
      state.page = 1;
      renderProjects();
    });
  });
}

function updateSortIndicators() {
  document.querySelectorAll("th[data-col]").forEach(th => {
    th.classList.remove("sort-asc", "sort-desc");
    if (th.dataset.col === state.sort.col) {
      th.classList.add(`sort-${state.sort.dir}`);
    }
  });
}

// ============================================================
// Pagination
// ============================================================

function renderPagination(total) {
  const totalPages = Math.max(1, Math.ceil(total / state.perPage));
  const start = Math.min((state.page - 1) * state.perPage + 1, total);
  const end   = Math.min(state.page * state.perPage, total);
  document.getElementById("pagination").innerHTML = `
    <span class="pg-info">Showing ${fmtNum(start)}--${fmtNum(end)} of ${fmtNum(total)}</span>
    <div class="pg-controls">
      <button class="pg-btn" ${state.page <= 1 ? "disabled" : ""} data-pg="${state.page - 1}">‹ Prev</button>
      <span>Page ${state.page} / ${totalPages}</span>
      <button class="pg-btn" ${state.page >= totalPages ? "disabled" : ""} data-pg="${state.page + 1}">Next ›</button>
    </div>
    <div>Per page: <select id="perPage-inline">
      ${[25,50,100].map(n => `<option ${n === state.perPage ? "selected" : ""}>${n}</option>`).join("")}
    </select></div>
  `;
  document.getElementById("perPage-inline")?.addEventListener("change", e => {
    state.perPage = Number(e.target.value);
    state.page = 1;
    renderProjects();
  });
}

function goPage(p) {
  state.page = p;
  renderProjects();
  document.getElementById("projectExplorer")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ============================================================
// Modal open / close
// ============================================================

// Lazy-load ts.json (compact time series). Returns a Promise.
function ensureTimeSeries() {
  if (state.ts) return Promise.resolve(state.ts);
  if (state.tsLoading) return state.tsLoading;
  state.tsLoading = fetch("ts.json?v=9")
    .then(r => { if (!r.ok) throw new Error("ts.json not found"); return r.json(); })
    .then(data => { state.ts = data; state.tsLoading = null; return data; })
    .catch(err => {
      console.warn("Time series unavailable:", err.message);
      state.ts = {};
      state.tsLoading = null;
      return {};
    });
  return state.tsLoading;
}

function ensureTsSize() {
  if (state.tsSize) return Promise.resolve(state.tsSize);
  if (state.tsSizeLoading) return state.tsSizeLoading;
  state.tsSizeLoading = fetch("ts_size.json?v=10")
    .then(r => { if (!r.ok) throw new Error("ts_size.json not found"); return r.json(); })
    .then(data => { state.tsSize = data; state.tsSizeLoading = null; return data; })
    .catch(err => {
      console.warn("Size time series unavailable:", err.message);
      state.tsSize = {};
      state.tsSizeLoading = null;
      return {};
    });
  return state.tsSizeLoading;
}

function ensureSchools() {
  if (state.schools) return Promise.resolve(state.schools);
  if (state.schoolsLoading) return state.schoolsLoading;
  state.schoolsLoading = fetch("schools.json?v=9")
    .then(r => { if (!r.ok) throw new Error("schools.json not found"); return r.json(); })
    .then(data => { state.schools = data; state.schoolsLoading = null; return data; })
    .catch(err => {
      console.warn("Schools data unavailable:", err.message);
      state.schools = {};
      state.schoolsLoading = null;
      return {};
    });
  return state.schoolsLoading;
}

// ============================================================
// Size-bucket breakdown for modal
// ============================================================

function renderSizeBuckets(p) {
  const tol = parseInt(document.getElementById("modal-bucket-tol")?.value ?? "0", 10) || 0;

  // Get all rows for this project
  const allRows = (state.data?.projects ?? []).filter(r => r.project_name === p.project_name);
  if (!allRows.length) {
    document.getElementById("size-bucket-body").innerHTML =
      `<tr><td colspan="8" class="muted-val">No data.</td></tr>`;
    return;
  }

  // Sort by sqft
  allRows.sort((a, b) => a.purchase_area_sqft - b.purchase_area_sqft);

  // Cluster by tolerance
  const clusters = [];
  let cluster = [allRows[0]];
  for (let i = 1; i < allRows.length; i++) {
    const prev = cluster[cluster.length - 1];
    if (allRows[i].purchase_area_sqft - prev.purchase_area_sqft <= tol) {
      cluster.push(allRows[i]);
    } else {
      clusters.push(cluster);
      cluster = [allRows[i]];
    }
  }
  clusters.push(cluster);

  // Store clusters so the click handler can look them up by index.
  state.sizeBucketClusters = clusters;

  const body = document.getElementById("size-bucket-body");
  if (!body) return;

  const activeSqft = Math.round(state.modalProject?.purchase_area_sqft ?? -1);

  body.innerHTML = clusters.map((cl, ci) => {
    const rep = cl.reduce((a, b) => (b.n > a.n ? b : a));
    const minSqft = Math.round(cl[0].purchase_area_sqft);
    const maxSqft = Math.round(cl[cl.length - 1].purchase_area_sqft);
    const sqftLabel = minSqft === maxSqft ? `${minSqft}` : `${minSqft}–${maxSqft}`;
    const totalN = cl.reduce((s, r) => s + r.n, 0);
    // Mark the row active when it contains the currently selected sqft.
    const isActive = cl.some(r => Math.round(r.purchase_area_sqft) === activeSqft);

    return `<tr data-ci="${ci}" class="bucket-row${isActive ? " selected-row" : ""}">
      <td>${sqftLabel}</td>
      <td>${fmtNum(totalN)}</td>
      <td>${fmtPct(rep.median_cagr)}</td>
      <td>${rep.cagr_new != null ? fmtPct(rep.cagr_new) : '<span class="muted-val">—</span>'}</td>
      <td>${rep.cagr_sub != null ? fmtPct(rep.cagr_sub) : '<span class="muted-val">—</span>'}</td>
      <td>${rep.cagr_res != null ? fmtPct(rep.cagr_res) : '<span class="muted-val">—</span>'}</td>
      <td class="${rep.profit_rate != null && rep.profit_rate >= 90 ? "good" : ""}">${fmtPct(rep.profit_rate)}</td>
      <td>${fmtYrs(rep.median_holding_years)}</td>
    </tr>`;
  }).join("");
}

function openModal(p) {
  state.modalProject = p;

  document.getElementById("modal-title").textContent = p.project_name;
  document.getElementById("modal-subtitle").textContent =
    `${p.planning_area} · D${p.district} · ${Math.round(p.purchase_area_sqft)} sqft`;

  // Address / map link
  const addrEl = document.getElementById("modal-address");
  if (addrEl) {
    const parts = [];
    if (p.postal_code) parts.push(`Singapore ${p.postal_code}`);
    if (p.nearest_mrt) parts.push(`Nearest MRT: ${p.nearest_mrt} (${fmtDist(p.nearest_mrt_distance_m)})`);
    const mapUrl = p.latitude != null && p.longitude != null
      ? `https://www.google.com/maps?q=${p.latitude},${p.longitude}`
      : null;
    addrEl.innerHTML = escapeHtml(parts.join(" · ")) +
      (mapUrl ? ` <a href="${mapUrl}" target="_blank" rel="noopener" class="map-link">View on map ↗</a>` : "");
  }

  const mrtRadiusEl = document.getElementById("mrt-nb-radius");
  if (mrtRadiusEl && p.nearest_mrt_distance_m != null) {
    mrtRadiusEl.value = Math.round(p.nearest_mrt_distance_m);
  }

  updateAdjNote();

  document.getElementById("modal-overlay").hidden = false;
  document.body.style.overflow = "hidden";

  // Show neighbours and size buckets immediately (no async needed)
  renderNeighbours(p);
  renderMrtNeighbours(p);
  renderSizeBuckets(p);

  // Load time series (project-wide + size-bucketed) then render chart
  const canvasEl = document.getElementById("modal-psf-chart");
  if (canvasEl) canvasEl.insertAdjacentHTML("beforebegin", `<p class="muted-sm" id="ts-loading-msg">Loading time series.</p>`);
  ensureTsSize().then(() => {
    document.getElementById("ts-loading-msg")?.remove();
    // Use state.modalProject (not p) so bucket clicks made while loading are respected.
    if (state.modalProject?.project_name === p.project_name) renderPsfCagrChart(state.modalProject);
  });

  // Render school proximity (lazy-load schools.json if not yet loaded)
  const schoolsBody = document.getElementById("schools-body");
  if (schoolsBody) schoolsBody.innerHTML = `<tr><td colspan="2">Loading schools.</td></tr>`;
  ensureSchools().then(() => {
    if (state.modalProject?.project_name === p.project_name) renderSchoolProximity(p);
  });
}

function closeModal() {
  document.getElementById("modal-overlay").hidden = true;
  document.body.style.overflow = "";
  Object.values(state.modalCharts).forEach(c => c.destroy());
  state.modalCharts = {};
  state.modalProject = null;
}

// ============================================================
// PSF + CAGR time-series chart — quarterly helpers
// ============================================================

/** "YYYY-MM" → internal quarter key "YYYY-Qn". */
function monthToQKey(m) {
  return `${m.slice(0, 4)}-Q${Math.ceil(parseInt(m.slice(5, 7)) / 3)}`;
}

/** "YYYY-Qn" → display label "Qn YYYY". */
function qKeyToLabel(qk) {
  const [yr, qPart] = qk.split("-");
  return `${qPart} ${yr}`;
}

/** "Qn YYYY" → internal quarter key "YYYY-Qn". */
function labelToQKey(label) {
  const [qPart, yr] = label.split(" ");
  return `${yr}-${qPart}`;
}

/** Start month of a quarter key: "2023-Q1" → "2023-01". */
function qKeyStartMonth(qk) {
  const q = parseInt(qk.slice(-1));
  return `${qk.slice(0, 4)}-${String((q - 1) * 3 + 1).padStart(2, "0")}`;
}

/** End month of a quarter key: "2023-Q1" → "2023-03". */
function qKeyEndMonth(qk) {
  const q = parseInt(qk.slice(-1));
  return `${qk.slice(0, 4)}-${String(q * 3).padStart(2, "0")}`;
}

/**
 * Aggregate monthly PSF series [[YYYY-MM, psf, n, area?], ...] to quarters.
 * Returns Map keyed by display label "Qn YYYY" → { psf (weighted-median), n (total), area (median) }.
 */
function aggregatePsfToQuarters(monthlyArr) {
  const byKey = new Map();
  for (const [m, psf, n, area] of monthlyArr) {
    const qk = monthToQKey(m);
    if (!byKey.has(qk)) byKey.set(qk, { psfs: [], ns: [], areas: [] });
    const d = byKey.get(qk);
    d.psfs.push(psf);
    d.ns.push(n ?? 1);
    if (area != null) d.areas.push(area);
  }
  const result = new Map();
  for (const [qk, { psfs, ns, areas }] of byKey) {
    const totalN = ns.reduce((s, v) => s + v, 0);
    // Weighted median PSF.
    const pairs = psfs.map((psf, i) => [psf, ns[i]]).sort((a, b) => a[0] - b[0]);
    const half = totalN / 2;
    let cumW = 0, medPsf = pairs[0][0];
    for (const [psf, w] of pairs) { cumW += w; if (cumW >= half) { medPsf = psf; break; } }
    const sortedAreas = areas.slice().sort((a, b) => a - b);
    const medArea = sortedAreas.length > 0 ? sortedAreas[Math.floor(sortedAreas.length / 2)] : null;
    result.set(qKeyToLabel(qk), { psf: Math.round(medPsf), n: totalN, area: medArea });
  }
  return result;
}

/**
 * Generate all display labels "Qn YYYY" between two quarter keys (inclusive).
 */
function allQuartersBetween(qkMin, qkMax) {
  const labels = [];
  let y = parseInt(qkMin.slice(0, 4)), q = parseInt(qkMin.slice(-1));
  const ey = parseInt(qkMax.slice(0, 4)), eq = parseInt(qkMax.slice(-1));
  while (y < ey || (y === ey && q <= eq)) {
    labels.push(`Q${q} ${y}`);
    if (++q > 4) { q = 1; y++; }
  }
  return labels;
}

// ============================================================
// PSF + CAGR time-series chart
// ============================================================

function renderPsfCagrChart(p) {
  const chartAxis = getChartAxis();
  const modalPT   = getModalPurchaseType();

  // Use size-specific data only. Do not fall back to project-wide (which mixes sizes).
  const sizeKey     = `${p.project_name}|${Math.round(p.purchase_area_sqft)}`;
  const hasSizeData = !!(state.tsSize?.[sizeKey]);
  const raw         = hasSizeData ? state.tsSize[sizeKey] : {};
  const sqftLabel   = `${Math.round(p.purchase_area_sqft)} sqft`;

  const sizeNoteEl = document.getElementById("chart-size-note");
  if (sizeNoteEl) {
    sizeNoteEl.textContent = hasSizeData
      ? `${Math.round(p.purchase_area_sqft)} sqft units only`
      : `${Math.round(p.purchase_area_sqft)} sqft — no transactions recorded`;
  }

  const adj     = getAdj();
  const dateMin = document.getElementById("chart-date-min")?.value || null;
  const dateMax = document.getElementById("chart-date-max")?.value || null;
  // Base PSF series for CAGR. Select by purchase type.
  // pn = New Sale, ps = Sub Sale, pr = Resale, p = all transactions (fallback).
  const basePsfArr = ({ all: raw.p, new: raw.pn, sub: raw.ps, res: raw.pr }[modalPT] ?? raw.p) ?? [];
  const basePsfMap = new Map(basePsfArr.map(([m, psf]) => [m, psf]));

  // Quarter-level data — keyed by display label "Qn YYYY".
  let tooltipPsfMap    = new Map();
  let quarters         = [];          // display labels (x-axis)
  let psfBarsData      = [];
  let cagrLineData     = [];
  let volLineData      = [];
  let purchPsfLineData = [];
  let baseMonth        = null;        // first monthly entry of basePsfArr (unchanged)
  let basePsf          = null;

  /** Return true when the quarter key overlaps the [dateMin, dateMax] filter. */
  function qkInRange(qk) {
    const s = qKeyStartMonth(qk), e = qKeyEndMonth(qk);
    if (dateMin && e < dateMin) return false;
    if (dateMax && s > dateMax) return false;
    return true;
  }

  if (chartAxis === "purchase") {
    // Purchase-date view:
    // x-axis = purchase quarter, PSF bars = weighted-median buy-in price.
    // CAGR = (latest market PSF / buy-in PSF)^(12/months_held) − 1.
    // No repeat-sale pairs required.
    const baseQMap = aggregatePsfToQuarters(basePsfArr);
    let allQKeys = [...baseQMap.keys()].map(labelToQKey).sort();
    if (dateMin || dateMax) allQKeys = allQKeys.filter(qkInRange);
    quarters = allQKeys.map(qKeyToLabel);
    tooltipPsfMap = baseQMap;

    psfBarsData = quarters.map(ql => {
      const d = tooltipPsfMap.get(ql);
      if (!d) return null;
      const yr = parseInt(ql.split(" ")[1]);
      return Math.round(d.psf * gfaFactor(p, adj.gfaOn, adj.gfaPct) * leaseFactor(p, yr, adj.leaseNorm));
    });

    // Latest overall market PSF — hypothetical sell price today.
    const allQMap = aggregatePsfToQuarters(raw.p ?? []);
    const sortedAllQL = [...allQMap.keys()].sort((a, b) => labelToQKey(a) < labelToQKey(b) ? -1 : 1);
    const latestQL  = sortedAllQL[sortedAllQL.length - 1] ?? null;
    const latestPsf = latestQL ? allQMap.get(latestQL)?.psf : null;
    const latestQKey = latestQL ? labelToQKey(latestQL) : null;

    cagrLineData = quarters.map(ql => {
      if (!latestPsf || !latestQKey) return null;
      const qk = labelToQKey(ql);
      if (qk >= latestQKey) return null;
      const buyData = tooltipPsfMap.get(ql);
      if (!buyData) return null;
      const [by, bq] = [parseInt(qk.slice(0, 4)), parseInt(qk.slice(-1))];
      const [ly, lq] = [parseInt(latestQKey.slice(0, 4)), parseInt(latestQKey.slice(-1))];
      const monthsHeld = (ly - by) * 12 + (lq - bq) * 3;
      if (monthsHeld <= 0) return null;
      return +((Math.pow(latestPsf / buyData.psf, 12 / monthsHeld) - 1) * 100).toFixed(2);
    });
    volLineData      = quarters.map(ql => tooltipPsfMap.get(ql)?.n ?? null);
    purchPsfLineData = [];

  } else {
    // Sale-date view (default):
    // x-axis = resale quarter, PSF bars = quarterly weighted-median market price.
    // CAGR = (PSF[Q] / base_PSF)^(12/months_elapsed) − 1.
    // base_PSF = earliest monthly entry of the purchase-type-specific series
    // on or after the date-filter start month (or the series' first entry when unfiltered).
    // Quarters with no transactions show null — no interpolation across gaps.
    const psfArr = raw.p  ?? [];
    const ppArr  = raw.pp ?? [];

    const psfQMap = aggregatePsfToQuarters(psfArr);
    const ppQMap  = aggregatePsfToQuarters(ppArr);

    const allPsfQKeys = [...psfQMap.keys()].map(labelToQKey).sort();
    if (allPsfQKeys.length > 0) {
      let allQL = allQuartersBetween(allPsfQKeys[0], allPsfQKeys[allPsfQKeys.length - 1]);
      if (dateMin || dateMax) allQL = allQL.filter(ql => qkInRange(labelToQKey(ql)));
      quarters = allQL;
    }

    tooltipPsfMap = psfQMap;

    psfBarsData = quarters.map(ql => {
      const d = psfQMap.get(ql);
      if (!d) return null;
      const yr = parseInt(ql.split(" ")[1]);
      return Math.round(d.psf * gfaFactor(p, adj.gfaOn, adj.gfaPct) * leaseFactor(p, yr, adj.leaseNorm));
    });

    // Base = earliest monthly entry of the purchase-type series on/after dateMin.
    // Filtering the start date moves the CAGR base with it, so the shown
    // return is measured from the filtered starting point, not project inception.
    const baseEntry = dateMin
      ? basePsfArr.find(([m]) => m >= dateMin)
      : basePsfArr[0];
    baseMonth = baseEntry ? baseEntry[0] : null;
    basePsf   = baseMonth != null ? basePsfMap.get(baseMonth) : null;
    const baseQKey = baseMonth ? monthToQKey(baseMonth) : null;

    // PSF-based cumulative CAGR. Raw prices only — no adjustments.
    cagrLineData = quarters.map(ql => {
      if (!basePsf || !baseQKey) return null;
      const qk = labelToQKey(ql);
      if (qk <= baseQKey) return null;
      const currentD = psfQMap.get(ql);
      if (!currentD) return null;
      // Elapsed months: from base month to start of this quarter.
      const [by, bmo] = baseMonth.split("-").map(Number);
      const qStartMo = (parseInt(qk.slice(-1)) - 1) * 3 + 1;
      const monthsElapsed = (parseInt(qk.slice(0, 4)) - by) * 12 + (qStartMo - bmo);
      if (monthsElapsed <= 0) return null;
      return +((Math.pow(currentD.psf / basePsf, 12 / monthsElapsed) - 1) * 100).toFixed(2);
    });

    volLineData = quarters.map(ql => psfQMap.get(ql)?.n ?? null);

    purchPsfLineData = quarters.map(ql => {
      const d = ppQMap.get(ql);
      if (!d) return null;
      const yr = parseInt(ql.split(" ")[1]);
      return Math.round(d.psf * gfaFactor(p, adj.gfaOn, adj.gfaPct) * leaseFactor(p, yr, adj.leaseNorm));
    });
  }

  const hasPurchPsfLine = purchPsfLineData.some(v => v != null);
  const hasCagrLine     = cagrLineData.some(v => v != null);

  // Volume summary for the filtered range.
  const totalPsfTxns = volLineData.reduce((s, v) => s + (v ?? 0), 0);
  const volSumEl = document.getElementById("chart-vol-summary");
  if (volSumEl) {
    const rangeLabel = (dateMin || dateMax)
      ? `(${dateMin ?? "start"} – ${dateMax ?? "end"})`
      : "(all time)";
    volSumEl.textContent = `${rangeLabel} ${fmtNum(totalPsfTxns)} transactions`;
  }

  // Method note.
  const noteEl = document.getElementById("chart-method-note");
  if (noteEl) {
    const ptLabel = { all: "All txns", new: "New Sale", sub: "Sub Sale", res: "Resale" }[modalPT] ?? "All txns";
    const basePsfStr = basePsf != null ? `$${Math.round(basePsf).toLocaleString()}/psf` : "—";
    if (chartAxis === "purchase") {
      noteEl.innerHTML = `<strong>Purchase-date view (${ptLabel}):</strong> X-axis = quarter the unit was bought.
        PSF bars = weighted-median price paid by ${ptLabel.toLowerCase()} buyers that quarter.
        <span style="color:#16704a"><strong>Green line</strong></span> = annualised CAGR from that buy-in price to the latest available market price. Shows which entry quarter gave the best return to date. No repeat-sale pairs required.`;
    } else {
      noteEl.innerHTML = `<strong>Resale-date view (${ptLabel}):</strong> X-axis = quarter the unit was resold.
        PSF bars = weighted-median market price that quarter. Quarters with no transactions are blank.
        <span style="color:#16704a"><strong>Green line</strong></span> = cumulative CAGR from first <em>${ptLabel.toLowerCase()}</em> price (${baseMonth ?? "—"}, ${basePsfStr}) to each quarter's market PSF. No repeat-sale pairs required. Raw prices — no lease or GFA adjustment.`;
    }
  }

  // Annotation lines: Launch and TOP.
  // Snap to the nearest quarter in the filtered chart.
  const allMonthsForAnnot = [...new Set([
    ...(raw.p  ?? []).map(([m]) => m),
    ...(raw.pp ?? []).map(([m]) => m),
  ])].sort();
  const launchQLabel = allMonthsForAnnot[0] ? qKeyToLabel(monthToQKey(allMonthsForAnnot[0])) : null;
  const topQLabel    = p.build_year
    ? qKeyToLabel(monthToQKey(`${p.build_year}-01`))
    : launchQLabel;

  function nearestChartQuarter(targetLabel) {
    if (!targetLabel || !quarters.length) return null;
    if (quarters.includes(targetLabel)) return targetLabel;
    const targetKey = labelToQKey(targetLabel);
    const after = quarters.find(ql => labelToQKey(ql) >= targetKey);
    return after ?? quarters[quarters.length - 1];
  }
  const launchQ = nearestChartQuarter(launchQLabel);
  const topQ    = nearestChartQuarter(topQLabel);

  const canvasId = "modal-psf-chart";
  if (state.modalCharts[canvasId]) {
    state.modalCharts[canvasId].destroy();
    delete state.modalCharts[canvasId];
  }
  const ctx = document.getElementById(canvasId);
  if (!ctx) return;

  const adjParts = [];
  if (adj.gfaOn && p.gfa_harmonized) adjParts.push(`-${adj.gfaPct}% GFA`);
  if (adj.leaseNorm !== "none" && p.tenure_type === "leasehold") {
    adjParts.push(adj.leaseNorm === "bala" ? "Bala lease norm." : "Prop. lease norm.");
  }
  const psfLabel  = `${chartAxis === "purchase" ? "Purchase" : "Median"} PSF${adjParts.length ? " (" + adjParts.join(", ") + ")" : ""}`;
  const ptLabel2  = { all: "All txns", new: "New Sale", sub: "Sub Sale", res: "Resale" }[modalPT] ?? "All txns";
  const cagrLabel = chartAxis === "purchase"
    ? `CAGR to current price (${ptLabel2} entry)`
    : `CAGR from first ${ptLabel2} price${baseMonth ? " · " + baseMonth : ""}`;
  const volLabel  = chartAxis === "purchase" ? "Buyers / quarter" : "Txns / quarter";

  const verticalLinesPlugin = {
    id: "vertLines",
    afterDraw(chart) {
      const { ctx: c, chartArea: { top, bottom }, scales: { x } } = chart;
      const lines = [];
      const topLabel = p.build_year ? `TOP ${p.build_year}` : "TOP (est.)";
      if (topQ) lines.push({ quarter: topQ, label: topLabel, color: "#4a6fa5" });
      if (launchQ && launchQ !== topQ) lines.push({ quarter: launchQ, label: "Launch", color: "#e07b39" });
      lines.forEach(({ quarter, label, color }) => {
        const xPx = x.getPixelForValue(quarters.indexOf(quarter));
        if (xPx < x.left || xPx > x.right) return;
        c.save();
        c.beginPath(); c.moveTo(xPx, top); c.lineTo(xPx, bottom);
        c.strokeStyle = color; c.lineWidth = 1.5; c.setLineDash([5, 4]); c.stroke();
        c.setLineDash([]);
        c.fillStyle = color; c.font = "bold 9px Inter, sans-serif";
        c.fillText(label, xPx + 3, top + 11);
        c.restore();
      });
    },
  };

  const datasets = [
    {
      type: "bar",
      label: psfLabel,
      data: psfBarsData,
      yAxisID: "yPsf",
      backgroundColor: "rgba(17,24,39,0.12)",
      borderColor: "rgba(17,24,39,0.3)",
      borderWidth: 1,
      order: 4,
    },
    {
      type: "line",
      label: volLabel,
      data: volLineData,
      yAxisID: "yVol",
      borderColor: "rgba(101,131,193,0.7)",
      backgroundColor: "rgba(101,131,193,0.08)",
      pointRadius: 0,
      borderWidth: 1.5,
      fill: true,
      spanGaps: false,
      order: 3,
    },
  ];
  if (hasCagrLine) {
    datasets.splice(1, 0, {
      type: "line",
      label: cagrLabel,
      data: cagrLineData,
      yAxisID: "yCagr",
      borderColor: "#16704a",
      backgroundColor: "transparent",
      pointRadius: 2,
      borderWidth: 2,
      spanGaps: false,
      order: 1,
    });
  }
  if (hasPurchPsfLine) {
    datasets.push({
      type: "line",
      label: `Purchase PSF trend (${sqftLabel})`,
      data: purchPsfLineData,
      yAxisID: "yPsf",
      borderColor: "#e07b39",
      backgroundColor: "transparent",
      pointRadius: 2,
      borderWidth: 1.5,
      borderDash: [4, 3],
      spanGaps: false,
      order: 2,
    });
  }

  state.modalCharts[canvasId] = new Chart(ctx, {
    data: { labels: quarters, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: true, position: "top", labels: { boxWidth: 12, font: { size: 11 } } },
        tooltip: {
          mode: "index",
          intersect: false,
          callbacks: {
            title: items => items[0]?.label ?? "",
            afterBody(items) {
              const ql = items[0]?.label;
              if (!ql) return [];
              const lines = [];
              const psfD  = tooltipPsfMap.get(ql);
              const qIdx  = quarters.indexOf(ql);
              const cagrVal = qIdx >= 0 ? cagrLineData[qIdx] : null;
              if (chartAxis === "purchase") {
                if (psfD) lines.push(`Txns: ${psfD.n} · paid $${Math.round(psfD.psf).toLocaleString()}/psf`);
                if (cagrVal != null) lines.push(`Return to latest: ${cagrVal.toFixed(2)}% p.a.`);
              } else {
                if (psfD) {
                  const areaStr = psfD.area != null ? `${fmtNum(psfD.area)} sqft` : sqftLabel;
                  lines.push(`Txns: ${psfD.n} · med. ${areaStr}`);
                }
                if (cagrVal != null && basePsf != null) {
                  lines.push(`CAGR from ${baseMonth}: ${cagrVal.toFixed(2)}% p.a. · base $${Math.round(basePsf).toLocaleString()}/psf`);
                }
              }
              return lines;
            },
          },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: {
            color: "#667085",
            maxTicksLimit: 20,
            maxRotation: 45,
            callback(val) {
              // Show year label only for Q1 ticks.
              const ql = this.getLabelForValue(val);
              return ql && ql.startsWith("Q1 ") ? ql.split(" ")[1] : "";
            },
          },
        },
        yPsf: {
          type: "linear",
          position: "left",
          title: { display: true, text: "PSF ($)", color: "#667085", font: { size: 11 } },
          grid: { color: "#edf0f3" },
          ticks: { color: "#667085" },
        },
        yCagr: {
          type: "linear",
          position: "right",
          title: { display: true, text: "CAGR (%)", color: "#16704a", font: { size: 11 } },
          grid: { drawOnChartArea: false },
          ticks: { color: "#16704a" },
        },
        yVol: {
          type: "linear",
          position: "right",
          display: false,  // hidden axis — volume shown as background area only
          grid: { drawOnChartArea: false },
          ticks: { display: false },
        },
      },
    },
    plugins: [verticalLinesPlugin],
  });
}

// ============================================================
// Neighbours
// ============================================================

/**
 * For a neighbour project, find the median_psf of the size bucket closest
 * to `targetSqft`. Only matches buckets within 20% of targetSqft.
 * Returns null when no close match exists or the bucket has no PSF data.
 */
function closestBucketPsf(neighbourName, targetSqft) {
  if (targetSqft == null) return null;
  let bestPsf = null, bestDiff = Infinity;
  for (const row of state.data.projects) {
    if (row.project_name !== neighbourName || row.median_psf == null) continue;
    const diff = Math.abs(row.purchase_area_sqft - targetSqft);
    if (diff < bestDiff) { bestDiff = diff; bestPsf = row.median_psf; }
  }
  // Reject match if the closest bucket is more than 20% away in size.
  return (bestDiff / targetSqft <= 0.20) ? bestPsf : null;
}

// Render an MRT cell. Uses the row's own nearest_mrt when populated (consistent
// with the modal). Falls back to state.mrtMap only when the row value is empty
// (pipeline _first_valid gap where a bucket spanning postal codes picks "").
function fmtMrtCell(rowMrt, rowDist, projectName) {
  const mrt  = rowMrt  || state.mrtMap?.[projectName]?.mrt  || null;
  const dist = rowMrt  ? rowDist : (state.mrtMap?.[projectName]?.dist ?? rowDist);
  if (mrt) return `${escapeHtml(mrt)}<br><small>${fmtDist(dist)}</small>`;
  return fmtDist(rowDist);
}

function fmtPsfDiff(pct) {
  if (pct == null) return '<span class="muted-val">—</span>';
  const sign = pct >= 0 ? "+" : "";
  const cls  = pct >= 0 ? "cagr-pos" : "cagr-neg";
  return `<span class="${cls}">${sign}${pct.toFixed(1)}%</span>`;
}

// Return MRT info for a project. O(1) lookup from state.mrtMap (built at load time).
function projectMrtInfo(projectName) {
  return state.mrtMap?.[projectName] ?? null;
}

// One representative per unique project name (highest-n entry).
function buildProjectIndex() {
  const byName = {};
  for (const p of state.data.projects) {
    if (!byName[p.project_name] || p.n > byName[p.project_name].n) {
      byName[p.project_name] = p;
    }
  }
  return Object.values(byName);
}

function renderNeighbours(p) {
  const radius = parseFloat(document.getElementById("nb-radius")?.value) || 500;
  document.getElementById("nb-radius-label").textContent = Math.round(radius);

  const tbody = document.getElementById("neighbours-body");

  if (p.latitude == null || p.longitude == null) {
    tbody.innerHTML = `<tr><td colspan="6">No location data for this project.</td></tr>`;
    return;
  }

  const index = buildProjectIndex();
  const nearby = index
    .filter(q => q.project_name !== p.project_name && q.latitude != null && q.longitude != null)
    .map(q => ({ ...q, _dist: haversineM(p.latitude, p.longitude, q.latitude, q.longitude) }))
    .filter(q => q._dist <= radius)
    .sort((a, b) => a._dist - b._dist)
    .slice(0, 50);

  state.nbRows = nearby;

  tbody.innerHTML = nearby.length
    ? nearby.map((q, i) => {
        const nbPsf   = closestBucketPsf(q.project_name, p.purchase_area_sqft);
        const psfDiff = (nbPsf != null && p.median_psf != null && p.median_psf > 0)
          ? (nbPsf - p.median_psf) / p.median_psf * 100 : null;
        return `<tr>
          <td>${escapeHtml(q.project_name)}${q.gfa_harmonized ? ` <span class="badge-gfa">GFA</span>` : ""}<br><small>${escapeHtml(q.planning_area)}</small></td>
          <td>${fmtDist(q._dist)}</td>
          <td>${fmtNum(q.n)}</td>
          <td>${fmtPct(q.median_cagr)}</td>
          <td>${q.cagr_new != null ? fmtPct(q.cagr_new) : `<span class="muted-val">—</span>`}</td>
          <td>${q.cagr_sub != null ? fmtPct(q.cagr_sub) : `<span class="muted-val">—</span>`}</td>
          <td>${q.cagr_res != null ? fmtPct(q.cagr_res) : `<span class="muted-val">—</span>`}</td>
          <td class="${q.profit_rate != null && q.profit_rate >= 90 ? "good" : ""}">${fmtPct(q.profit_rate)}</td>
          <td>${fmtYrs(q.median_holding_years)}</td>
          <td>${q.purchase_area_sqft != null ? fmtNum(Math.round(q.purchase_area_sqft)) : "—"}</td>
          <td>${fmtPsfDiff(psfDiff)}</td>
          <td>${q.build_year ?? "—"}</td>
          <td>${leaseLabel(q)}</td>
          <td>${fmtMrtCell(q.nearest_mrt, q.nearest_mrt_distance_m, q.project_name)}</td>
          <td>${fmtNum(q.total_units)}</td>
          <td><button class="btn-view btn-sm nb-view" data-row="${i}">View</button></td>
        </tr>`;
      }).join("")
    : `<tr><td colspan="16">No neighbours within ${Math.round(radius)} m.</td></tr>`;
}

function renderMrtNeighbours(p) {
  const radius  = parseFloat(document.getElementById("mrt-nb-radius")?.value) ||
                  (p.nearest_mrt_distance_m ?? 300);
  const mrtName = p.nearest_mrt || "";

  document.getElementById("mrt-nb-radius-val").textContent = Math.round(radius);
  document.getElementById("mrt-nb-name").textContent = mrtName || "—";

  const tbody = document.getElementById("mrt-neighbours-body");

  if (!mrtName) {
    tbody.innerHTML = `<tr><td colspan="6">This project has no MRT data.</td></tr>`;
    return;
  }

  const index = buildProjectIndex();
  const nearby = index
    .filter(q =>
      q.project_name !== p.project_name &&
      q.nearest_mrt === mrtName &&
      q.nearest_mrt_distance_m != null &&
      q.nearest_mrt_distance_m <= radius
    )
    .sort((a, b) => a.nearest_mrt_distance_m - b.nearest_mrt_distance_m)
    .slice(0, 50);

  state.mrtRows = nearby;

  tbody.innerHTML = nearby.length
    ? nearby.map((q, i) => {
        const nbPsf   = closestBucketPsf(q.project_name, p.purchase_area_sqft);
        const psfDiff = (nbPsf != null && p.median_psf != null && p.median_psf > 0)
          ? (nbPsf - p.median_psf) / p.median_psf * 100 : null;
        return `<tr>
          <td>${escapeHtml(q.project_name)}${q.gfa_harmonized ? ` <span class="badge-gfa">GFA</span>` : ""}<br><small>${escapeHtml(q.planning_area)}</small></td>
          <td>${fmtDist(q.nearest_mrt_distance_m)}</td>
          <td>${fmtNum(q.n)}</td>
          <td>${fmtPct(q.median_cagr)}</td>
          <td>${q.cagr_new != null ? fmtPct(q.cagr_new) : `<span class="muted-val">—</span>`}</td>
          <td>${q.cagr_sub != null ? fmtPct(q.cagr_sub) : `<span class="muted-val">—</span>`}</td>
          <td>${q.cagr_res != null ? fmtPct(q.cagr_res) : `<span class="muted-val">—</span>`}</td>
          <td class="${q.profit_rate != null && q.profit_rate >= 90 ? "good" : ""}">${fmtPct(q.profit_rate)}</td>
          <td>${fmtYrs(q.median_holding_years)}</td>
          <td>${q.purchase_area_sqft != null ? fmtNum(Math.round(q.purchase_area_sqft)) : "—"}</td>
          <td>${fmtPsfDiff(psfDiff)}</td>
          <td>${q.build_year ?? "—"}</td>
          <td>${leaseLabel(q)}</td>
          <td>${fmtNum(q.total_units)}</td>
          <td><button class="btn-view btn-sm mrt-view" data-row="${i}">View</button></td>
        </tr>`;
      }).join("")
    : `<tr><td colspan="15">No projects within ${Math.round(radius)} m of ${escapeHtml(mrtName)}.</td></tr>`;
}

// ============================================================
// School proximity
// ============================================================

function renderSchoolProximity(p) {
  const tbody = document.getElementById("schools-body");
  if (!tbody) return;
  const nearby = state.schools?.[p.project_name] ?? [];
  if (!nearby.length) {
    tbody.innerHTML = `<tr><td colspan="2" style="color:var(--muted)">No primary schools within 2&thinsp;km.</td></tr>`;
    return;
  }
  tbody.innerHTML = nearby.map(s => `
    <tr>
      <td>${escapeHtml(s.n)}</td>
      <td>${fmtDist(s.d)}</td>
    </tr>
  `).join("");
}

function renderProjectTags() {
  const el = document.getElementById("project-tags");
  if (!el) return;
  if (!state.selectedProjects.size) { el.innerHTML = ""; return; }
  el.innerHTML = [...state.selectedProjects].sort().map(name => `
    <span class="project-tag">
      ${escapeHtml(name)}
      <span class="project-tag-remove" data-name="${escapeHtml(name)}" title="Remove">×</span>
    </span>
  `).join("");
  el.querySelectorAll(".project-tag-remove").forEach(btn => {
    btn.addEventListener("click", () => {
      state.selectedProjects.delete(btn.dataset.name);
      renderProjectTags();
      state.page = 1;
      renderProjects();
    });
  });
}

function renderSchoolTags() {
  const el = document.getElementById("school-tags");
  if (!el) return;
  if (!state.selectedSchools.size) { el.innerHTML = ""; return; }
  el.innerHTML = [...state.selectedSchools].sort().map(name => `
    <span class="school-tag">
      ${escapeHtml(name)}
      <span class="school-tag-remove" data-name="${escapeHtml(name)}" title="Remove">×</span>
    </span>
  `).join("");
  el.querySelectorAll(".school-tag-remove").forEach(btn => {
    btn.addEventListener("click", () => {
      state.selectedSchools.delete(btn.dataset.name);
      renderSchoolTags();
      state.page = 1;
      renderProjects();
    });
  });
}

// ============================================================
// Utilities
// ============================================================

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  }[c]));
}

// ============================================================
// Event delegation
// ============================================================

function initEventDelegation() {
  // Main project table -- View buttons
  document.getElementById("projectTable").addEventListener("click", e => {
    const btn = e.target.closest(".btn-view[data-row]");
    if (!btn) return;
    openModal(state.pageRows[parseInt(btn.dataset.row)]);
  });

  // Pagination buttons
  document.getElementById("pagination").addEventListener("click", e => {
    const btn = e.target.closest(".pg-btn[data-pg]");
    if (!btn) return;
    goPage(parseInt(btn.dataset.pg));
  });

  // Spatial neighbour table -- View buttons
  document.getElementById("neighbours-body").addEventListener("click", e => {
    const btn = e.target.closest(".nb-view[data-row]");
    if (!btn) return;
    openModal(state.nbRows[parseInt(btn.dataset.row)]);
  });

  // MRT neighbour table -- View buttons
  document.getElementById("mrt-neighbours-body").addEventListener("click", e => {
    const btn = e.target.closest(".mrt-view[data-row]");
    if (!btn) return;
    openModal(state.mrtRows[parseInt(btn.dataset.row)]);
  });

  // Radius inputs re-render neighbour tables
  document.getElementById("nb-radius")?.addEventListener("input", () => {
    if (state.modalProject) renderNeighbours(state.modalProject);
  });
  document.getElementById("mrt-nb-radius")?.addEventListener("input", () => {
    if (state.modalProject) renderMrtNeighbours(state.modalProject);
  });

  // Modal overlay click —' close
  document.getElementById("modal-overlay").addEventListener("click", e => {
    if (e.target === document.getElementById("modal-overlay")) closeModal();
  });

  // Escape key —' close modal
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !document.getElementById("modal-overlay").hidden) closeModal();
  });
}

// ============================================================
// Floating tooltip (JS-based — works inside overflow:auto containers)
// ============================================================

function initTooltips() {
  const tip = document.createElement("div");
  tip.className = "float-tip";
  tip.hidden = true;
  document.body.appendChild(tip);

  document.addEventListener("mouseover", e => {
    const el = e.target.closest("[data-tip]");
    if (!el) { tip.hidden = true; return; }
    tip.textContent = el.dataset.tip;
    tip.hidden = false;
  });
  document.addEventListener("mousemove", e => {
    if (tip.hidden) return;
    const pad = 14;
    let x = e.clientX + pad;
    let y = e.clientY - pad;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    if (x + w > window.innerWidth  - 8) x = e.clientX - w - pad;
    if (y + h > window.innerHeight - 8) y = e.clientY - h + pad;
    tip.style.left = x + "px";
    tip.style.top  = y + "px";
  });
  document.addEventListener("mouseout", e => {
    if (!e.target.closest("[data-tip]")) tip.hidden = true;
  });
}

// ============================================================
// Init
// ============================================================

initTooltips();
initSortHeaders();
initEventDelegation();

loadData().catch(err => {
  document.body.innerHTML += `<div style="padding:30px;color:#a33a3a">
    <strong>Data not loaded.</strong><br>${escapeHtml(err.message)}
  </div>`;
});

