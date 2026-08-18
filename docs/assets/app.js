/* HMMA Survey project page */
(function () {
  'use strict';

  var PAPERS = window.HMMA_PAPERS || [];
  var STATS = window.HMMA_STATS || {};

  var PATTERNS = [
    {
      key: 'LLM + Perception', short: 'P', color: '#2f6f8f',
      roles: ['Perception'],
      desc: 'The LLM reasons over the outputs of specialist perception models (detectors, segmenters, depth estimators, OCR) to ground language in visual or sensory evidence. Typical of visual QA, document understanding, and multimodal reasoning systems.',
      examples: ['ViperGPT', 'HuggingGPT', 'Chameleon']
    },
    {
      key: 'LLM + Perception + Action', short: 'P+A', color: '#4c9a6c',
      roles: ['Perception', 'Action'],
      desc: 'Perception models ground the scene while action models (robot policies, UI executors, navigation controllers) carry out the LLM’s plan. Dominant in embodied navigation, robot manipulation, GUI agents, and autonomous driving.',
      examples: ['RoboGen', 'SG-Nav', 'Agent S']
    },
    {
      key: 'LLM + Perception + Generation', short: 'P+G', color: '#7a5195',
      roles: ['Perception', 'Generation'],
      desc: 'The LLM interprets input through perception models and directs generation models (diffusion image/video/3D generators) to produce artifacts, often iterating with perceptual verification. Common in image editing, 3D synthesis, and medical assistants.',
      examples: ['GenArtist', 'Idea2Img', 'PathAsst']
    },
    {
      key: 'LLM + Generation', short: 'G', color: '#c9a227',
      roles: ['Generation'],
      desc: 'The LLM composes prompts, plans, and constraints for generation models without a dedicated perception channel. Examples include story-to-image pipelines, audio and speech production, and layout-driven synthesis.',
      examples: ['PodAgent', 'Video', 'Speech']
    },
    {
      key: 'LLM + Perception + Generation + Action', short: 'P+G+A', color: '#d1603d',
      roles: ['Perception', 'Generation', 'Action'],
      desc: 'Full-stack HMMAs combining all three specialist roles: perceive the world, imagine or simulate content with generative models, and act on the environment, as in world-model-driven robots and open-world game agents.',
      examples: ['World', 'Game', 'Simul']
    }
  ];

  var patternColor = {};
  PATTERNS.forEach(function (p) { patternColor[p.key] = p.color; });

  function $(s, r) { return (r || document).querySelector(s); }
  function $$(s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function prettyLabel(s) { return String(s || '').replace(/_/g, ' '); }
  function shortPattern(p) {
    return String(p || '').replace('LLM + ', '').replace(/Perception/g, 'P')
      .replace(/Generation/g, 'G').replace(/Action/g, 'A').replace(/ \+ /g, '+');
  }

  /* ---------- theme ---------- */
  var root = document.documentElement;
  var savedTheme = null;
  try { savedTheme = localStorage.getItem('hmma-theme'); } catch (e) {}
  if (savedTheme) root.setAttribute('data-theme', savedTheme);
  else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches)
    root.setAttribute('data-theme', 'dark');

  $('#themeToggle').addEventListener('click', function () {
    var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    root.setAttribute('data-theme', next);
    try { localStorage.setItem('hmma-theme', next); } catch (e) {}
    rebuildCharts();
  });

  /* ---------- nav ---------- */
  var nav = $('#nav');
  window.addEventListener('scroll', function () {
    nav.classList.toggle('scrolled', window.scrollY > 8);
  }, { passive: true });
  $('#navBurger').addEventListener('click', function () {
    $('#navLinks').classList.toggle('open');
  });
  $$('#navLinks a').forEach(function (a) {
    a.addEventListener('click', function () { $('#navLinks').classList.remove('open'); });
  });

  var sectionIds = ['overview', 'taxonomy', 'trends', 'architecture', 'browser', 'pipeline', 'citation'];
  var navAnchors = $$('#navLinks a');
  var spy = new IntersectionObserver(function (entries) {
    entries.forEach(function (en) {
      if (!en.isIntersecting) return;
      navAnchors.forEach(function (a) {
        a.classList.toggle('active', a.getAttribute('href') === '#' + en.target.id);
      });
    });
  }, { rootMargin: '-30% 0px -60% 0px' });
  sectionIds.forEach(function (id) { var el = document.getElementById(id); if (el) spy.observe(el); });

  /* ---------- counters & reveal ---------- */
  var counterObs = new IntersectionObserver(function (entries) {
    entries.forEach(function (en) {
      if (!en.isIntersecting) return;
      counterObs.unobserve(en.target);
      var target = +en.target.getAttribute('data-count');
      var t0 = null;
      function tick(ts) {
        if (!t0) t0 = ts;
        var k = Math.min(1, (ts - t0) / 1200);
        en.target.textContent = Math.round(target * (1 - Math.pow(1 - k, 3))).toLocaleString('en-US');
        if (k < 1) requestAnimationFrame(tick);
      }
      requestAnimationFrame(tick);
    });
  }, { threshold: 0.4 });
  $$('[data-count]').forEach(function (el) { counterObs.observe(el); });

  var revealObs = new IntersectionObserver(function (entries) {
    entries.forEach(function (en) {
      if (en.isIntersecting) { en.target.classList.add('visible'); revealObs.unobserve(en.target); }
    });
  }, { threshold: 0.08 });
  $$('.section, .chart-card, .pstep').forEach(function (el) {
    el.classList.add('reveal'); revealObs.observe(el);
  });

  /* ---------- taxonomy tabs ---------- */
  function papersOf(patternKey) {
    return PAPERS.filter(function (p) { return p.pattern === patternKey; });
  }
  function pickExamples(pattern) {
    var pool = papersOf(pattern.key);
    var picked = [];
    pattern.examples.forEach(function (kw) {
      var hit = pool.find(function (p) {
        return p.title.toLowerCase().indexOf(kw.toLowerCase()) !== -1 &&
          picked.indexOf(p) === -1;
      });
      if (hit) picked.push(hit);
    });
    for (var i = 0; picked.length < 3 && i < pool.length; i++) {
      if (picked.indexOf(pool[i]) === -1) picked.push(pool[i]);
    }
    return picked;
  }

  var tabsEl = $('#patternTabs');
  var panelEl = $('#patternPanel');
  PATTERNS.forEach(function (p, i) {
    var n = (STATS.by_pattern && STATS.by_pattern[p.key]) || papersOf(p.key).length;
    var b = document.createElement('button');
    b.className = 'pattern-tab' + (i === 0 ? ' active' : '');
    b.style.setProperty('--tab-color', p.color);
    b.setAttribute('role', 'tab');
    b.innerHTML = esc(p.short) + '<span class="tab-count">' + n + '</span>';
    b.title = p.key;
    b.addEventListener('click', function () {
      $$('.pattern-tab', tabsEl).forEach(function (t) { t.classList.remove('active'); });
      b.classList.add('active');
      renderPattern(p);
    });
    tabsEl.appendChild(b);
  });

  function patternSVG(p) {
    var bw = 118, bh = 46, gap = 26, pad = 8, top = 8, mid = 62;
    var n = p.roles.length;
    var W = n * bw + (n - 1) * gap + pad * 2;
    var H = top + bh + mid + bh + 8;
    var cx = W / 2;
    var parts = [];
    var i, rx, x, y;
    for (i = 0; i < n; i++) {
      rx = pad + i * (bw + gap) + bw / 2;
      parts.push('<line x1="' + cx + '" y1="' + (top + bh) + '" x2="' + rx + '" y2="' + (top + bh + mid) + '" class="pwire"/>');
    }
    parts.push('<rect x="' + (cx - bw / 2) + '" y="' + top + '" width="' + bw + '" height="' + bh + '" rx="10" class="pll"/>');
    parts.push('<text x="' + cx + '" y="' + (top + bh / 2 - 1) + '" class="ptext" text-anchor="middle">LLM</text>');
    parts.push('<text x="' + cx + '" y="' + (top + bh / 2 + 14) + '" class="psub" text-anchor="middle">orchestrator</text>');
    for (i = 0; i < n; i++) {
      x = pad + i * (bw + gap);
      y = top + bh + mid;
      parts.push('<rect x="' + x + '" y="' + y + '" width="' + bw + '" height="' + bh + '" rx="10" style="fill:' + p.color + '"/>');
      parts.push('<text x="' + (x + bw / 2) + '" y="' + (y + bh / 2 - 1) + '" class="ptext" text-anchor="middle">' + p.roles[i] + '</text>');
      parts.push('<text x="' + (x + bw / 2) + '" y="' + (y + bh / 2 + 14) + '" class="psub" text-anchor="middle">models</text>');
    }
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" class="pattern-svg" role="img" aria-label="' + esc(p.key) + ' diagram">' + parts.join('') + '</svg>';
  }

  function renderPattern(p) {
    var examples = pickExamples(p).map(function (ex) {
      return '<a class="example-item" href="' + esc(ex.url) + '" target="_blank" rel="noopener">' +
        esc(ex.title) + '<span class="ex-venue">' + esc(ex.venue) + ' ' + ex.year + '</span></a>';
    }).join('');
    var n = (STATS.by_pattern && STATS.by_pattern[p.key]) || 0;
    panelEl.innerHTML =
      '<div><div class="pattern-diagram">' + patternSVG(p) + '</div>' +
      '<p class="pattern-desc"><strong>' + esc(p.key) + '</strong>: ' + esc(p.desc) + '</p>' +
      '<p class="pattern-desc"><strong>' + n + '</strong> papers (' + (n / PAPERS.length * 100).toFixed(1) + '% of the surveyed set).</p></div>' +
      '<div class="pattern-examples"><h4>Representative systems</h4><div class="example-list">' + examples + '</div></div>';
  }
  renderPattern(PATTERNS[0]);

  /* ---------- charts ---------- */
  var charts = [];
  function isDark() { return root.getAttribute('data-theme') === 'dark'; }
  function axisColors() {
    return {
      text: isDark() ? '#98a2b3' : '#5b6474',
      line: isDark() ? '#2a3342' : '#e3dfd4',
      tooltipBg: isDark() ? '#1a212c' : '#ffffff'
    };
  }
  function makeChart(id, option) {
    var el = document.getElementById(id);
    if (!el || typeof echarts === 'undefined') return;
    var c = echarts.init(el);
    c.setOption(option);
    charts.push({ inst: c, build: option });
  }
  function baseTooltip() {
    var ac = axisColors();
    return { backgroundColor: ac.tooltipBg, borderColor: ac.line, textStyle: { color: isDark() ? '#e7eaf1' : '#1d2433', fontSize: 12 } };
  }

  function rebuildCharts() {
    charts.forEach(function (c) { c.inst.dispose(); });
    charts = [];
    buildCharts();
  }

  function buildCharts() {
    if (typeof echarts === 'undefined' || !STATS.by_year) return;
    var ac = axisColors();
    var years = Object.keys(STATS.by_year).map(Number).sort();
    var patternKeys = PATTERNS.map(function (p) { return p.key; });

    /* pattern x year stacked bars + total line */
    makeChart('chartPatternYear', {
      tooltip: Object.assign(baseTooltip(), { trigger: 'axis', axisPointer: { type: 'shadow' } }),
      legend: { textStyle: { color: ac.text, fontSize: 11 }, bottom: 0, itemWidth: 14, itemHeight: 9 },
      grid: { left: 48, right: 30, top: 30, bottom: 62 },
      xAxis: { type: 'category', data: years, axisLabel: { color: ac.text }, axisLine: { lineStyle: { color: ac.line } } },
      yAxis: [
        { type: 'value', name: 'papers', nameTextStyle: { color: ac.text }, axisLabel: { color: ac.text }, splitLine: { lineStyle: { color: ac.line } } }
      ],
      series: patternKeys.map(function (k) {
        return {
          name: shortPattern(k), type: 'bar', stack: 'all', barWidth: '55%',
          itemStyle: { color: patternColor[k] },
          data: years.map(function (y) { return (STATS.pattern_by_year[k] || {})[y] || 0; })
        };
      }).concat([{
        name: 'Total', type: 'line', symbol: 'circle', symbolSize: 7,
        lineStyle: { color: isDark() ? '#e7eaf1' : '#1d2433', width: 2 },
        itemStyle: { color: isDark() ? '#e7eaf1' : '#1d2433' },
        data: years.map(function (y) { return STATS.by_year[y] || 0; })
      }])
    });

    /* venue x year heatmap */
    var venues = Object.keys(STATS.by_venue).sort(function (a, b) { return STATS.by_venue[b] - STATS.by_venue[a]; });
    var heatData = [];
    var maxCell = 1;
    venues.forEach(function (v, vi) {
      years.forEach(function (y, yi) {
        var n = (STATS.venue_by_year[v] || {})[y] || 0;
        if (n) { heatData.push([yi, vi, n]); if (n > maxCell) maxCell = n; }
      });
    });
    makeChart('chartVenueYear', {
      tooltip: Object.assign(baseTooltip(), {
        formatter: function (p) { return venues[p.value[1]] + ' ' + years[p.value[0]] + ': <b>' + p.value[2] + '</b>'; }
      }),
      grid: { left: 92, right: 60, top: 16, bottom: 32 },
      xAxis: { type: 'category', data: years, axisLabel: { color: ac.text }, axisLine: { lineStyle: { color: ac.line } }, splitArea: { show: false } },
      yAxis: { type: 'category', data: venues, axisLabel: { color: ac.text, fontSize: 11 }, axisLine: { lineStyle: { color: ac.line } } },
      visualMap: {
        min: 0, max: maxCell, calculable: true, orient: 'vertical', right: 0, top: 'center',
        textStyle: { color: ac.text, fontSize: 11 },
        inRange: { color: isDark() ? ['#1a212c', '#24507c', '#4c8fc4', '#9cc7e8'] : ['#f3f0e8', '#c6d9ec', '#6ea3d1', '#1d4e89'] }
      },
      series: [{
        type: 'heatmap', data: heatData,
        label: { show: true, fontSize: 10, color: isDark() ? '#e7eaf1' : '#1d2433', formatter: function (p) { return p.value[2]; } },
        itemStyle: { borderColor: isDark() ? '#10141b' : '#faf9f6', borderWidth: 2 }
      }]
    });

    /* domain x pattern bubble */
    var domains = Object.keys(STATS.by_domain);
    var bubble = [];
    domains.forEach(function (d) {
      patternKeys.forEach(function (k) {
        var n = ((STATS.domain_by_pattern[d] || {})[k]) || 0;
        if (n) bubble.push({ name: d, value: [shortPattern(k), prettyLabel(d), n] });
      });
    });
    makeChart('chartDomainPattern', {
      tooltip: Object.assign(baseTooltip(), {
        formatter: function (p) { return prettyLabel(p.value[1]) + ' · ' + p.value[0] + '<br><b>' + p.value[2] + '</b> papers'; }
      }),
      grid: { left: 190, right: 40, top: 20, bottom: 36 },
      xAxis: { type: 'category', data: patternKeys.map(shortPattern), axisLabel: { color: ac.text }, axisLine: { lineStyle: { color: ac.line } }, splitLine: { show: true, lineStyle: { color: ac.line } } },
      yAxis: { type: 'category', data: domains.map(prettyLabel), axisLabel: { color: ac.text, fontSize: 11 }, axisLine: { lineStyle: { color: ac.line } }, splitLine: { show: true, lineStyle: { color: ac.line } } },
      series: [{
        type: 'scatter',
        data: bubble,
        symbolSize: function (v) { return 6 + Math.sqrt(v[2]) * 5.2; },
        itemStyle: {
          color: function (p) {
            var k = patternKeys[p.value[0] === 'P' ? 0 : p.value[0] === 'P+A' ? 1 : p.value[0] === 'P+G' ? 2 : p.value[0] === 'G' ? 3 : 4];
            return patternColor[k];
          },
          opacity: 0.82
        },
        label: { show: true, position: 'inside', fontSize: 9, color: '#fff', formatter: function (p) { return p.value[2] > 6 ? p.value[2] : ''; } }
      }]
    });

    /* donut helper */
    function donut(id, dist, palette) {
      var keys = Object.keys(dist);
      makeChart(id, {
        tooltip: Object.assign(baseTooltip(), {
          formatter: function (p) { return prettyLabel(p.name) + ': <b>' + p.value + '</b> (' + p.percent + '%)'; }
        }),
        legend: { bottom: 0, textStyle: { color: ac.text, fontSize: 11 }, itemWidth: 12, itemHeight: 9, type: 'scroll' },
        series: [{
          type: 'pie', radius: ['42%', '68%'], center: ['50%', '44%'],
          itemStyle: { borderColor: isDark() ? '#1a212c' : '#fff', borderWidth: 2, borderRadius: 4 },
          label: { color: ac.text, fontSize: 11, formatter: '{d}%' },
          data: keys.map(function (k, i) {
            return { name: prettyLabel(k), value: dist[k], itemStyle: { color: palette[i % palette.length] } };
          })
        }]
      });
    }
    var archPalette = ['#1d4e89', '#4c8fc4', '#7a5195', '#c9a227', '#4c9a6c', '#d1603d', '#98a2b3'];
    donut('chartFlow', STATS.by_information_flow, archPalette);
    donut('chartInterface', STATS.by_interface_type, archPalette);
    donut('chartFeedback', STATS.by_feedback_structure, archPalette);
    donut('chartUncertainty', STATS.by_uncertainty_handling, archPalette);
    donut('chartCoupling', STATS.by_model_coupling, archPalette);

    /* domains horizontal bar */
    var domPairs = Object.keys(STATS.by_domain).map(function (k) { return [k, STATS.by_domain[k]]; })
      .sort(function (a, b) { return a[1] - b[1]; });
    makeChart('chartDomains', {
      tooltip: baseTooltip(),
      grid: { left: 150, right: 40, top: 10, bottom: 24 },
      xAxis: { type: 'value', axisLabel: { color: ac.text }, splitLine: { lineStyle: { color: ac.line } } },
      yAxis: { type: 'category', data: domPairs.map(function (d) { return prettyLabel(d[0]); }), axisLabel: { color: ac.text, fontSize: 10.5 }, axisLine: { lineStyle: { color: ac.line } } },
      series: [{
        type: 'bar', barWidth: '62%',
        itemStyle: { color: '#4c8fc4', borderRadius: [0, 4, 4, 0] },
        label: { show: true, position: 'right', color: ac.text, fontSize: 10.5 },
        data: domPairs.map(function (d) { return d[1]; })
      }]
    });
  }
  buildCharts();

  var resizeTimer;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { charts.forEach(function (c) { c.inst.resize(); }); }, 150);
  });

  /* ---------- paper browser ---------- */
  var state = { q: '', year: '', venue: '', pattern: '', domain: '', iface: '', code: false, sort: 'year', dir: -1, page: 1, perPage: 25 };
  var filtered = PAPERS.slice();

  var GH_ICON = '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" aria-hidden="true"><path d="M12 .5C5.65.5.5 5.65.5 12c0 5.08 3.29 9.39 7.86 10.91.58.11.79-.25.79-.56v-2.17c-3.2.7-3.87-1.36-3.87-1.36-.52-1.33-1.28-1.69-1.28-1.69-1.04-.71.08-.7.08-.7 1.15.08 1.76 1.19 1.76 1.19 1.03 1.76 2.7 1.25 3.36.96.1-.75.4-1.25.72-1.54-2.55-.29-5.24-1.28-5.24-5.69 0-1.26.45-2.28 1.19-3.09-.12-.29-.52-1.46.11-3.05 0 0 .97-.31 3.18 1.18a11 11 0 0 1 5.8 0c2.2-1.49 3.17-1.18 3.17-1.18.63 1.59.23 2.76.11 3.05.74.81 1.19 1.83 1.19 3.09 0 4.42-2.7 5.39-5.27 5.68.41.36.78 1.05.78 2.13v3.16c0 .31.21.68.8.56A11.5 11.5 0 0 0 23.5 12C23.5 5.65 18.35.5 12 .5z"/></svg>';

  function fillSelect(id, values, keep) {
    var sel = document.getElementById(id);
    values.forEach(function (v) {
      var o = document.createElement('option');
      o.value = v; o.textContent = keep ? v : prettyLabel(v);
      sel.appendChild(o);
    });
  }
  fillSelect('filterYear', Object.keys(STATS.by_year || {}).sort());
  fillSelect('filterVenue', Object.keys(STATS.by_venue || {}).sort());
  fillSelect('filterPattern', PATTERNS.map(function (p) { return p.key; }), true);
  fillSelect('filterDomain', Object.keys(STATS.by_domain || {}).sort());
  fillSelect('filterInterface', Object.keys(STATS.by_interface_type || {}).sort());

  function applyFilters() {
    var q = state.q.trim().toLowerCase();
    filtered = PAPERS.filter(function (p) {
      if (state.year && String(p.year) !== state.year) return false;
      if (state.venue && p.venue !== state.venue) return false;
      if (state.pattern && p.pattern !== state.pattern) return false;
      if (state.domain && p.domain !== state.domain) return false;
      if (state.iface && p.interface !== state.iface) return false;
      if (state.code && !p.code) return false;
      if (q) {
        var hay = (p.title + ' ' + (p.authors || []).join(' ') + ' ' +
          (p.llm_models || []).join(' ') + ' ' +
          ((p.non_llm_models && [].concat(p.non_llm_models.perception || [], p.non_llm_models.generation || [], p.non_llm_models.execution || []).join(' ')) || '')).toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
      return true;
    });
    var dir = state.dir, k = state.sort;
    filtered.sort(function (a, b) {
      var va = a[k], vb = b[k];
      if (typeof va === 'string') { va = va.toLowerCase(); vb = String(vb).toLowerCase(); }
      return (va < vb ? -1 : va > vb ? 1 : 0) * dir;
    });
    state.page = 1;
    renderTable();
  }

  function renderTable() {
    var tbody = $('#paperTbody');
    var total = filtered.length;
    var pages = Math.max(1, Math.ceil(total / state.perPage));
    if (state.page > pages) state.page = pages;
    var start = (state.page - 1) * state.perPage;
    var rows = filtered.slice(start, start + state.perPage);

    $('#resultCount').textContent =
      total.toLocaleString('en-US') + ' papers · page ' + state.page + ' / ' + pages;

    tbody.innerHTML = rows.map(function (p, i) {
      return '<tr data-idx="' + (start + i) + '">' +
        '<td>' + esc(p.title) +
        (p.code ? ' <a class="code-icon" href="' + esc(p.code) + '" target="_blank" rel="noopener" title="Open-source repository" onclick="event.stopPropagation()">' + GH_ICON + '</a>' : '') +
        '</td>' +
        '<td class="venue-cell">' + esc(p.venue) + '</td>' +
        '<td class="year-cell">' + p.year + '</td>' +
        '<td><span class="tag tag-pattern" style="--tag-color:' + (patternColor[p.pattern] || '#5b6474') + '">' + esc(shortPattern(p.pattern)) + '</span></td>' +
        '<td><span class="tag tag-domain">' + esc(prettyLabel(p.domain)) + '</span></td>' +
        '</tr>';
    }).join('');

    $$('tr', tbody).forEach(function (tr) {
      tr.addEventListener('click', function () { openDetail(filtered[+tr.getAttribute('data-idx')]); });
    });

    /* sort indicators */
    $$('#paperTable th').forEach(function (th) {
      th.classList.remove('sorted-asc', 'sorted-desc');
      if (th.getAttribute('data-sort') === state.sort)
        th.classList.add(state.dir === 1 ? 'sorted-asc' : 'sorted-desc');
    });

    /* pagination */
    var pag = $('#pagination');
    var pagesTotal = pages, cur = state.page;
    var btns = [];
    function btn(label, page, opts) {
      opts = opts || {};
      return '<button class="page-btn' + (opts.active ? ' active' : '') + '"' +
        (opts.disabled ? ' disabled' : '') + ' data-page="' + page + '">' + label + '</button>';
    }
    btns.push(btn('‹', cur - 1, { disabled: cur === 1 }));
    var window_ = 2;
    var pagesShown = {};
    [1, pagesTotal].forEach(function (p) { pagesShown[p] = true; });
    for (var d = -window_; d <= window_; d++) {
      var pp = cur + d;
      if (pp >= 1 && pp <= pagesTotal) pagesShown[pp] = true;
    }
    var list = Object.keys(pagesShown).map(Number).sort(function (a, b) { return a - b; });
    var prev = 0;
    list.forEach(function (p) {
      if (p - prev > 1) btns.push('<span class="page-btn" style="border:none;background:none;cursor:default">…</span>');
      btns.push(btn(String(p), p, { active: p === cur }));
      prev = p;
    });
    btns.push(btn('›', cur + 1, { disabled: cur === pagesTotal }));
    pag.innerHTML = btns.join('');
    $$('.page-btn[data-page]', pag).forEach(function (b) {
      b.addEventListener('click', function () {
        if (b.disabled) return;
        state.page = +b.getAttribute('data-page');
        renderTable();
        $('#browser').scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    });
  }

  var searchTimer;
  $('#searchInput').addEventListener('input', function (e) {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(function () { state.q = e.target.value; applyFilters(); }, 180);
  });
  [['filterYear', 'year'], ['filterVenue', 'venue'], ['filterPattern', 'pattern'],
   ['filterDomain', 'domain'], ['filterInterface', 'iface']].forEach(function (pair) {
    document.getElementById(pair[0]).addEventListener('change', function (e) {
      state[pair[1]] = e.target.value; applyFilters();
    });
  });
  $('#filterCode').addEventListener('change', function (e) {
    state.code = e.target.checked; applyFilters();
  });
  $('#resetFilters').addEventListener('click', function () {
    state.q = ''; state.year = ''; state.venue = ''; state.pattern = ''; state.domain = ''; state.iface = ''; state.code = false;
    $('#searchInput').value = '';
    $('#filterCode').checked = false;
    $$('.filter-select').forEach(function (s) { s.value = ''; });
    applyFilters();
  });
  $$('#paperTable th').forEach(function (th) {
    th.addEventListener('click', function () {
      var k = th.getAttribute('data-sort');
      if (state.sort === k) state.dir *= -1;
      else { state.sort = k; state.dir = k === 'year' ? -1 : 1; }
      applyFilters();
    });
  });

  $('#exportCsv').addEventListener('click', function () {
    var header = ['title', 'authors', 'venue', 'year', 'pattern', 'domain', 'interface', 'url'];
    var lines = [header.join(',')];
    filtered.forEach(function (p) {
      lines.push([
        p.title, (p.authors || []).join('; '), p.venue, p.year, p.pattern,
        prettyLabel(p.domain), p.interface || '', p.url
      ].map(function (v) { return '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"'; }).join(','));
    });
    var blob = new Blob(['﻿' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'hmma_papers.csv';
    a.click();
    URL.revokeObjectURL(a.href);
  });

  /* detail overlay */
  var overlay = $('#detailOverlay');
  function openDetail(p) {
    if (!p) return;
    var nm = p.non_llm_models || {};
    var nonLlm = [].concat(nm.perception || [], nm.generation || [], nm.execution || []);
    $('#detailBody').innerHTML =
      '<h3>' + esc(p.title) + '</h3>' +
      '<p class="detail-authors">' + esc((p.authors || []).join(', ')) + '</p>' +
      '<div class="detail-tags">' +
      '<span class="tag tag-pattern" style="--tag-color:' + (patternColor[p.pattern] || '#5b6474') + '">' + esc(shortPattern(p.pattern)) + '</span>' +
      '<span class="tag tag-domain">' + esc(p.venue) + ' ' + p.year + '</span>' +
      '<span class="tag tag-domain">' + esc(prettyLabel(p.domain)) + '</span>' +
      (p.interface ? '<span class="tag tag-domain">' + esc(prettyLabel(p.interface)) + ' interface</span>' : '') +
      '</div>' +
      (p.summary ? '<div class="detail-section"><h5>How the models are composed</h5><p>' + esc(p.summary) + '</p></div>' : '') +
      ((p.llm_models || []).length ? '<div class="detail-section"><h5>LLMs</h5><div class="detail-models">' + p.llm_models.map(function (m) { return '<span class="model-pill">' + esc(m) + '</span>'; }).join('') + '</div></div>' : '') +
      (nonLlm.length ? '<div class="detail-section"><h5>Specialized models</h5><div class="detail-models">' + nonLlm.map(function (m) { return '<span class="model-pill non-llm">' + esc(m) + '</span>'; }).join('') + '</div></div>' : '') +
      ((p.llm_roles || []).length ? '<div class="detail-section"><h5>LLM roles</h5><p>' + esc(p.llm_roles.join(', ')) + '</p></div>' : '') +
      '<div class="detail-section"><h5>Architecture</h5><p>' +
      esc(prettyLabel(p.flow)) + ' flow · ' + esc(prettyLabel(p.coupling)) + ' coupling · ' +
      esc(prettyLabel(p.feedback)) + ' feedback · ' + esc(prettyLabel(p.uncertainty)) + ' uncertainty</p></div>' +
      '<div class="detail-link"><a class="btn btn-outline btn-small" href="' + esc(p.url) + '" target="_blank" rel="noopener">Open paper ↗</a>' +
      (p.code ? ' <a class="btn btn-outline btn-small" href="' + esc(p.code) + '" target="_blank" rel="noopener">' + GH_ICON + ' Open code ↗</a>' : '') +
      '</div>';
    overlay.classList.add('open');
    document.body.style.overflow = 'hidden';
  }
  function closeDetail() {
    overlay.classList.remove('open');
    document.body.style.overflow = '';
  }
  $('#detailClose').addEventListener('click', closeDetail);
  overlay.addEventListener('click', function (e) { if (e.target === overlay) closeDetail(); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeDetail(); });

  /* bibtex copy */
  $('#copyBib').addEventListener('click', function () {
    var btn = this;
    function done() {
      btn.textContent = 'Copied ✓';
      btn.classList.add('copied');
      setTimeout(function () { btn.textContent = 'Copy'; btn.classList.remove('copied'); }, 1600);
    }
    var text = $('#bibtex').textContent;
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, done);
    } else {
      var ta = document.createElement('textarea');
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); } catch (e) {}
      document.body.removeChild(ta); done();
    }
  });

  applyFilters();
})();
